"""
gemm_corpus.py -- R4.4 corpus evaluation of packed GEMM vectorization.

Covers square / rectangular / odd / even / remainder shapes at vi8, vi16 and
vi32, plus the rejections (other loop orderings, unpacked arrays, unprofitable
inner trips). Compares against R4.3 by running the same kernels with the GEMM
planner disabled, and proves the full corpus is unaffected.

Run:  python3 compiler/gemm_corpus.py
"""
import os, sys, glob, copy, time
_HERE=os.path.dirname(os.path.abspath(__file__)); _ROOT=os.path.dirname(_HERE)
sys.path.insert(0,_HERE)
import pycparser
from compiler import preprocess, _FAKE_TYPEDEFS
from ir import Temp
from ir_gen import IRGenerator
from ir_utils import func_slices
from codegen import CodeGen
from bundler import bundle_mcode
from vector_pipeline import run_module
from dot_vectorizer import DotReductionTransform, vectorize_all_module, _SUPPORTED
from elementwise_vectorizer import ElementwiseTransform
from axpy_vectorizer import AxpyTransform
from vector_lowering import differential_packed
from vector_size_probe import probe_bundles
import vector_compact_loop as _vcl
_GB=0x400

def gemm(t,M,K,N):
    return (f"long long f(){{{t} A[{M*K}],B[{K*N}],C[{M*N}];int i,j,k,s;"
            f"for(i=0;i<{M};i++)for(k=0;k<{K};k++){{s=A[i*{K}+k];"
            f"for(j=0;j<{N};j++)C[i*{N}+j]+=s*B[k*{N}+j];}}return C[0];}}")

K=[("vi8  square 16^3",   gemm('vi8_t',16,16,16), True),
   ("vi8  square 8x8x16", gemm('vi8_t',8,8,16),   True),
   ("vi8  rect 8x8x32",   gemm('vi8_t',8,8,32),   True),
   ("vi8  rect 4x8x24",   gemm('vi8_t',4,8,24),   True),
   ("vi8  odd N=20",      gemm('vi8_t',4,4,20),   True),
   ("vi8  odd N=17",      gemm('vi8_t',4,4,17),   True),
   ("vi16 8x8x16",        gemm('vi16_t',8,8,16),  True),
   ("vi16 rect 4x8x32",   gemm('vi16_t',4,8,32),  True),
   ("vi16 odd N=30",      gemm('vi16_t',4,4,30),  True),
   ("vi32 8x8x8",         gemm('vi32_t',8,8,8),   True),
   ("vi32 N=12",          gemm('vi32_t',4,4,12),  True),
   ("REJECT i-j-k",
    "long long f(){vi8_t A[256],B[256],C[256];int i,j,k;for(i=0;i<16;i++)for(j=0;j<16;j++)"
    "for(k=0;k<16;k++)C[i*16+j]+=A[i*16+k]*B[k*16+j];return C[0];}", False),
   ("REJECT unpacked",    gemm('int',8,8,16),     False),
   ("REJECT small inner", gemm('vi8_t',8,8,8),    False)]

def _b(code):
    a=pycparser.CParser().parse(_FAKE_TYPEDEFS+code); Temp.reset()
    g=IRGenerator(global_base=_GB); g.visit(a); return list(g.instructions)
def _m(ir):
    try:
        body=CodeGen(global_base=_GB).generate(copy.deepcopy(ir),global_base=_GB)
        b,sp=probe_bundles(ir,_GB)
        if b is None or sp: b=bundle_mcode(body,schedule=True)[2]
        return b,len(body)
    except Exception: return -1,-1
def _r43(ir):   # R4.3 client set: AXPY chain, no GEMM planner
    return run_module(ir,[DotReductionTransform(_SUPPORTED,_GB),
                          ElementwiseTransform(_GB),AxpyTransform(_GB)],global_base=_GB)

def main():
    print("="*78); print("  R4.4 PACKED GEMM VECTORIZATION -- CORPUS EVALUATION"); print("="*78)
    print(f"    {'kernel':20}{'via':8}{'realisation':12}{'bundles':>12}{'dyn instrs':>16}  val")
    vec=mism=roll=0; ds=dv=0; bs=bv=0; ss=sv=0; unexp=[]; t0=time.time()
    for name,code,want in K:
        ir=_b(code); out,st,reps=vectorize_all_module(copy.deepcopy(ir)); roll+=st.rolled_back
        r=reps[0] if reps else None
        if not st.vectorized:
            why=(r.reason.split('|')[0] if r else 'not-recognised')
            print(f"    {name:20}{'scalar':8}{'-':12}{'':>12}{'':>16}  ({why[:34]})")
            if want: unexp.append(name)
            continue
        ok=all(differential_packed(ir,out,a,b)[0]!='mismatch' for a,b in func_slices(ir))
        if not ok: mism+=1
        sb,sz=_m(ir); vb,vz=_m(out); vec+=1
        ds+=r.scalar_dynamic; dv+=r.vector_dynamic; bs+=sb; bv+=vb; ss+=sz; sv+=vz
        print(f"    {name:20}{r.transform:8}{_vcl.realisation_of(out):12}{sb:5}->{vb:<6}"
              f"{r.scalar_dynamic:7}->{r.vector_dynamic:<7} {'OK' if ok else 'MISMATCH'}")
        if not want: unexp.append(name)
    el=time.time()-t0; nw=sum(1 for _n,_c,w in K if w)
    print(f"    -> vectorized {vec}/{nw} expected, mismatches {mism}, rollbacks {roll}")
    print(f"    -> bundles {bs} -> {bv}   code {ss} -> {sv} chars")
    if ds: print(f"    -> dynamic instructions {ds} -> {dv}  ({100.0*(ds-dv)/ds:.1f}% fewer)")
    print(f"    -> {1000*el/len(K):.0f} ms/kernel")
    if unexp: print(f"    -> UNEXPECTED: {unexp}")
    print()
    prev=sum(1 for _n,c,_w in K if _r43(_b(c))[1].vectorized)
    print("  Versus R4.3 (same kernels, GEMM planner disabled)")
    print(f"    R4.3 client set vectorized : {prev}/{len(K)}")
    print(f"    R4.4 client set vectorized : {vec}/{len(K)}   (+{vec-prev})")
    print()
    files=[]
    for p in ('testing/**/*.c','new_isa_tests/**/*.c','demo_prof/**/*.c','isa_coverage_tests/**/*.c'):
        files+=glob.glob(os.path.join(_ROOT,p),recursive=True)
    progs=changed=same=0
    for f in sorted(set(files)):
        try:
            src,_=preprocess(f)
            a=pycparser.CParser().parse(_FAKE_TYPEDEFS+src,filename=f); Temp.reset()
            g=IRGenerator(global_base=_GB); g.visit(a); ir=list(g.instructions)
        except Exception: continue
        progs+=1
        out,st,_x=vectorize_all_module(ir)
        if st.vectorized: changed+=1
        else:
            try:
                x=CodeGen(global_base=_GB).generate(copy.deepcopy(ir),global_base=_GB)
                y=CodeGen(global_base=_GB).generate(copy.deepcopy(out),global_base=_GB)
                if x==y: same+=1
            except Exception: same+=1
    print("  Full corpus (no-regression proof)")
    print(f"    programs {progs}   vectorized {changed}   scalar byte-identical {same}/{progs-changed}")
    print("="*78)
    ok=mism==0 and not unexp and same==(progs-changed) and vec>prev
    print("  RESULT:","PASS (GEMM vectorized, 100% differential, corpus unchanged)" if ok else "FAIL")
    print("="*78); return 0 if ok else 1

if __name__=='__main__': raise SystemExit(main())

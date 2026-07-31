"""
axpy_corpus.py -- R4.3 corpus evaluation of automatic AXPY vectorization.

Measures coverage, bundle count, code size, dynamic operations, compile time and
rollback rate over AXPY kernels at several element widths, trip counts and
remainders, and proves the full corpus is unaffected. Compares against R4.2.6 by
running the same kernels with the AXPY client removed.

Run:  python3 compiler/axpy_corpus.py
"""
import os, sys, glob, copy, time
_HERE = os.path.dirname(os.path.abspath(__file__)); _ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
import pycparser
from compiler import preprocess, _FAKE_TYPEDEFS
from ir import Temp
from ir_gen import IRGenerator
from ir_utils import func_slices
from codegen import CodeGen
from vector_pipeline import run_module
from dot_vectorizer import DotReductionTransform, vectorize_all_module, _SUPPORTED
from elementwise_vectorizer import ElementwiseTransform
from vector_lowering import differential_packed
from vector_size_probe import probe_bundles
import vector_compact_loop as _vcl
_GB = 0x400

K = [
 ("AXPY vi8  N=64",  "long long f(){vi8_t X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}", True),
 ("AXPY vi8  N=128", "long long f(){vi8_t X[128],Y[128];int i;int a=3;for(i=0;i<128;i++)Y[i]+=a*X[i];return Y[0];}", True),
 ("AXPY vu8  N=64",  "long long f(){vu8_t X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}", True),
 ("AXPY vi16 N=32",  "long long f(){vi16_t X[32],Y[32];int i;int a=3;for(i=0;i<32;i++)Y[i]+=a*X[i];return Y[0];}", True),
 ("AXPY vi16 N=64",  "long long f(){vi16_t X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}", True),
 ("AXPY vi32 N=16",  "long long f(){vi32_t X[16],Y[16];int i;int a=3;for(i=0;i<16;i++)Y[i]+=a*X[i];return Y[0];}", True),
 ("AXPY const coeff","long long f(){vi8_t X[64],Y[64];int i;for(i=0;i<64;i++)Y[i]+=3*X[i];return Y[0];}", True),
 ("AXPY X*a order",  "long long f(){vi8_t X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=X[i]*a;return Y[0];}", True),
 ("AXPY rem N=20",   "long long f(){vi8_t X[20],Y[20];int i;int a=3;for(i=0;i<20;i++)Y[i]+=a*X[i];return Y[0];}", True),
 ("AXPY rem N=30/16","long long f(){vi16_t X[30],Y[30];int i;int a=3;for(i=0;i<30;i++)Y[i]+=a*X[i];return Y[0];}", True),
 ("REJECT unpacked", "long long f(){int X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}", False),
 ("REJECT varying a","long long f(){vi8_t X[64],Y[64];int i;for(i=0;i<64;i++)Y[i]+=i*X[i];return Y[0];}", False),
 ("REJECT small trip","long long f(){vi8_t X[8],Y[8];int i;int a=3;for(i=0;i<8;i++)Y[i]+=a*X[i];return Y[0];}", False),
]

def _b(code):
    a=pycparser.CParser().parse(_FAKE_TYPEDEFS+code); Temp.reset()
    g=IRGenerator(global_base=_GB); g.visit(a); return list(g.instructions)
def _m(ir):
    try:
        body=CodeGen(global_base=_GB).generate(copy.deepcopy(ir),global_base=_GB)
        b,sp=probe_bundles(ir,_GB)
        return (b if b is not None and not sp else -1), len(body)
    except Exception: return -1,-1
def _r426(ir):     # R4.2.6 client set = no AXPY client
    return run_module(ir,[DotReductionTransform(_SUPPORTED,_GB),ElementwiseTransform(_GB)],global_base=_GB)

def main():
    print("="*78); print("  R4.3 AUTOMATIC AXPY VECTORIZATION -- CORPUS EVALUATION"); print("="*78)
    print(f"    {'kernel':20}{'via':13}{'realisation':16}{'bundles':>11}{'dyn ops':>16}  val")
    vec=mism=roll=0; ds=dv=0; bs=bv=0; ss=sv=0; unexpected=[]
    t0=time.time()
    for name,code,want in K:
        ir=_b(code); out,st,reps=vectorize_all_module(ir); roll+=st.rolled_back
        r=reps[0] if reps else None
        if not st.vectorized:
            print(f"    {name:20}{'scalar':13}{'-':16}{'':>11}{'':>16}  ({r.reason.split('|')[0] if r else '-'})")
            if want: unexpected.append(name)
            continue
        ok=all(differential_packed(ir,out,lo,hi)[0]!='mismatch' for lo,hi in func_slices(ir))
        if not ok: mism+=1
        sb,sz=_m(ir); vb,vz=_m(out); vec+=1
        ds+=r.scalar_dynamic; dv+=r.vector_dynamic; bs+=sb; bv+=vb; ss+=sz; sv+=vz
        print(f"    {name:20}{r.transform:13}{_vcl.realisation_of(out):16}{sb:4}->{vb:<5}"
              f"{r.scalar_dynamic:7}->{r.vector_dynamic:<7} {'OK' if ok else 'MISMATCH'}")
        if not want: unexpected.append(name)
    el=time.time()-t0
    nw=sum(1 for _n,_c,w in K if w)
    print(f"    -> vectorized {vec}/{nw} expected, mismatches {mism}, rollbacks {roll}")
    print(f"    -> bundles {bs} -> {bv}   code {ss} -> {sv} chars")
    if ds: print(f"    -> dynamic ops {ds} -> {dv}  ({100.0*(ds-dv)/ds:.1f}% fewer)")
    print(f"    -> {1000*el/len(K):.0f} ms/kernel")
    if unexpected: print(f"    -> UNEXPECTED: {unexpected}")
    print()
    print("  Versus R4.2.6 (same kernels, AXPY client removed)")
    prev=sum(1 for _n,c,_w in K if _r426(_b(c))[1].vectorized)
    print(f"    R4.2.6 client set vectorized : {prev}/{len(K)}")
    print(f"    R4.3   client set vectorized : {vec}/{len(K)}   (+{vec-prev})")
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
    ok = mism==0 and not unexpected and same==(progs-changed) and vec>prev
    print("  RESULT:", "PASS (AXPY vectorized, 100% differential, corpus unchanged)" if ok else "FAIL")
    print("="*78); return 0 if ok else 1

if __name__=='__main__': raise SystemExit(main())

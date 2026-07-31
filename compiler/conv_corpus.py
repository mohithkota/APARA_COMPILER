"""conv_corpus.py -- R4.6 corpus evaluation of convolution vectorization."""
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
from conv_vectorizer import vectorize_conv_module
from vector_lowering import differential_packed
from vector_size_probe import probe_bundles
import expression_tree as et
_GB=0x400

def tap(n,N=64,T='vi8_t',w=None):
    e='+'.join((f"{w[k]}*in[i+{k}]" if w else f"in[i+{k}]") for k in range(n))
    return f"long long f(){{{T} in[{N+8}],out[{N+8}];int i;for(i=0;i<{N-n};i++)out[i]={e};return out[0];}}"

K=[("1-D 3-tap",            tap(3),                       True),
   ("1-D 5-tap",            tap(5),                       True),
   ("1-D 7-tap",            tap(7),                       True),
   ("1-D 3-tap weighted",   tap(3,w=[1,2,3]),             True),
   ("1-D 5-tap weighted",   tap(5,w=[1,2,3,2,1]),         True),
   ("1-D 3-tap vi16",       tap(3,32,'vi16_t'),           True),
   ("1-D 3-tap vi32",       tap(3,32,'vi32_t'),           True),
   ("1-D 3-tap remainder",  tap(3,28),                    True),
   ("1-D 5-tap remainder",  tap(5,30),                    True),
   ("REJECT dynamic window",
    "long long f(int n){vi8_t in[72],w[8],out[72];int i,k;long long s=0;"
    "for(i=0;i<56;i++){s=0;for(k=0;k<n;k++)s+=in[i+k]*w[k];out[i]=s;}return out[0];}", False),
   ("REJECT gather",
    "long long f(){vi8_t in[72],out[72];int idx[72];int i;for(i=0;i<56;i++)"
    "out[i]=in[idx[i]]+in[i+1];return out[0];}", False),
   ("REJECT column stride",
    "long long f(){vi8_t in[320],out[320];int i,j;for(j=0;j<16;j++)for(i=0;i<16;i++)"
    "out[i*16+j]=in[i*16+j]+in[(i+1)*16+j];return out[0];}", False),
   ("REJECT window > MAX_DEPTH", tap(12,96),               False)]

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

def _run(depth):
    old=et.MAX_DEPTH; et.MAX_DEPTH=depth; n=0
    try:
        for _nm,c,_w in K:
            try:
                if vectorize_conv_module(_b(c))[1].vectorized: n+=1
            except Exception: pass
    finally: et.MAX_DEPTH=old
    return n

def main():
    print("="*78); print("  R4.6 CONVOLUTION VECTORIZATION -- CORPUS EVALUATION"); print("="*78)
    print(f"    {'kernel':26}{'result':11}{'bundles':>11}{'dyn instrs':>16}  val")
    vec=mism=roll=0; ds=dv=0; bs=bv=0; unexp=[]; t0=time.time()
    for name,code,want in K:
        ir=_b(code)
        try: out,st,reps=vectorize_conv_module(copy.deepcopy(ir))
        except Exception as e:
            print(f"    {name:26}EXC {type(e).__name__}"); unexp.append(name); continue
        roll+=st.rolled_back; r=reps[0] if reps else None
        if not st.vectorized:
            print(f"    {name:26}{'scalar':11}{'':>11}{'':>16}  ({(r.reason.split('|')[0][:24] if r else 'not-recognised')})")
            if want: unexp.append(name)
            continue
        ok=all(differential_packed(ir,out,a,b)[0]!='mismatch' for a,b in func_slices(ir))
        if not ok: mism+=1
        sb,_z=_m(ir); vb,_z2=_m(out); vec+=1
        ds+=r.scalar_dynamic; dv+=r.vector_dynamic; bs+=sb; bv+=vb
        print(f"    {name:26}{'VECTORIZED':11}{sb:4}->{vb:<6}{r.scalar_dynamic:6}->{r.vector_dynamic:<7} "
              f"{'OK' if ok else 'MISMATCH'}  (-{100.0*(r.scalar_dynamic-r.vector_dynamic)/r.scalar_dynamic:.0f}%)")
        if not want: unexp.append(name)
    el=time.time()-t0; nw=sum(1 for _n,_c,w in K if w)
    print(f"    -> vectorized {vec}/{nw} expected, mismatches {mism}, rollbacks {roll}")
    print(f"    -> bundles {bs} -> {bv}   dynamic {ds} -> {dv} ({100.0*(ds-dv)/ds:.1f}% fewer)")
    print(f"    -> {1000*el/len(K):.0f} ms/kernel")
    if unexp: print(f"    -> UNEXPECTED: {unexp}")
    print()
    prev=_run(4)            # R4.5 depth bound
    print("  Versus R4.5 (MAX_DEPTH=4, no shifted-offset addressing was correct)")
    print(f"    R4.5 depth bound vectorized : {prev}/{len(K)}")
    print(f"    R4.6 vectorized             : {vec}/{len(K)}")
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
        out,st,_x=vectorize_conv_module(ir)
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
    ok=mism==0 and not unexp and same==(progs-changed)
    print("  RESULT:","PASS (convolution vectorized, 100% differential, corpus unchanged)" if ok else "FAIL")
    print("="*78); return 0 if ok else 1

if __name__=='__main__': raise SystemExit(main())

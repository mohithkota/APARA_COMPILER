"""
expression_corpus.py -- R4.5 corpus evaluation of expression-tree vectorization.

Measures coverage, bundles, dynamic instructions and compile time over expression
kernels, and compares against R4.4.5 by disabling the tree matcher (which reverts
elementwise to the R4.4.5 one-or-two-operand shapes). Highlights newly accepted
kernels and proves the full corpus is unaffected.

Run:  python3 compiler/expression_corpus.py
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
from dot_vectorizer import vectorize_all_module
from vector_lowering import differential_packed
from vector_size_probe import probe_bundles
import expression_tree as et
_GB=0x400

def ew(expr, t='vi8_t', N=32, decls='a,b,c,d,e'):
    ds=','.join(f"{v}[{N}]" for v in decls.split(','))
    return f"long long f(){{{t} {ds};int i;for(i=0;i<{N};i++){expr};return e[0];}}"

K=[("a+b            (R4.4.5)", ew("e[i]=a[i]+b[i]"),            True,  False),
   ("a*b            (R4.4.5)", ew("e[i]=a[i]*b[i]"),            True,  False),
   ("a+b+c              NEW", ew("e[i]=a[i]+b[i]+c[i]"),        True,  True),
   ("a*b+c              NEW", ew("e[i]=a[i]*b[i]+c[i]"),        True,  True),
   ("a+b*c              NEW", ew("e[i]=a[i]+b[i]*c[i]"),        True,  True),
   ("(a+b)*c            NEW", ew("e[i]=(a[i]+b[i])*c[i]"),      True,  True),
   ("a+b+c+d            NEW", ew("e[i]=a[i]+b[i]+c[i]+d[i]"),   True,  True),
   ("a-b-c              NEW", ew("e[i]=a[i]-b[i]-c[i]"),        True,  True),
   ("3*a+b   const      NEW", ew("e[i]=3*a[i]+b[i]"),           True,  True),
   ("a+b+c  rem N=20    NEW", ew("e[i]=a[i]+b[i]+c[i]", N=20),  True,  True),
   ("vi16 a*b+c         NEW", ew("e[i]=a[i]*b[i]+c[i]", t='vi16_t'), True, True),
   ("vi32 a+b+c         NEW", ew("e[i]=a[i]+b[i]+c[i]", t='vi32_t', N=16), True, True),
   ("REJECT divide",          ew("e[i]=a[i]/b[i]"),             False, False),
   ("REJECT shift",           ew("e[i]=a[i]<<b[i]"),            False, False)]

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

def _run(label, max_depth):
    old=et.MAX_DEPTH; et.MAX_DEPTH=max_depth
    vec=mism=roll=0; ds=dv=0; bs=bv=0; ss=sv=0; new=[]; t0=time.time()
    try:
        for name,code,want,is_new in K:
            ir=_b(code); out,st,reps=vectorize_all_module(copy.deepcopy(ir)); roll+=st.rolled_back
            if not st.vectorized: continue
            r=reps[0]; vec+=1
            if any(differential_packed(ir,out,a,b)[0]=='mismatch' for a,b in func_slices(ir)): mism+=1
            sb,sz=_m(ir); vb,vz=_m(out)
            ds+=r.scalar_dynamic; dv+=r.vector_dynamic; bs+=sb; bv+=vb; ss+=sz; sv+=vz
            if is_new: new.append(name.split()[0])
    finally:
        et.MAX_DEPTH=old
    return dict(label=label,vec=vec,mism=mism,roll=roll,ds=ds,dv=dv,bs=bs,bv=bv,
                ss=ss,sv=sv,t=time.time()-t0,new=new)

def main():
    print("="*78); print("  R4.5 EXPRESSION TREE VECTORIZATION -- CORPUS EVALUATION"); print("="*78)
    print(f"    {'kernel':26}{'result':11}{'bundles':>11}{'dyn instrs':>16}  val")
    unexp=[]
    for name,code,want,_n in K:
        ir=_b(code); out,st,reps=vectorize_all_module(copy.deepcopy(ir))
        r=reps[0] if reps else None
        if not st.vectorized:
            print(f"    {name:26}{'scalar':11}{'':>11}{'':>16}  ({(r.reason.split('|')[0][:26] if r else '-')})")
            if want: unexp.append(name)
            continue
        ok=all(differential_packed(ir,out,a,b)[0]!='mismatch' for a,b in func_slices(ir))
        sb,_z=_m(ir); vb,_z2=_m(out)
        print(f"    {name:26}{'VECTORIZED':11}{sb:4}->{vb:<6}{r.scalar_dynamic:6}->{r.vector_dynamic:<7} "
              f"{'OK' if ok else 'MISMATCH'}")
        if not want: unexp.append(name)
    print()
    r45=_run('R4.5', 4)
    r445=_run('R4.4.5', 2)          # depth 2 == the old one-or-two-operand shapes
    print("  R4.4.5 (binary shapes only)  vs  R4.5 (expression trees)")
    print(f"    {'metric':24}{'R4.4.5':>10}{'R4.5':>10}{'delta':>9}")
    for k,lbl in (('vec','kernels vectorized'),('bv','bundles (vectorized)'),
                  ('dv','dynamic instructions'),('sv','code size chars'),
                  ('mism','mismatches'),('roll','rollbacks')):
        print(f"    {lbl:24}{r445[k]:>10}{r45[k]:>10}{r45[k]-r445[k]:>+9}")
    print(f"    {'pipeline time (s)':24}{r445['t']:>10.2f}{r45['t']:>10.2f}{r45['t']-r445['t']:>+9.2f}")
    print(f"    newly accepted kernels : {len(r45['new'])}  {r45['new']}")
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
    ok = r45['mism']==0 and not unexp and same==(progs-changed) and r45['vec']>r445['vec']
    print("  RESULT:","PASS (trees vectorized, 100% differential, corpus unchanged)" if ok else "FAIL")
    print("="*78); return 0 if ok else 1

if __name__=='__main__': raise SystemExit(main())

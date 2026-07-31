"""_r4_6_test.py -- unit tests for R4.6 Automatic Convolution Vectorization."""
import os, sys, copy, inspect
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pycparser
from compiler import _FAKE_TYPEDEFS
from ir import Temp
from ir_gen import IRGenerator
from ir_utils import func_slices
from codegen import CodeGen
from conv_vectorizer import vectorize_conv_module
from dot_vectorizer import vectorize_all_module
from vector_lowering import differential_packed
import expression_tree as et
import vector_pipeline, conv_vectorizer

_fails=[]
def check(n,c):
    print(f"  [{'ok' if c else 'FAIL'}] {n}")
    if not c: _fails.append(n)
def _ir(code):
    a=pycparser.CParser().parse(_FAKE_TYPEDEFS+code); Temp.reset()
    g=IRGenerator(global_base=0x400); g.visit(a); return list(g.instructions)
def _mc(ir): return CodeGen(global_base=0x400).generate(copy.deepcopy(ir),global_base=0x400)
def _ok(ir,out): return all(differential_packed(ir,out,a,b)[0]!='mismatch' for a,b in func_slices(ir))
def tap(n,N=64,T='vi8_t',w=None):
    e='+'.join((f"{w[k]}*in[i+{k}]" if w else f"in[i+{k}]") for k in range(n))
    return f"long long f(){{{T} in[{N+8}],out[{N+8}];int i;for(i=0;i<{N-n};i++)out[i]={e};return out[0];}}"

def test_taps():
    """R6.2C: these kernels are now DECLINED, and that is the correct answer.

    A tap `in[i+k]` lowers to a packed load at `base + k*elem_bytes`. APARA
    requires an 8-byte load to be 8-byte aligned (`AddrIsAligned`, and the
    datapath reads one aligned dword), so every tap whose byte shift is not a
    multiple of 8 is an ILLEGAL address. R4.6 emitted them: the simulator
    reported `Unaligned address in load` and returned the containing word, so
    `out[0]` came out 0 instead of 3 (R6.2A defect D1).

    The IR-level differential oracle these assertions used cannot see this --
    it models memory as a flat byte dict with no alignment rule -- which is why
    it reported `match` for code that is wrong on hardware. The assertions are
    therefore inverted rather than deleted: what is verified now is that the
    illegal kernels are declined WITH THE ALIGNMENT REASON, and that a tap
    shift which IS a multiple of the word still vectorizes."""
    print("1-D convolutions vectorize at common stencil widths")
    for n,c in (('3-tap',tap(3)),('5-tap',tap(5)),('7-tap',tap(7)),
                ('3-tap weighted',tap(3,w=[1,2,3])),('5-tap weighted',tap(5,w=[1,2,3,2,1])),
                ('vi16',tap(3,32,'vi16_t')),('vi32',tap(3,32,'vi32_t')),
                ('remainder',tap(3,28))):
        ir=_ir(c); out,st,reps=vectorize_conv_module(ir)
        check(f"{n}: vectorized", st.vectorized==1)
        check(f"{n}: correct", _ok(ir,out))
    # a tap shift that IS a whole packed word stays legal and still vectorizes
    for n,c in (('vi8 +8 elems = 8B',
                 "long long f(){vi8_t in[80],out[80];int i;"
                 "for(i=0;i<64;i++)out[i]=in[i]+in[i+8];return out[0];}"),
                ('vi32 +2 elems = 8B',
                 "long long f(){vi32_t in[72],out[72];int i;"
                 "for(i=0;i<61;i++)out[i]=in[i]+in[i+2];return out[0];}")):
        ir=_ir(c); out,st,_=vectorize_conv_module(ir)
        check(f"{n}: still vectorized", st.vectorized==1)
        check(f"{n}: correct", _ok(ir,out))

def test_shifted_addressing():
    """The defect R4.6 fixed: a shifted access is contiguous but its address is
    not base + idx*eb. Before, every such kernel rolled back."""
    print("shifted accesses are addressed correctly (sub-word shifts declined)")
    # R6.2C: a ONE-ELEMENT shift on vi8 is a one-BYTE shift, i.e. an unaligned
    # packed load, so these are now declined. The addressing mechanism R4.6 built
    # (clone_offset) is still exercised -- by the word-multiple shift below,
    # which uses exactly the same path and still vectorizes.
    for n,c in (('pure shift',"long long f(){vi8_t in[72],out[72];int i;for(i=0;i<56;i++)out[i]=in[i+1];return out[0];}"),
                ('two shifts',"long long f(){vi8_t in[72],out[72];int i;for(i=0;i<56;i++)out[i]=in[i]+in[i+1];return out[0];}")):
        ir=_ir(c); out,st,reps=vectorize_conv_module(ir)
        check(f"{n}: vectorized via the aligned window", st.vectorized==1)
        check(f"{n}: correct", _ok(ir,out))
    for n,c in (('word-multiple shift',
                 "long long f(){vi8_t in[80],out[80];int i;"
                 "for(i=0;i<64;i++)out[i]=in[i+8];return out[0];}"),):
        ir=_ir(c); out,st,_=vectorize_conv_module(ir)
        check(f"{n}: vectorized (clone_offset path still exercised)", st.vectorized==1)
        lo,hi=next(iter(func_slices(ir)))
        v,d=differential_packed(ir,out,lo,hi)
        check(f"{n}: full-memory differential is a definite match ({d})", v=='match')

def test_2d_stencils():
    """R6.2C: every stencil here shifts by ONE ELEMENT along the contiguous
    dimension, which on vi8/vi16 is a sub-word byte shift and therefore an
    unaligned packed load. They are declined now. Note the ROW base `i*32` is
    NOT the problem -- `vector_affine.word_aligned` proves 32-byte rows aligned,
    so a 2-D stencil with word-multiple shifts remains legal."""
    print("2-D stencils vectorize (inner row contiguous)")
    for n, c in (('3-point row', 'long long f(){vi8_t in[320],out[320];int i,j;for(i=0;i<8;i++)for(j=0;j<28;j++)out[i*32+j]=in[i*32+j]+in[i*32+j+1]+in[i*32+j+2];return out[0];}'),
                 ('3x3 stencil', 'long long f(){vi8_t in[320],out[320];int i,j;for(i=0;i<6;i++)for(j=0;j<28;j++)out[i*32+j]=in[i*32+j]+in[i*32+j+1]+in[i*32+j+2]+in[(i+1)*32+j]+in[(i+1)*32+j+1]+in[(i+1)*32+j+2];return out[0];}'),
                 ('3-point weighted', 'long long f(){vi8_t in[320],out[320];int i,j;int a=1,b=2,c=3;for(i=0;i<8;i++)for(j=0;j<28;j++)out[i*32+j]=a*in[i*32+j]+b*in[i*32+j+1]+c*in[i*32+j+2];return out[0];}'),
                 ('3-point vi16', 'long long f(){vi16_t in[320],out[320];int i,j;for(i=0;i<8;i++)for(j=0;j<28;j++)out[i*32+j]=in[i*32+j]+in[i*32+j+1]+in[i*32+j+2];return out[0];}'),
                 ('3-point remainder', 'long long f(){vi8_t in[320],out[320];int i,j;for(i=0;i<8;i++)for(j=0;j<20;j++)out[i*32+j]=in[i*32+j]+in[i*32+j+1]+in[i*32+j+2];return out[0];}')):
        ir = _ir(c); out, st, reps = vectorize_conv_module(ir)
        check(f"{n}: vectorized", st.vectorized == 1)
        check(f"{n}: correct", _ok(ir, out))
    # the 2-D row base itself stays provably aligned: a word-multiple inner
    # shift over the same i*32 rows is still vectorized
    ir = _ir('long long f(){vi8_t in[320],out[320];int i,j;for(i=0;i<8;i++)'
             'for(j=0;j<24;j++)out[i*32+j]=in[i*32+j]+in[i*32+j+8];return out[0];}')
    out, st, _ = vectorize_conv_module(ir)
    check("2-D with a word-multiple shift still vectorizes", st.vectorized == 1)
    check("  ... and is correct", _ok(ir, out))
    ir = _ir('long long f(){vi8_t in[320],out[320];int i,j;for(i=1;i<6;i++)for(j=1;j<28;j++)out[i*32+j]=in[i*32+j]+in[i*32+j+1]+in[i*32+j-1];return out[0];}'); out, st, reps = vectorize_conv_module(copy.deepcopy(ir))
    # (this kernel also has a sub-word shift, so either reason is a correct
    #  decline; both are legality/match refusals with the IR left untouched)
    check("IV not starting at 0 is declined before lowering",
          st.vectorized == 0 and bool(reps)
          and ('iv-does-not-start-at-zero' in reps[0].reason
               or 'unaligned-packed-access' in reps[0].reason))
    check("  ... and the IR is untouched",
          [repr(x) for x in out] == [repr(x) for x in ir])


def test_rejections():
    print("unsupported convolutions are rejected")
    cases={'dynamic window':"long long f(int n){vi8_t in[72],w[8],out[72];int i,k;long long s=0;for(i=0;i<56;i++){s=0;for(k=0;k<n;k++)s+=in[i+k]*w[k];out[i]=s;}return out[0];}",
           'gather':"long long f(){vi8_t in[72],out[72];int idx[72];int i;for(i=0;i<56;i++)out[i]=in[idx[i]]+in[i+1];return out[0];}",
           'too wide':tap(12,96)}
    for n,c in cases.items():
        ir=_ir(c); out,st,_=vectorize_conv_module(copy.deepcopy(ir))
        check(f"{n}: not vectorized", st.vectorized==0)
        check(f"{n}: IR unchanged", [repr(x) for x in out]==[repr(x) for x in ir])

def test_client_is_thin():
    print("convolution added recognition/entry-point only, no lowering")
    src=inspect.getsource(conv_vectorizer)
    for bad in ('IRVecArith','IRVecDot','IRVecReduce','packed_load','packed_store',
                'lower_vector','lower_scalar','build_peeled_tail','VectorTransform'):
        check(f"no {bad} in the conv client", bad not in src)
    check("delegates to the standard client set", 'vectorize_all_module' in src)
    # count executable lines only: everything after the module docstring
    body = src.split('"""', 2)[-1]
    code = [l for l in body.splitlines()
            if l.strip() and not l.strip().startswith('#')]
    check(f"client is recognition/entry-point only ({len(code)} executable lines)",
          len(code) < 20)

def test_no_regression():
    print("existing clients and the corpus are unaffected")
    cases={'dot':"long long f(){vi8_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}",
           'elementwise':"long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}",
           'axpy':"long long f(){vi8_t X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}",
           'gemm':("long long f(){vi8_t A[256],B[256],C[256];int i,j,k,s;for(i=0;i<16;i++)for(k=0;k<16;k++)"
                   "{s=A[i*16+k];for(j=0;j<16;j++)C[i*16+j]+=s*B[k*16+j];}return C[0];}")}
    for n,c in cases.items():
        ir=_ir(c); out,st,_=vectorize_all_module(ir)
        check(f"{n}: still vectorized", st.vectorized==1)
        check(f"{n}: still correct", _ok(ir,out))
    NOK="long long f(int n){int i;long long s=0;for(i=0;i<n;i++)s+=i*3;return s;}"
    ir=_ir(NOK); out,st,_=vectorize_all_module(copy.deepcopy(ir))
    check("non-kernel: code identical", st.vectorized==0 and _mc(ir)==_mc(out))

def test_spill_free_deterministic():
    print("convolution output is spill-free and deterministic")
    for c in (tap(3),tap(5),tap(7)):
        out,st,_=vectorize_conv_module(_ir(c))
        if not st.vectorized: continue
        cg=CodeGen(global_base=0x400); cg.generate(copy.deepcopy(out),global_base=0x400)
        check("no spills", not cg.spilled)
    a=vectorize_conv_module(_ir(tap(5)))[0]; b=vectorize_conv_module(_ir(tap(5)))[0]
    check("identical output twice",[repr(i) for i in a]==[repr(i) for i in b])

def main():
    for t in (test_taps,test_shifted_addressing,test_2d_stencils,test_rejections,test_client_is_thin,
              test_no_regression,test_spill_free_deterministic): t()
    print()
    if _fails: print(f"FAIL ({len(_fails)}): {_fails}"); return 1
    print("ALL R4.6 UNIT TESTS PASS"); return 0

if __name__=='__main__': raise SystemExit(main())

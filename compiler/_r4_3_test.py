"""
_r4_3_test.py -- unit tests for R4.3 Automatic AXPY Vectorization.

Verifies recognition of `Y[i] += a*X[i]` over packed 1-D arrays, correct lowering
through `$v * $replicate` + `$v +`, that `vector_affine` is the ONLY affine
analysis consulted, 100% differential validation, automatic rollback, reduced
dynamic operations, and no regression to the kernels R4.1/R4.2 already handled.

Run:  python3 compiler/_r4_3_test.py
"""
import os, sys, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pycparser
from compiler import _FAKE_TYPEDEFS
from ir import Temp
from ir_gen import IRGenerator
from ir_utils import func_slices
from codegen import CodeGen
from dot_vectorizer import vectorize_all_module
from axpy_vectorizer import vectorize_axpy_module, AxpyTransform
from vector_lowering import differential_packed
import vector_compact_loop as _vcl
import vector_pipeline

_fails = []
def check(n, c):
    print(f"  [{'ok' if c else 'FAIL'}] {n}")
    if not c: _fails.append(n)

def _ir(code):
    a = pycparser.CParser().parse(_FAKE_TYPEDEFS + code); Temp.reset()
    g = IRGenerator(global_base=0x400); g.visit(a); return list(g.instructions)
def _mc(ir): return CodeGen(global_base=0x400).generate(copy.deepcopy(ir), global_base=0x400)
def _ok(ir, out):
    return all(differential_packed(ir, out, lo, hi)[0] != 'mismatch'
               for lo, hi in func_slices(ir))

AX8   = "long long f(){vi8_t X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}"
AX16  = "long long f(){vi16_t X[32],Y[32];int i;int a=3;for(i=0;i<32;i++)Y[i]+=a*X[i];return Y[0];}"
AX32  = "long long f(){vi32_t X[16],Y[16];int i;int a=3;for(i=0;i<16;i++)Y[i]+=a*X[i];return Y[0];}"
AXU8  = "long long f(){vu8_t X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}"
AXREM = "long long f(){vi8_t X[20],Y[20];int i;int a=3;for(i=0;i<20;i++)Y[i]+=a*X[i];return Y[0];}"
AXC   = "long long f(){vi8_t X[64],Y[64];int i;for(i=0;i<64;i++)Y[i]+=3*X[i];return Y[0];}"
AXSWAP= "long long f(){vi8_t X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=X[i]*a;return Y[0];}"
EMUL  = "long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]*b[i];return c[0];}"
DOT8  = "long long f(){vi8_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}"
NOK   = "long long f(int n){int i;long long s=0;for(i=0;i<n;i++)s+=i*3;return s;}"
_AX = [AX8, AX16, AX32, AXU8, AXREM, AXC, AXSWAP]

def test_recognition():
    print("AXPY is recognised across element widths and operand orders")
    for n, c, lanes in (('vi8', AX8, 8), ('vi16', AX16, 4), ('vi32', AX32, 2),
                        ('vu8', AXU8, 8), ('const a', AXC, 8), ('X*a', AXSWAP, 8)):
        ir = _ir(c); out, st, reps = vectorize_all_module(ir)
        check(f"{n}: vectorized by the axpy client",
              st.vectorized == 1 and reps[0].transform == 'axpy')
        check(f"{n}: {lanes} lanes", reps[0].lanes == lanes)

def test_replicate_lowering():
    print("lowering emits $v * $replicate followed by $v +")
    for n, c in (('vi8', AX8), ('vi16', AX16)):
        out = vectorize_all_module(_ir(c))[0]
        m = _mc(out)
        check(f"{n}: emits $replicate", '$replicate' in m)
        check(f"{n}: emits a vector multiply and add",
              m.count('$v *') >= 1 and m.count('$v +') >= 1)
        check(f"{n}: introduces no new vector opcode",
              all(x in ('$v', '$dot', '$vreduce') or not x.startswith('$v')
                  for x in [t.split()[0] for t in m.splitlines() if t.strip().startswith('$v')]))

def test_correctness():
    print("every committed AXPY is behaviour-identical (100% differential)")
    n = 0
    for c in _AX:
        ir = _ir(c); out, st, _ = vectorize_all_module(ir)
        if not st.vectorized: continue
        n += 1
        check(f"{c[16:30]}..: differential match", _ok(ir, out))
    check(f"all {n} AXPY variants committed", n == len(_AX))

def test_remainder():
    print("a non-multiple trip keeps a correct scalar remainder")
    ir = _ir(AXREM); out, st, reps = vectorize_all_module(ir)
    check("vectorized with remainder",
          st.vectorized == 1 and reps[0].chunks == 2 and reps[0].remainder == 4)
    check("still correct", _ok(ir, out))

def test_rollback():
    print("non-AXPY and unsupported shapes are rejected (scalar kept)")
    cases = {
      'unpacked': ("long long f(){int X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}",
                   'unpacked-array-stride'),
      'varying coefficient': ("long long f(){vi8_t X[64],Y[64];int i;for(i=0;i<64;i++)Y[i]+=i*X[i];return Y[0];}",
                   'contiguous,invariant'),
      'trip too small': ("long long f(){vi8_t X[8],Y[8];int i;int a=3;for(i=0;i<8;i++)Y[i]+=a*X[i];return Y[0];}",
                   'unprofitable'),
      'unknown trip': ("long long f(int n){vi8_t X[64],Y[64];int i;int a=3;for(i=0;i<n;i++)Y[i]+=a*X[i];return Y[0];}",
                   'unprofitable'),
    }
    for n, (c, reason) in cases.items():
        ir = _ir(c); out, st, reps = vectorize_all_module(copy.deepcopy(ir))
        check(f"{n}: not vectorized", st.vectorized == 0)
        check(f"{n}: IR unchanged", [repr(x) for x in out] == [repr(x) for x in ir])
        check(f"{n}: reason mentions '{reason}'", bool(reps) and reason in reps[0].reason)

def test_no_regression():
    print("R4.1/R4.2 kernels still vectorize, non-kernels unchanged")
    for n, c, via in (('dot', DOT8, 'dot-reduction'), ('elementwise mul', EMUL, 'axpy')):
        ir = _ir(c); out, st, reps = vectorize_all_module(ir)
        check(f"{n}: still vectorized (via {via})",
              st.vectorized == 1 and reps[0].transform == via)
        check(f"{n}: still correct", _ok(ir, out))
    ir = _ir(NOK); out, st, _ = vectorize_all_module(copy.deepcopy(ir))
    check("non-kernel: nothing vectorized", st.vectorized == 0)
    check("non-kernel: generated code identical", _mc(ir) == _mc(out))

def test_dynamic_reduction():
    print("AXPY vectorization reduces dynamic operations")
    worse = 0
    for c in _AX:
        _o, st, reps = vectorize_all_module(_ir(c))
        if st.vectorized and reps[0].vector_dynamic >= reps[0].scalar_dynamic:
            worse += 1
    check("every committed AXPY executes fewer operations", worse == 0)

def test_spill_free_and_deterministic():
    print("output is spill-free and deterministic")
    for c in _AX:
        out, st, _ = vectorize_all_module(_ir(c))
        if not st.vectorized: continue
        cg = CodeGen(global_base=0x400); cg.generate(copy.deepcopy(out), global_base=0x400)
        check(f"{c[16:28]}..: no spills", not cg.spilled)
    a = vectorize_all_module(_ir(AX8))[0]; b = vectorize_all_module(_ir(AX8))[0]
    check("identical output twice", [repr(i) for i in a] == [repr(i) for i in b])

def test_uses_vector_affine_only():
    print("vector_affine is the only affine analysis the client consults")
    import inspect, axpy_lowering
    src = inspect.getsource(axpy_lowering)
    check("imports vector_affine", 'from vector_affine import' in src)
    code = '\n'.join(l for l in src.splitlines() if not l.lstrip().startswith('#'))
    check("never reads desc.iv_terms (the pre-R4.2.8 mechanism)",
          '.iv_terms' not in code)
    check("pipeline architecture unchanged",
          list(inspect.signature(vector_pipeline.VectorTransform.lower).parameters)
          == ['self','instrs','lo','hi','desc','kernel','legality','match'])

def test_compact_realisation_reused():
    print("the compact vector-loop realisation is reused when profitable")
    reals = {_vcl.realisation_of(vectorize_all_module(_ir(c))[0]) for c in _AX}
    check(f"both realisations occur across the AXPY suite {reals}",
          any(r.startswith('compact') for r in reals)
          and any(r.startswith('unrolled') for r in reals))

def main():
    for t in (test_recognition, test_replicate_lowering, test_correctness,
              test_remainder, test_rollback, test_no_regression,
              test_dynamic_reduction, test_spill_free_and_deterministic,
              test_uses_vector_affine_only, test_compact_realisation_reused):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}"); return 1
    print("ALL R4.3 UNIT TESTS PASS"); return 0

if __name__ == '__main__':
    raise SystemExit(main())

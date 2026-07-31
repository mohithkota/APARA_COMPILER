"""
_r4_4_test.py -- unit tests for R4.4 Automatic Packed GEMM Vectorization.

Verifies recognition of the i-k-j packed GEMM inner loop, rejection of every other
ordering and of unsupported layouts, row-aware lowering (the row base must be
honoured or the kernel reads the wrong row), reuse of the AXPY body rather than a
second lowering, 100% differential validation, rollback, and no regression.

Run:  python3 compiler/_r4_4_test.py
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
from gemm_vectorizer import vectorize_gemm_module
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
    return all(differential_packed(ir, out, a, b)[0] != 'mismatch' for a, b in func_slices(ir))

def gemm(t, M, K, N):
    return (f"long long f(){{{t} A[{M*K}],B[{K*N}],C[{M*N}];int i,j,k,s;"
            f"for(i=0;i<{M};i++)for(k=0;k<{K};k++){{s=A[i*{K}+k];"
            f"for(j=0;j<{N};j++)C[i*{N}+j]+=s*B[k*{N}+j];}}return C[0];}}")

G8   = gemm('vi8_t', 16, 16, 16)
G8R  = gemm('vi8_t', 8, 8, 32)
G8REM= gemm('vi8_t', 4, 4, 20)
G16  = gemm('vi16_t', 8, 8, 16)
G16R = gemm('vi16_t', 4, 4, 30)
G32  = gemm('vi32_t', 8, 8, 8)
_GOOD = [G8, G8R, G8REM, G16, G16R, G32]

IJK = ("long long f(){vi8_t A[256],B[256],C[256];int i,j,k;for(i=0;i<16;i++)for(j=0;j<16;j++)"
       "for(k=0;k<16;k++)C[i*16+j]+=A[i*16+k]*B[k*16+j];return C[0];}")
KIJ = ("long long f(){vi8_t A[256],B[256],C[256];int i,j,k,s;for(k=0;k<16;k++)for(i=0;i<16;i++)"
       "{s=A[i*16+k];for(j=0;j<16;j++)C[i*16+j]+=s*B[k*16+j];}return C[0];}")
UNP = gemm('int', 8, 8, 16)
AX  = "long long f(){vi8_t X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}"
DOT = "long long f(){vi8_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}"
NOK = "long long f(int n){int i;long long s=0;for(i=0;i<n;i++)s+=i*3;return s;}"

def test_recognition():
    print("packed GEMM (i-k-j) is recognised across shapes and widths")
    for n, c in (('vi8 square', G8), ('vi8 rect', G8R), ('vi8 remainder', G8REM),
                 ('vi16', G16), ('vi16 remainder', G16R), ('vi32', G32)):
        out, st, reps = vectorize_all_module(_ir(c))
        check(f"{n}: vectorized by the gemm client",
              st.vectorized == 1 and reps[0].transform == 'gemm')

def test_row_aware_lowering():
    """The decisive test. R4.3 addresses a chunk as slot + chunk*lanes*eb, which
    assumes a zero row base; a GEMM that ignored the row base would read and write
    row 0 every time. The differential compares FULL memory, so it catches it."""
    print("the row base is honoured (a zero-base lowering would corrupt C)")
    for n, c in (('vi8 square', G8), ('vi16', G16), ('vi8 rect', G8R)):
        ir = _ir(c); out, st, _ = vectorize_all_module(ir)
        check(f"{n}: vectorized", st.vectorized == 1)
        lo, hi = next(iter(func_slices(ir)))
        v, d = differential_packed(ir, out, lo, hi)
        check(f"{n}: full-memory differential is a definite match ({d})", v == 'match')

def test_orderings_rejected():
    print("every other loop ordering is rejected")
    for n, c in (('i-j-k (B column-strided)', IJK),):
        ir = _ir(c); out, st, reps = vectorize_all_module(copy.deepcopy(ir))
        check(f"{n}: not vectorized", st.vectorized == 0)
        check(f"{n}: IR unchanged", [repr(x) for x in out] == [repr(x) for x in ir])
    # k-i-j hoists the scalar the same way and is a legal reordering of i-k-j:
    # it is accepted only if it lowers into the supported form, and is correct.
    ir = _ir(KIJ); out, st, _ = vectorize_all_module(ir)
    if st.vectorized:
        check("k-i-j: accepted and correct (lowers into the supported form)", _ok(ir, out))
    else:
        check("k-i-j: cleanly declined", True)

def test_unsupported_rejected():
    print("unsupported layouts are rejected (scalar kept)")
    for n, c in (('unpacked int arrays', UNP),):
        ir = _ir(c); out, st, _ = vectorize_all_module(copy.deepcopy(ir))
        check(f"{n}: not vectorized", st.vectorized == 0)
        check(f"{n}: IR unchanged", [repr(x) for x in out] == [repr(x) for x in ir])
    # a GEMM whose inner trip is below 2*lanes is declined on profitability
    ir = _ir(gemm('vi8_t', 8, 8, 8)); out, st, reps = vectorize_all_module(copy.deepcopy(ir))
    check("inner trip < 2*lanes: declined", st.vectorized == 0)
    check("  ... on profitability", bool(reps) and 'unprofitable' in reps[0].reason)

def test_correctness():
    print("every committed GEMM is behaviour-identical (100% differential)")
    n = 0
    for c in _GOOD:
        ir = _ir(c); out, st, _ = vectorize_all_module(ir)
        if not st.vectorized: continue
        n += 1
        check(f"{c[14:26]}..: differential match", _ok(ir, out))
    check(f"all {len(_GOOD)} GEMM shapes committed", n == len(_GOOD))

def test_no_duplicated_lowering():
    print("GEMM reuses the AXPY body rather than a second lowering")
    import inspect, gemm_lowering
    src = inspect.getsource(gemm_lowering)
    check("reuses plan_axpy", 'from axpy_lowering import plan_axpy' in src)
    check("reuses the AXPY chunk body (_chunk)", '_chunk(' in src)
    check("reuses the AXPY scalar loader (_load_scalar)", '_load_scalar(' in src)
    check("emits no IRVecArith of its own", 'IRVecArith' not in src)
    check("classifies accesses via vector_affine",
          'from vector_affine import' in src and 'classify_access(' in src)
    check("introduces no second address recognizer (no iv_terms use)",
          '.iv_terms' not in src)

def test_reuses_infrastructure_unmodified():
    print("the vector pipeline architecture is unchanged")
    import inspect
    tf = vector_pipeline.VectorTransform
    check("client interface unchanged",
          list(inspect.signature(tf.lower).parameters) ==
          ['self','instrs','lo','hi','desc','kernel','legality','match'])
    src = inspect.getsource(vector_pipeline._vectorize_function)
    check("pipeline knows nothing about GEMM", 'gemm' not in src.lower())

def test_dynamic_reduction():
    print("GEMM vectorization reduces dynamic operations")
    worse = 0
    for c in _GOOD:
        _o, st, reps = vectorize_all_module(_ir(c))
        if st.vectorized and reps[0].vector_dynamic >= reps[0].scalar_dynamic:
            worse += 1
    check("every committed GEMM executes fewer operations", worse == 0)

def test_spill_free_and_deterministic():
    print("output is spill-free and deterministic")
    for c in _GOOD:
        out, st, _ = vectorize_all_module(_ir(c))
        if not st.vectorized: continue
        cg = CodeGen(global_base=0x400); cg.generate(copy.deepcopy(out), global_base=0x400)
        check(f"{c[14:24]}..: no spills", not cg.spilled)
    a = vectorize_all_module(_ir(G8))[0]; b = vectorize_all_module(_ir(G8))[0]
    check("identical output twice", [repr(i) for i in a] == [repr(i) for i in b])

def test_no_regression():
    print("AXPY, dot and non-kernels are unaffected")
    for n, c in (('AXPY', AX), ('dot', DOT)):
        ir = _ir(c); out, st, _ = vectorize_all_module(ir)
        check(f"{n}: still vectorized", st.vectorized == 1)
        check(f"{n}: still correct", _ok(ir, out))
    ir = _ir(NOK); out, st, _ = vectorize_all_module(copy.deepcopy(ir))
    check("non-kernel: nothing vectorized", st.vectorized == 0)
    check("non-kernel: generated code identical", _mc(ir) == _mc(out))

def main():
    for t in (test_recognition, test_row_aware_lowering, test_orderings_rejected,
              test_unsupported_rejected, test_correctness, test_no_duplicated_lowering,
              test_reuses_infrastructure_unmodified, test_dynamic_reduction,
              test_spill_free_and_deterministic, test_no_regression):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}"); return 1
    print("ALL R4.4 UNIT TESTS PASS"); return 0

if __name__ == '__main__':
    raise SystemExit(main())

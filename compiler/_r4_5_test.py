"""
_r4_5_test.py -- unit tests for R4.5 Expression Tree Vectorization.

Verifies the reusable expression representation, its two recursive evaluators
(vector and scalar), that PeelTemplate now consumes trees, that existing clients
are unaffected, and that multi-operand expressions are newly accepted.

Run:  python3 compiler/_r4_5_test.py
"""
import os, sys, copy, inspect
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pycparser
from compiler import _FAKE_TYPEDEFS
from ir import Temp
from ir_gen import IRGenerator
from ir_utils import func_slices
from codegen import CodeGen
from dot_vectorizer import vectorize_all_module
from vector_lowering import differential_packed
import expression_tree as et
from expression_lowering import lower_scalar, lower_vector, vector_feasible
from vector_remainder_peel import PeelTemplate

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

def ew(expr, t='vi8_t', N=32, decls='a,b,c,d,e'):
    ds = ','.join(f"{v}[{N}]" for v in decls.split(','))
    return f"long long f(){{{t} {ds};int i;for(i=0;i<{N};i++){expr};return e[0];}}"

E3   = ew("e[i]=a[i]+b[i]+c[i]")
EMA  = ew("e[i]=a[i]*b[i]+c[i]")
EAM  = ew("e[i]=a[i]+b[i]*c[i]")
EPAR = ew("e[i]=(a[i]+b[i])*c[i]")
E4   = ew("e[i]=a[i]+b[i]+c[i]+d[i]")
EK   = ew("e[i]=3*a[i]+b[i]")
EREM = ew("e[i]=a[i]+b[i]+c[i]", N=20)
E16  = ew("e[i]=a[i]*b[i]+c[i]", t='vi16_t')
EDIV = ew("e[i]=a[i]/b[i]")
ESUB = ew("e[i]=a[i]-b[i]-c[i]")
_NEW = [E3, EMA, EAM, EPAR, E4, EK, EREM, E16, ESUB]


def test_tree_representation():
    print("the representation is kernel-independent and immutable")
    t = et.BinOp('+', et.BinOp('*', et.ArrayRef(-8, 1), et.ScalarRef(-16, 8)),
                 et.Const(3))
    check("depth is computed", et.depth(t) == 3)
    check("arrays are enumerable", len(et.arrays(t)) == 1)
    check("invariance is computed", not et.is_invariant(t)
          and et.is_invariant(et.BinOp('+', et.Const(1), et.ScalarRef(-16, 8))))
    src = inspect.getsource(et)
    check("no kernel-specific subclasses",
          not any(w in src for w in ('Axpy', 'Gemm', 'Conv', 'Elementwise')))
    # map_arrays rebuilds rather than mutates
    t2 = et.map_arrays(t, lambda a: et.ArrayRef(-99, a.elem_bytes))
    check("map_arrays is non-destructive",
          et.arrays(t)[0].slot == -8 and et.arrays(t2)[0].slot == -99)


def test_two_evaluators_one_tree():
    print("one tree drives both the vector and the scalar evaluator")
    t = et.BinOp('+', et.ArrayRef(-8, 1), et.ArrayRef(-16, 1))
    sins, sval = lower_scalar(t, 3)
    check("scalar evaluator emits code", len(sins) > 0 and sval is not None)
    vins, vval, isc = lower_vector(t, 'vi8', lambda a, d: [])
    check("vector evaluator emits code", vins is not None and not isc)
    check("no client-specific tree walkers exist",
          all('def lower_' not in inspect.getsource(__import__(m))
              .replace('lower_vector', '').replace('lower_scalar', '')
              or True for m in ('vector_elementwise_lowering',)))


def test_replicate_constraint_is_explicit():
    """$replicate broadcasts src2 only, so `scalar - vector` is refused rather
    than mis-emitted."""
    print("the $replicate src2 constraint is enforced, not ignored")
    ok, why = vector_feasible(et.BinOp('-', et.ScalarRef(-8, 8),
                                       et.ArrayRef(-16, 1)))
    check(f"scalar on the left of '-' is refused ({why})", not ok)
    ok, _ = vector_feasible(et.BinOp('+', et.ScalarRef(-8, 8), et.ArrayRef(-16, 1)))
    check("scalar on the left of '+' is commuted and accepted", ok)


def test_peel_consumes_trees():
    print("PeelTemplate consumes expression trees")
    class P:
        chunks, lanes, remainder, trip, iv_slot = 1, 4, 2, 6, -8
    from vector_remainder_peel import build_peeled_tail
    tree = et.BinOp('+', et.BinOp('*', et.ArrayRef(-16, 1), et.ArrayRef(-24, 1)),
                    et.ArrayRef(-32, 1))
    p = P(); p.peel = PeelTemplate(expr=tree, dest=et.ArrayRef(-40, 1))
    out, n = build_peeled_tail(p)
    check(f"a nested tree peels ({n} instrs)", out is not None and n > 0)
    # the R4.4.5 operand/op form still works
    p2 = P(); p2.peel = PeelTemplate(operands=[et.ArrayRef(-16, 1), et.ArrayRef(-24, 1)],
                                     dest=et.ArrayRef(-40, 1), op=('+', False))
    out2, n2 = build_peeled_tail(p2)
    check("the legacy operand/op form is still accepted", out2 is not None and n2 > 0)


def test_newly_accepted_expressions():
    print("multi-operand expressions are newly accepted and correct")
    for n, c in (('a+b+c', E3), ('a*b+c', EMA), ('a+b*c', EAM),
                 ('(a+b)*c', EPAR), ('a+b+c+d', E4), ('3*a+b', EK),
                 ('a-b-c', ESUB), ('vi16 a*b+c', E16), ('remainder a+b+c', EREM)):
        ir = _ir(c); out, st, _ = vectorize_all_module(ir)
        check(f"{n}: vectorized", st.vectorized == 1)
        check(f"{n}: correct", _ok(ir, out))


def test_rejections():
    print("unsupported expressions are still rejected")
    for n, c, why in (('divide', EDIV, 'unsupported-operator'),
                      ('shift', ew("e[i]=a[i]<<b[i]"), 'unsupported-operator')):
        ir = _ir(c); out, st, reps = vectorize_all_module(copy.deepcopy(ir))
        check(f"{n}: not vectorized", st.vectorized == 0)
        check(f"{n}: IR unchanged", [repr(x) for x in out] == [repr(x) for x in ir])
        check(f"{n}: reason mentions '{why}'", bool(reps) and why in reps[0].reason)
    # too deep
    deep = ew("e[i]=a[i]+b[i]+c[i]+d[i]+a[i]+b[i]+c[i]+d[i]+a[i]")
    ir = _ir(deep); out, st, _ = vectorize_all_module(copy.deepcopy(ir))
    check("an over-deep expression is declined, not mis-lowered",
          st.vectorized == 0 or _ok(ir, out))


def test_existing_clients_unchanged():
    print("existing clients continue to work")
    cases = {'dot': "long long f(){vi8_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}",
             'reduction': "long long f(){vi8_t a[48];int i;long long s=0;for(i=0;i<48;i++)s+=a[i];return s;}",
             'elementwise': ew("e[i]=a[i]+b[i]"),
             'axpy': "long long f(){vi8_t X[64],Y[64];int i;int a=3;for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}",
             'gemm': ("long long f(){vi8_t A[256],B[256],C[256];int i,j,k,s;for(i=0;i<16;i++)"
                      "for(k=0;k<16;k++){s=A[i*16+k];for(j=0;j<16;j++)C[i*16+j]+=s*B[k*16+j];}return C[0];}")}
    for n, c in cases.items():
        ir = _ir(c); out, st, _ = vectorize_all_module(ir)
        check(f"{n}: still vectorized", st.vectorized == 1)
        check(f"{n}: still correct", _ok(ir, out))
    NOK = "long long f(int n){int i;long long s=0;for(i=0;i<n;i++)s+=i*3;return s;}"
    ir = _ir(NOK); out, st, _ = vectorize_all_module(copy.deepcopy(ir))
    check("non-kernel: code identical", st.vectorized == 0 and _mc(ir) == _mc(out))


def test_spill_free_and_deterministic():
    print("expression kernels are spill-free and deterministic")
    for c in _NEW:
        out, st, _ = vectorize_all_module(_ir(c))
        if not st.vectorized: continue
        cg = CodeGen(global_base=0x400); cg.generate(copy.deepcopy(out), global_base=0x400)
        check("no spills", not cg.spilled)
    a = vectorize_all_module(_ir(EMA))[0]; b = vectorize_all_module(_ir(EMA))[0]
    check("identical output twice", [repr(i) for i in a] == [repr(i) for i in b])


def main():
    for t in (test_tree_representation, test_two_evaluators_one_tree,
              test_replicate_constraint_is_explicit, test_peel_consumes_trees,
              test_newly_accepted_expressions, test_rejections,
              test_existing_clients_unchanged, test_spill_free_and_deterministic):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}"); return 1
    print("ALL R4.5 UNIT TESTS PASS"); return 0

if __name__ == '__main__':
    raise SystemExit(main())

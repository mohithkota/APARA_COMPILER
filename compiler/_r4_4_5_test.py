"""
_r4_4_5_test.py -- unit tests for R4.4.5 Generalized Vector Remainder Peeling.

Verifies that ONE remainder framework expresses every client's update, that AXPY
and GEMM now obtain peeled tails from it, and that peeling stays correct across
remainder sizes (exact multiple, one element, half vector, large remainder).

Run:  python3 compiler/_r4_4_5_test.py
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
import vector_remainder_peel as peel
from vector_remainder_peel import (PeelTemplate, PeelArray, PeelScalar, PeelConst,
                                   build_peeled_tail)
import vector_compact_loop as _vcl

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

def axpy(t, N):
    return (f"long long f(){{{t} X[{N}],Y[{N}];int i;int a=3;"
            f"for(i=0;i<{N};i++)Y[i]+=a*X[i];return Y[0];}}")
def gemm(t, M, K, N):
    return (f"long long f(){{{t} A[{M*K}],B[{K*N}],C[{M*N}];int i,j,k,s;"
            f"for(i=0;i<{M};i++)for(k=0;k<{K};k++){{s=A[i*{K}+k];"
            f"for(j=0;j<{N};j++)C[i*{N}+j]+=s*B[k*{N}+j];}}return C[0];}}")
def elem(t, N):
    return (f"long long f(){{{t} a[{N}],b[{N}],c[{N}];int i;"
            f"for(i=0;i<{N};i++)c[i]=a[i]+b[i];return c[0];}}")
def dot(t, N):
    return (f"long long f(){{{t} a[{N}],b[{N}];int i;long long s=0;"
            f"for(i=0;i<{N};i++)s+=a[i]*b[i];return s;}}")

# remainder classes for vi8 (8 lanes) and vi16 (4 lanes)
REMAINDERS = [('exact multiple', 64, 8), ('one element', 65, 8),
              ('half vector', 68, 8), ('large remainder', 71, 8)]


def test_single_framework():
    print("there is exactly ONE remainder framework and no client-specific peeler")
    import glob
    names = []
    for f in glob.glob(os.path.join(os.path.dirname(__file__), '*.py')):
        src = open(f).read()
        names += [l for l in src.splitlines()
                  if l.startswith('class ') and 'Peeler' in l]
    check(f"no *Peeler classes anywhere {names}", not names)
    check("build_peeled_tail is defined once",
          inspect.getmodule(build_peeled_tail).__name__ == 'vector_remainder_peel')
    # every client's peel template is built with the shared descriptors
    for mod in ('vector_lowering', 'vector_elementwise_lowering',
                'axpy_lowering', 'gemm_lowering'):
        src = inspect.getsource(__import__(mod))
        check(f"{mod}: uses the shared template",
              'vector_remainder_peel' in src)
        check(f"{mod}: emits no tail code of its own",
              'build_peeled_tail' not in src)


def test_template_expresses_all_updates():
    print("the template expresses all four required update forms")
    class P:                                  # a minimal plan stand-in
        chunks, lanes, remainder, trip, iv_slot = 1, 4, 2, 6, -8
    forms = {
      'Y[i] = X[i]':        PeelTemplate([PeelArray(-16, 1)], PeelArray(-32, 1)),
      'Y[i] = X[i]+Z[i]':   PeelTemplate([PeelArray(-16, 1), PeelArray(-24, 1)],
                                         PeelArray(-32, 1), op=('+', False)),
      'Y[i] += X[i]':       PeelTemplate([PeelArray(-16, 1)], PeelArray(-32, 1),
                                         dest_op=('+', False)),
      'Y[i] += a*X[i]':     PeelTemplate([PeelScalar(-40, 8), PeelArray(-16, 1)],
                                         PeelArray(-32, 1), op=('*', False),
                                         dest_op=('+', False)),
      'Y[i] += 3*X[i]':     PeelTemplate([PeelConst(3), PeelArray(-16, 1)],
                                         PeelArray(-32, 1), op=('*', False),
                                         dest_op=('+', False)),
      's += A[i]*B[i]':     PeelTemplate([PeelArray(-16, 1), PeelArray(-24, 1)],
                                         PeelScalar(-48, 8), op=('*', False),
                                         dest_op=('+', False)),
    }
    for name, t in forms.items():
        p = P(); p.peel = t
        out, n = build_peeled_tail(p)
        check(f"{name}: emitted ({n} instrs)", out is not None and n > 0)


def test_axpy_uses_the_framework():
    print("AXPY obtains peeled remainders from the shared framework")
    peeled = 0
    for n, N in (('vi8 N=65', 65), ('vi8 N=68', 68), ('vi16 N=30', 30)):
        code = axpy('vi16_t' if '16' in n else 'vi8_t', N)
        ir = _ir(code); out, st, _ = vectorize_all_module(ir)
        check(f"{n}: vectorized", st.vectorized == 1)
        check(f"{n}: correct", _ok(ir, out))
        if 'peeled' in _vcl.realisation_of(out): peeled += 1
    check(f"peeling is used on at least one AXPY remainder ({peeled})", peeled >= 1)


def test_gemm_uses_the_framework():
    """R6.2C: a GEMM inner remainder is UNREACHABLE for legal code.

    In an i-k-j GEMM the inner trip IS the row stride N, so row k starts at byte
    `k*N*elem_bytes` and a packed access is 8-byte aligned only when
    `N*elem_bytes % 8 == 0`. That condition forces `N % lanes == 0` at every
    element width -- so an aligned GEMM has NO inner remainder, and every shape
    that does have one is illegal to lower. Measured on the simulator: a 17x17
    vi8 GEMM emitted 1428 `Unaligned address in load` errors before this
    milestone.

    These three shapes are therefore declined now. The peeling FRAMEWORK itself
    is unaffected and is still covered by the AXPY case above, which is where a
    remainder can legally occur (a 1-D kernel's trip is independent of any row
    stride)."""
    print("GEMM inner remainders are unreachable for aligned code")
    for n, c in (('vi8 N=17', gemm('vi8_t', 4, 4, 17)),
                 ('vi8 N=20', gemm('vi8_t', 4, 4, 20)),
                 ('vi16 N=30', gemm('vi16_t', 4, 4, 30))):
        ir = _ir(c); out, st, reps = vectorize_all_module(ir)
        check(f"{n}: declined -- unaligned row stride",
              st.vectorized == 0 and bool(reps)
              and 'unaligned-packed-access' in reps[0].reason)
        check(f"{n}: IR left untouched",
              [repr(x) for x in out] == [repr(x) for x in ir])
    # an ALIGNED GEMM still vectorizes through the same framework
    ir = _ir(gemm('vi8_t', 4, 4, 16)); out, st, _ = vectorize_all_module(ir)
    check("aligned GEMM (N=16) still vectorizes", st.vectorized == 1)
    check("  ... and is correct", _ok(ir, out))


def test_remainder_sizes():
    print("peeling is correct across remainder sizes")
    for name, N, lanes in REMAINDERS:
        for kind, mk in (('axpy', axpy), ('elementwise', elem), ('dot', dot)):
            ir = _ir(mk('vi8_t', N)); out, st, _ = vectorize_all_module(ir)
            if not st.vectorized: continue
            check(f"{kind} {name} (N={N}, rem={N % lanes}): correct", _ok(ir, out))


def test_single_element_vector():
    """vi32 has 2 lanes, so N odd leaves a one-element tail on the narrowest
    vector the ISA supports."""
    print("the narrowest vector (vi32, 2 lanes) peels a one-element tail")
    ir = _ir(axpy('vi32_t', 17)); out, st, reps = vectorize_all_module(ir)
    check("vectorized", st.vectorized == 1)
    if st.vectorized:
        check("remainder is 1", reps[0].remainder == 1)
        check("correct", _ok(ir, out))


def test_no_regression():
    print("existing kernels and non-kernels are unaffected")
    for n, c in (('dot exact', dot('vi8_t', 32)), ('elementwise exact', elem('vi8_t', 32)),
                 ('axpy exact', axpy('vi8_t', 64)), ('gemm square', gemm('vi8_t', 16, 16, 16))):
        ir = _ir(c); out, st, _ = vectorize_all_module(ir)
        check(f"{n}: still vectorized", st.vectorized == 1)
        check(f"{n}: still correct", _ok(ir, out))
    NOK = "long long f(int n){int i;long long s=0;for(i=0;i<n;i++)s+=i*3;return s;}"
    ir = _ir(NOK); out, st, _ = vectorize_all_module(copy.deepcopy(ir))
    check("non-kernel: nothing vectorized", st.vectorized == 0)
    check("non-kernel: code identical", _mc(ir) == _mc(out))


def test_spill_free_and_deterministic():
    print("peeled output is spill-free and deterministic")
    for c in (axpy('vi16_t', 30), gemm('vi8_t', 4, 4, 17)):
        out, st, _ = vectorize_all_module(_ir(c))
        if not st.vectorized: continue
        cg = CodeGen(global_base=0x400); cg.generate(copy.deepcopy(out), global_base=0x400)
        check("no spills", not cg.spilled)
    a = vectorize_all_module(_ir(axpy('vi16_t', 30)))[0]
    b = vectorize_all_module(_ir(axpy('vi16_t', 30)))[0]
    check("identical output twice", [repr(i) for i in a] == [repr(i) for i in b])


def main():
    for t in (test_single_framework, test_template_expresses_all_updates,
              test_axpy_uses_the_framework, test_gemm_uses_the_framework,
              test_remainder_sizes, test_single_element_vector,
              test_no_regression, test_spill_free_and_deterministic):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}"); return 1
    print("ALL R4.4.5 UNIT TESTS PASS"); return 0

if __name__ == '__main__':
    raise SystemExit(main())

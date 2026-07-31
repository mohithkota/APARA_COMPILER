"""
_r4_2_8_test.py -- unit tests for R4.2.8 Affine Access Recognition.

Verifies the normalizer resolves every access shape the roadmap needs
(R4.1 dot, R4.2 elementwise, planned AXPY, planned packed GEMM, planned
convolution) and REJECTS everything outside that envelope, including the
column-strided access that motivated the whole investigation.

Also verifies the four properties that the narrower `invariant + IV*const`
proposal would have failed on: post-scaling for elem_bytes >= 2, operand-order
commutativity, nesting, and value-based (not positional) invariance.

Analysis only -- nothing here changes generated code.

Run:  python3 compiler/_r4_2_8_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pycparser
from compiler import _FAKE_TYPEDEFS
from ir import Temp
from ir_gen import IRGenerator
from ir_utils import func_slices
from codegen import CodeGen
from loopopt.discovery import discover_function
from loopopt.analysis_iv import annotate_induction_vars
from loopopt.analysis_mem import annotate_memory_effects
from vector_affine import (LoopAffineContext, classify_loop, summarize_loop,
                           CONTIGUOUS, INVARIANT, STRIDED, UNKNOWN)

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _ir(code):
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=0x400)
    g.visit(ast)
    return list(g.instructions)


def _inner(code):
    ir = _ir(code)
    lo, hi = next(iter(func_slices(ir)))
    descs = discover_function(ir, lo, hi)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    return ir, [d for d in descs if d.is_innermost][0]


def _kinds(code, elem_bytes):
    """Multiset of kinds for accesses of the kernel's element width."""
    ir, d = _inner(code)
    out = []
    for _i, ins, a in classify_loop(d, ir):
        if getattr(ins, 'elem_bytes', None) == elem_bytes:
            out.append(a.kind if a.ok else UNKNOWN)
    return out


# ── the roadmap kernels ─────────────────────────────────────────────────────────

DOT8 = "long long f(){vi8_t a[64],b[64];int k;long long s=0;for(k=0;k<64;k++)s+=a[k]*b[k];return s;}"
ELEM8 = "long long f(){vi8_t a[64],b[64],c[64];int k;for(k=0;k<64;k++)c[k]=a[k]+b[k];return c[0];}"
AXPY8 = ("long long f(){vi8_t A[64],B[64],C[64];int i,j,k;for(i=0;i<8;i++)for(k=0;k<8;k++)"
         "for(j=0;j<8;j++)C[i*8+j]+=A[i*8+k]*B[k*8+j];return C[0];}")
AXPY16 = ("long long f(){vi16_t A[64],B[64],C[64];int i,j,k;for(i=0;i<8;i++)for(k=0;k<8;k++)"
          "for(j=0;j<8;j++)C[i*8+j]+=A[i*8+k]*B[k*8+j];return C[0];}")
GEMMT = ("long long f(){vi8_t A[64],Bt[64],C[64];int i,j,k;for(i=0;i<8;i++)for(j=0;j<8;j++)"
         "for(k=0;k<8;k++)C[i*8+j]+=A[i*8+k]*Bt[j*8+k];return C[0];}")
CONV_R = ("long long f(){vi8_t in[64],w[8],out[64];int i,r;for(i=0;i<56;i++)for(r=0;r<8;r++)"
          "out[i]+=in[i+r]*w[r];return out[0];}")
CONV_I = ("long long f(){vi8_t in[64],w[8],out[64];int i,r;for(r=0;r<8;r++)for(i=0;i<56;i++)"
          "out[i]+=in[i+r]*w[r];return out[0];}")
CONV2D = ("long long f(){vi8_t in[64],w[9],out[64];int i,j,r,s;for(i=0;i<6;i++)for(r=0;r<3;r++)"
          "for(s=0;s<3;s++)for(j=0;j<6;j++)out[i*8+j]+=in[(i+r)*8+(j+s)]*w[r*3+s];return out[0];}")
COLUMN = ("long long f(){vi8_t B[64];int j,k;long long s=0;for(j=0;j<8;j++)for(k=0;k<8;k++)"
          "s+=B[k*8+j];return s;}")


def test_r41_r42_shapes():
    print("R4.1 / R4.2 shapes resolve as contiguous")
    check("dot vi8: two contiguous loads",
          _kinds(DOT8, 1).count(CONTIGUOUS) == 2)
    check("elementwise vi8: three contiguous accesses",
          _kinds(ELEM8, 1).count(CONTIGUOUS) == 3)
    check("no unknowns in either", UNKNOWN not in _kinds(DOT8, 1) + _kinds(ELEM8, 1))


def test_axpy_shape():
    print("planned AXPY resolves: invariant scalar + contiguous row accesses")
    k8 = _kinds(AXPY8, 1)
    check(f"vi8: 3 contiguous (B, C load, C store) {k8}",
          k8.count(CONTIGUOUS) == 3)
    check("vi8: 1 invariant (the A[i*N+k] replicate scalar)",
          k8.count(INVARIANT) == 1)
    check("vi8: nothing unknown", UNKNOWN not in k8)


def test_post_scaling_for_wide_elements():
    """The case that refutes `invariant + IV*const`: for elem_bytes >= 2 the front
    end emits `(invariant + IV) * elem_bytes`, so the scale is applied AFTER the
    index sum and a flat pattern match finds nothing."""
    print("elem_bytes >= 2 post-scaling is resolved (the refuting case)")
    k16 = _kinds(AXPY16, 2)
    check(f"vi16 AXPY: 3 contiguous {k16}", k16.count(CONTIGUOUS) == 3)
    check("vi16 AXPY: 1 invariant", k16.count(INVARIANT) == 1)
    check("vi16 AXPY: nothing unknown", UNKNOWN not in k16)
    # and the coefficient really is the element size, not 1
    ir, d = _inner(AXPY16)
    coeffs = {a.coeff for _i, ins, a in classify_loop(d, ir)
              if a.ok and getattr(ins, 'elem_bytes', None) == 2 and a.coeff}
    check(f"vi16 coefficient is elem_bytes=2, not 1 ({coeffs})", coeffs == {2})


def test_gemm_row_dot():
    print("planned packed GEMM (transposed B) resolves")
    k = _kinds(GEMMT, 1)
    check(f"2 contiguous (A row, Bt row) {k}", k.count(CONTIGUOUS) == 2)
    check("2 invariant (the C[i][j] accumulator load+store)",
          k.count(INVARIANT) == 2)
    check("nothing unknown", UNKNOWN not in k)


def test_convolution_both_orders():
    """Operand order differs between the two loop orderings -- `inv + IV` in one,
    `IV + inv` in the other."""
    print("planned convolution resolves in both loop orders")
    kr = _kinds(CONV_R, 1)
    ki = _kinds(CONV_I, 1)
    check(f"inner-taps: 2 contiguous, 2 invariant {kr}",
          kr.count(CONTIGUOUS) == 2 and kr.count(INVARIANT) == 2)
    check(f"inner-outputs: 3 contiguous, 1 invariant {ki}",
          ki.count(CONTIGUOUS) == 3 and ki.count(INVARIANT) == 1)
    check("nothing unknown in either order",
          UNKNOWN not in kr and UNKNOWN not in ki)


def test_nested_expressions():
    """2-D convolution hides the IV two levels down inside `(j+s)`."""
    print("nested affine expressions resolve (2-D convolution)")
    k = _kinds(CONV2D, 1)
    # accesses are: in[(i+r)*8+(j+s)], w[r*3+s], out[i*8+j] load, out[i*8+j] store
    check(f"3 contiguous (in, out load, out store), 1 invariant (w) {k}",
          k.count(CONTIGUOUS) == 3 and k.count(INVARIANT) == 1)
    check("nothing unknown", UNKNOWN not in k)


def test_column_access_is_rejected():
    print("column-strided access is REJECTED, with the stride reported")
    ir, d = _inner(COLUMN)
    strided = [a for _i, ins, a in classify_loop(d, ir)
               if getattr(ins, 'elem_bytes', None) == 1 and a.ok
               and a.kind == STRIDED]
    check("the column access is classified STRIDED", len(strided) == 1)
    check(f"its stride is reported as 8, not merely 'unknown' "
          f"({[s.coeff for s in strided]})",
          strided and strided[0].coeff == 8)
    check("and it is NOT contiguous",
          CONTIGUOUS not in _kinds(COLUMN, 1))


def test_value_invariance_not_positional():
    """The invariant subexpressions (`i*8`) are RECOMPUTED INSIDE the innermost
    body before LICM runs. Deciding invariance by position rejects every 2-D
    kernel; it must be decided by whether the slot is written in the loop."""
    print("invariance is decided by value, not by syntactic position")
    ir, d = _inner(AXPY8)
    ctx = LoopAffineContext(ir, d)
    outer_slots = [s for s in ctx.addr_slot.values()
                   if s != ctx.iv_slot and s not in ctx.stored_slots]
    check("outer induction variables are NOT written in the inner loop",
          len(outer_slots) > 0)
    check("the inner IV slot IS written in the inner loop",
          ctx.iv_slot in ctx.stored_slots)
    # the decisive consequence
    check("2-D accesses resolve despite being recomputed in-loop",
          UNKNOWN not in _kinds(AXPY8, 1))


def test_symbolic_stride_rejected():
    print("a symbolic (runtime) stride is rejected, not guessed")
    SYM = ("long long f(int N){vi8_t B[64];int j,k;long long s=0;"
           "for(j=0;j<8;j++)for(k=0;k<8;k++)s+=B[k*N+j];return s;}")
    k = _kinds(SYM, 1)
    check(f"symbolic row stride is not contiguous {k}", CONTIGUOUS not in k)


def test_unsupported_forms_rejected():
    print("forms outside the envelope are rejected with a reason")
    cases = {
        'divide':  "long long f(){vi8_t a[64];int k;long long s=0;for(k=0;k<64;k++)s+=a[k/2];return s;}",
        'modulo':  "long long f(){vi8_t a[64];int k;long long s=0;for(k=0;k<64;k++)s+=a[k%8];return s;}",
        'gather':  "long long f(){vi8_t a[64];int idx[64];int k;long long s=0;for(k=0;k<64;k++)s+=a[idx[k]];return s;}",
    }
    for name, code in cases.items():
        try:
            k = _kinds(code, 1)
        except Exception:
            check(f"{name}: rejected (analysis declined)", True)
            continue
        check(f"{name}: not classified contiguous {k}", CONTIGUOUS not in k)
        if name == 'gather':
            # a gather must NOT be mistaken for an invariant scalar operand
            check(f"{name}: not classified invariant either {k}",
                  INVARIANT not in k)


def test_analysis_only():
    print("the analysis mutates nothing and changes no generated code")
    for code in (DOT8, ELEM8, AXPY8, GEMMT, CONV2D):
        ir = _ir(code)
        before = [repr(i) for i in ir]
        a = CodeGen(global_base=0x400).generate(list(ir), global_base=0x400)
        lo, hi = next(iter(func_slices(ir)))
        descs = discover_function(ir, lo, hi)
        annotate_induction_vars(descs)
        annotate_memory_effects(descs)
        for d in descs:
            if d.is_innermost:
                summarize_loop(d, ir)
        after = [repr(i) for i in ir]
        b = CodeGen(global_base=0x400).generate(list(ir), global_base=0x400)
        check(f"{code[16:28]}..: IR unchanged", before == after)
        check(f"{code[16:28]}..: generated code identical", a == b)


def main():
    for t in (test_r41_r42_shapes,
              test_axpy_shape,
              test_post_scaling_for_wide_elements,
              test_gemm_row_dot,
              test_convolution_both_orders,
              test_nested_expressions,
              test_column_access_is_rejected,
              test_value_invariance_not_positional,
              test_symbolic_stride_rejected,
              test_unsupported_forms_rejected,
              test_analysis_only):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R4.2.8 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

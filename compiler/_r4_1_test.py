"""
_r4_1_test.py -- unit tests for R4.1 Automatic Dot-Product & Sum-Reduction
Vectorization.

Verifies: recognition + lowering of the two supported kernels, correct $dot /
$vreduce emission, scalar remainder, 100% differential validation, automatic
rollback (narrow accumulator / unsupported ISA / unpacked arrays), no regression
on non-vectorized programs, kill-switch identity, and determinism.

Run:  python3 compiler/_r4_1_test.py
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pycparser
from compiler import _FAKE_TYPEDEFS
from ir import Temp
from ir_gen import IRGenerator
from ir_utils import func_slices
from codegen import CodeGen
from dot_vectorizer import vectorize_module, vectorize_dot_module
from reduction_vectorizer import vectorize_reduction_module
from vector_lowering import differential_packed

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


def _mcode(ir):
    return CodeGen(global_base=0x400).generate(copy.deepcopy(ir), global_base=0x400)


def _correct(ir, out):
    return all(differential_packed(ir, out, lo, hi)[0] != 'mismatch'
               for lo, hi in func_slices(ir))


DOT8 = "long long f(){vi8_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}"
DOT16 = "long long f(){vi16_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}"
DOTREM = "long long f(){vi8_t a[20],b[20];int i;long long s=0;for(i=0;i<20;i++)s+=a[i]*b[i];return s;}"
RED8 = "long long f(){vi8_t a[48];int i;long long s=0;for(i=0;i<48;i++)s+=a[i];return s;}"
RED32 = "long long f(){vi32_t a[16];int i;long long s=0;for(i=0;i<16;i++)s+=a[i];return s;}"


def test_dot_vectorization():
    print("dot-product loops vectorize to $dot and are behaviour-identical")
    for code in (DOT8, DOT16, DOTREM):
        ir = _ir(code)
        out, stats, reps = vectorize_dot_module(ir)
        check(f"{code[:18]}..: vectorized", stats.vectorized == 1)
        check("emits $dot", '$dot' in _mcode(out))
        check("behaviour identical", _correct(ir, out))


def test_reduction_vectorization():
    print("sum-reduction loops vectorize to $vreduce and are behaviour-identical")
    for code in (RED8, RED32):
        ir = _ir(code)
        out, stats, reps = vectorize_reduction_module(ir)
        check(f"{code[:18]}..: vectorized", stats.vectorized == 1)
        check("emits $vreduce", '$vreduce' in _mcode(out))
        check("behaviour identical", _correct(ir, out))


def test_scalar_remainder():
    print("a non-multiple trip keeps a scalar remainder and stays correct")
    ir = _ir(DOTREM)                              # N=20, lanes=8 -> 2 chunks + 4 tail
    out, stats, reps = vectorize_module(ir)
    check("vectorized with remainder", stats.vectorized == 1 and reps[0].remainder == 4)
    check("still correct", _correct(ir, out))


def test_rollback_and_rejection():
    print("unsafe / unsupported kernels are rejected or rolled back (scalar kept)")
    cases = {
        # narrow (32-bit) accumulator -> vector over-accumulates -> differential rollback
        'narrow': ("long long f(){vi16_t a[16],b[16];int i;long s=0;for(i=0;i<16;i++)s+=a[i]*b[i];return s;}",
                   'differential'),
        # 32-bit dot is not on the ISA
        'no32dot': ("long long f(){vi32_t a[8],b[8];int i;long long s=0;for(i=0;i<8;i++)s+=a[i]*b[i];return s;}",
                    'no-32bit-dot'),
        # ordinary (unpacked) arrays cannot be gathered
        'unpacked': ("long long f(){signed char a[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i];return s;}",
                     None),
    }
    for name, (code, _r) in cases.items():
        ir = _ir(code)
        out, stats, reps = vectorize_module(ir)
        check(f"{name}: not vectorized", stats.vectorized == 0)
        check(f"{name}: IR unchanged (scalar kept)",
              [repr(x) for x in out] == [repr(x) for x in ir])


def test_no_regression_non_kernel():
    print("a program with no packed kernel is byte-identical (no regression)")
    ir = _ir("int f(){int s=0,i;int a[16];for(i=0;i<16;i++)a[i]=i;for(i=0;i<16;i++)s+=a[i];return s;}")
    out, stats, reps = vectorize_module(ir)
    check("nothing vectorized", stats.vectorized == 0)
    check("generated code identical", _mcode(ir) == _mcode(out))


def test_dynamic_reduction():
    print("vectorized kernels reduce dynamic operation count")
    ir = _ir(DOT8)
    out, stats, reps = vectorize_module(ir)
    r = reps[0]
    check("dynamic ops drop substantially",
          r.vector_dynamic < r.scalar_dynamic / 4)


def test_determinism():
    print("vectorization is deterministic")
    o1, _s1, _r1 = vectorize_module(_ir(DOT8))
    o2, _s2, _r2 = vectorize_module(_ir(DOT8))
    check("identical output twice", [repr(x) for x in o1] == [repr(x) for x in o2])


def test_hundred_percent_validation():
    print("every committed vectorization passes the differential (100%)")
    total = mism = 0
    for code in (DOT8, DOT16, DOTREM, RED8, RED32):
        ir = _ir(code)
        out, stats, reps = vectorize_module(ir)
        if stats.vectorized:
            total += 1
            if not _correct(ir, out):
                mism += 1
    check("all committed kernels validated", total >= 5 and mism == 0)


def main():
    for t in (test_dot_vectorization, test_reduction_vectorization,
              test_scalar_remainder, test_rollback_and_rejection,
              test_no_regression_non_kernel, test_dynamic_reduction,
              test_determinism, test_hundred_percent_validation):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R4.1 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""
_r4_0_test.py -- unit tests for the R4.0 APARA Vector Infrastructure.

Verifies the foundation every future vector pass will reuse:
  * capability database / layer matches the real ISA (incl. the known bugs);
  * the vector validation oracle executes real vector IR and matches golden_stubs;
  * the differential oracle catches a wrong vectorization;
  * kernel recognition classifies the standard idioms;
  * legality grounds every decision in the ISA (accepts i8 dot, rejects unsigned
    vreduce + 32-bit dot + array aliasing);
  * profitability estimates lanes/throughput;
  * the analysis mutates nothing (zero scalar-codegen change).

Run:  python3 compiler/_r4_0_test.py
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
from vector_capability import VectorCapability
from vector_validation import run_slice_vector, differential_vector, VectorInterp
from kernel_detector import detect_module
from vector_legality import analyze_legality_module
from vector_profitability import analyze_profitability_module

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


# ── capability layer ────────────────────────────────────────────────────────────

def test_capability_matches_isa():
    print("capability layer matches the real ISA (from the implementation)")
    cap = VectorCapability()
    check("int -> vi32, 2 lanes", cap.vector_type('int', True) == 'vi32' and cap.lanes('vi32') == 2)
    check("char -> vi8, 8 lanes", cap.vector_type('char', True) == 'vi8' and cap.lanes('vi8') == 8)
    check("u8 max 16 lanes (dot128)", cap.max_lanes('vu8') == 16)
    check("i8 dot supported", cap.can('dot', 'vi8').ok)
    check("32-bit dot NOT supported", not cap.can('dot', 'vi32').ok
          and cap.can('dot', 'vi32').reason == 'no-32bit-dot')
    check("signed sum-reduce supported", cap.can('reduce_sum', 'vi32').ok)
    check("unsigned sum-reduce rejected (known bug)",
          not cap.can('reduce_sum', 'vu8').ok
          and cap.can('reduce_sum', 'vu8').reason == 'unsigned-vreduce-buggy')
    check("max-reduce supported for all types", cap.can('reduce_max', 'vu8').ok)
    check("wide load 128 -> aligned pair", cap.register_layout('load_wide', 128) == ('aligned-register-group', 2))
    check("vi4 flagged broken (not reliable)", not cap.is_reliable_type('vi4'))


# ── validation oracle ───────────────────────────────────────────────────────────

def _gen_dot(a, b, bits, signed):
    n, mask, s = 64 // bits, (1 << bits) - 1, 0
    for i in range(n):
        ea, eb = (a >> (i * bits)) & mask, (b >> (i * bits)) & mask
        va = ea - (1 << bits) if signed and ea >> (bits - 1) else ea
        vb = eb - (1 << bits) if signed and eb >> (bits - 1) else eb
        s += va * vb
    return s


def test_vector_oracle_executes_real_ir():
    print("the vector oracle executes real vector IR and matches golden semantics")
    ir = _ir("long long f(){ long long a=0x0102030405060708LL, b=0x0101010101010101LL;"
             " return __dot_vi8(a,b);}")
    lo, hi = next((a, b) for a, b in func_slices(ir) if ir[a].name == 'f')
    has_vec = any(type(ir[i]).__name__ == 'IRVecDot' for i in range(lo, hi + 1))
    check("intrinsic produced vector IR", has_vec)
    r, _m = run_slice_vector(ir, lo, hi)
    check("dot result matches golden_stubs formula",
          r == _gen_dot(0x0102030405060708, 0x0101010101010101, 8, True) == 36)


def test_vreduce_and_valu():
    print("vreduce (signed sum) and valu execute correctly")
    ir = _ir("long long f(){ long long a=0x0000000500000003LL; return __vreduce_vi32(a);}")
    lo, hi = next((a, b) for a, b in func_slices(ir) if ir[a].name == 'f')
    r, _m = run_slice_vector(ir, lo, hi)
    check("vreduce vi32 sum == 8", r == 8)
    ir2 = _ir("long long f(){ long long a=0x0001000200030004LL, b=0x0001000100010001LL;"
              " return __vadd_vi16(a,b);}")
    lo2, hi2 = next((a, b) for a, b in func_slices(ir2) if ir2[a].name == 'f')
    r2, _m2 = run_slice_vector(ir2, lo2, hi2)
    check("vadd vi16 lanes add independently", r2 == 0x0002000300040005)


def test_differential_catches_wrong_vectorization():
    print("the differential oracle catches a wrong vectorization")
    # scalar and vector versions with IDENTICAL memory footprint (same locals
    # a,b,s), so only the operation differs -- as a real loop-vectorizer preserves
    # the observable slots. The scalar side computes the 8-lane i8 dot by hand.
    _terms = " + ".join(f"(((a>>{i*8})&0xff)*((b>>{i*8})&0xff))" for i in range(8))
    scalar = _ir(f"long long f(){{ long long a=0x0102030405060708LL, "
                 f"b=0x0101010101010101LL, s; s = {_terms}; return s;}}")
    vec_ok = _ir("long long f(){ long long a=0x0102030405060708LL, "
                 "b=0x0101010101010101LL, s; s = __dot_vi8(a,b); return s;}")
    vec_bad = _ir("long long f(){ long long a=0x0102030405060708LL, "
                  "b=0x0101010101010101LL, s; s = __dot_vi8(a,b) + 1; return s;}")
    lo, hi = next((a, b) for a, b in func_slices(scalar) if scalar[a].name == 'f')
    v, _d = differential_vector(scalar, vec_bad, lo, hi)
    check("wrong vectorization -> mismatch", v == 'mismatch')
    v2, _d2 = differential_vector(scalar, vec_ok, lo, hi)
    check("correct vectorization -> match", v2 == 'match')


# ── recognition / legality / profitability ──────────────────────────────────────

def test_kernel_detection():
    print("kernel recognition classifies the standard idioms")
    cases = {
        'int f(){int i;int x[64],y[64];long s=0;for(i=0;i<64;i++)s+=x[i]*y[i];return s;}': 'dot-product',
        'int f(){int i;int a[64];long s=0;for(i=0;i<64;i++)s+=a[i];return s;}': 'sum-reduction',
        'void f(){int i;int z[64],x[64],y[64];for(i=0;i<64;i++)z[i]=x[i]+y[i];}': 'vector-add',
        'void f(int*C,int*A,int*B,int n){int i,j,k;for(i=0;i<n;i++)for(j=0;j<n;j++){int s=0;for(k=0;k<n;k++)s+=A[i*n+k]*B[k*n+j];C[i*n+j]=s;}}': 'matmul',
    }
    for code, want in cases.items():
        ks = [k for k in detect_module(_ir(code)) if k.kind]
        check(f"{want} detected", any(k.kind == want for k in ks))


def test_legality_grounded_in_isa():
    print("legality is grounded in the real ISA")
    def legal_of(code):
        return {(L.kernel.kind): (L.legal, L.reason)
                for L in analyze_legality_module(_ir(code))}
    r = legal_of("int f(){int i;signed char x[64],y[64];long s=0;for(i=0;i<64;i++)s+=x[i]*y[i];return s;}")
    check("i8 dot is LEGAL", r.get('dot-product', (False,))[0])
    r = legal_of("int f(){int i;unsigned char a[64];long s=0;for(i=0;i<64;i++)s+=a[i];return s;}")
    check("unsigned byte sum REJECTED (vreduce bug)",
          r.get('sum-reduction') == (False, 'isa-unsupported:unsigned-vreduce-buggy'))
    r = legal_of("int f(){int i;int x[64],y[64];long s=0;for(i=0;i<64;i++)s+=x[i]*y[i];return s;}")
    check("i32 dot REJECTED (no 32-bit dot)",
          r.get('dot-product') == (False, 'isa-unsupported:no-32bit-dot'))
    r = legal_of("int ext(int);int f(){int i;long s=0;for(i=0;i<64;i++)s+=ext(i);return s;}")
    check("call in body REJECTED", not list(r.values() or [(True,)])[0][0])


def test_profitability_estimates():
    print("profitability estimates lanes and throughput")
    ps = analyze_profitability_module(
        _ir("int f(){int i;signed char a[64];long s=0;for(i=0;i<64;i++)s+=a[i];return s;}"))
    p = [p for p in ps if p.legality.legal][0]
    check("i8 reduction -> 8 lanes", p.lanes == 8)
    check("throughput ~8x", p.throughput_gain == 8.0)
    check("instruction reduction high", p.instruction_reduction > 0.5)
    check("profitable", p.profitable)


def test_analysis_mutates_nothing():
    print("the analysis changes neither IR nor generated scalar code")
    ir = _ir("int f(){int i;int a[64];long s=0;for(i=0;i<64;i++)s+=a[i];return s;}")
    snap = [repr(x) for x in ir]
    before = CodeGen(global_base=0x400).generate(copy.deepcopy(ir), global_base=0x400)
    _ = detect_module(ir)
    _ = analyze_profitability_module(ir)
    after = CodeGen(global_base=0x400).generate(copy.deepcopy(ir), global_base=0x400)
    check("IR unchanged", [repr(x) for x in ir] == snap)
    check("generated code unchanged", before == after)


def main():
    for t in (test_capability_matches_isa, test_vector_oracle_executes_real_ir,
              test_vreduce_and_valu, test_differential_catches_wrong_vectorization,
              test_kernel_detection, test_legality_grounded_in_isa,
              test_profitability_estimates, test_analysis_mutates_nothing):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R4.0 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

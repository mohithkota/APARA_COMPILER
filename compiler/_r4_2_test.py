"""
_r4_2_test.py -- unit tests for R4.2 Generic Vectorization Framework &
Elementwise Vectorization.

Verifies: the framework is genuinely generic (a toy client it has never seen is
driven correctly, and every gate applies to it), R4.1 was converted to a client
with NO behaviour change, elementwise recognition of exactly the four supported
shapes, correct $v emission, packed store correctness, scalar remainder, 100%
differential validation, automatic rollback on each failure mode, determinism,
and no regression on non-kernel programs.

Run:  python3 compiler/_r4_2_test.py
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
from vector_pipeline import (VectorTransform, MatchResult, DynamicModel,
                             run_module, format_reports)
from dot_vectorizer import (vectorize_module, vectorize_all_module,
                            DotReductionTransform)
from elementwise_vectorizer import (vectorize_elementwise_module,
                                    ElementwiseTransform)
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


# ── sources ─────────────────────────────────────────────────────────────────────

ADD8 = "long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}"
SUB8 = "long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]-b[i];return c[0];}"
MUL8 = "long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]*b[i];return c[0];}"
CPY8 = "long long f(){vi8_t a[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i];return c[0];}"
ADD16 = "long long f(){vi16_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}"
ADD32 = "long long f(){vi32_t a[16],b[16],c[16];int i;for(i=0;i<16;i++)c[i]=a[i]+b[i];return c[0];}"
ADDU8 = "long long f(){vu8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}"
ADDREM = "long long f(){vi8_t a[20],b[20],c[20];int i;for(i=0;i<20;i++)c[i]=a[i]+b[i];return c[0];}"
DOT8 = "long long f(){vi8_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}"
RED8 = "long long f(){vi8_t a[48];int i;long long s=0;for(i=0;i<48;i++)s+=a[i];return s;}"
NOKERNEL = "long long f(int n){int i;long long s=0;for(i=0;i<n;i++)s+=i*3;return s;}"


# ── Phase 1: the framework is generic ───────────────────────────────────────────

class _ToyTransform(VectorTransform):
    """A client the framework has never seen. It claims elementwise kinds and
    delegates the real work, but records that every hook was invoked -- proving
    the pipeline drives an arbitrary client through the full sequence."""

    name = 'toy'
    kinds = ('vector-add',)

    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.calls = []
        self._inner = ElementwiseTransform()

    def reset(self):
        self.calls.append('reset')
        self._inner.reset()

    def match(self, desc, instrs, kernel, legality):
        self.calls.append('match')
        if self.fail_at == 'match':
            return MatchResult(False, 'toy-refused')
        return self._inner.match(desc, instrs, kernel, legality)

    def lower(self, instrs, lo, hi, desc, kernel, legality, match):
        self.calls.append('lower')
        if self.fail_at == 'lower':
            return None, 'toy-refused'
        return self._inner.lower(instrs, lo, hi, desc, kernel, legality, match)

    def validate(self, scalar_instrs, vector_instrs, lo, hi):
        self.calls.append('validate')
        if self.fail_at == 'validate':
            return 'mismatch', 'toy-refused'
        return self._inner.validate(scalar_instrs, vector_instrs, lo, hi)

    def dynamic_model(self, desc, kernel, legality, match):
        self.calls.append('dynamic_model')
        if self.fail_at == 'dynamic':
            return DynamicModel(1, 999)             # no reduction -> declined
        return self._inner.dynamic_model(desc, kernel, legality, match)


def test_framework_is_generic():
    print("the pipeline drives an arbitrary client through the whole sequence")
    toy = _ToyTransform()
    out, stats, reps = run_module(_ir(ADD8), [toy])
    check("toy client vectorized a kernel", stats.vectorized == 1)
    check("every hook was called in order",
          toy.calls == ['reset', 'match', 'lower', 'validate', 'dynamic_model'])
    check("stats credit the client by name", stats.by_transform.get('toy') == 1)
    check("report records the transform", reps and reps[0].transform == 'toy')


def test_framework_gates_apply_to_every_client():
    print("every gate rolls back, whichever client fails it")
    expect = {'match': 'pattern:toy-refused',
              'lower': 'lower:toy-refused',
              'validate': 'differential:mismatch',
              'dynamic': 'no-dynamic-reduction'}
    for stage, reason_prefix in expect.items():
        ir = _ir(ADD8)
        out, stats, reps = run_module(copy.deepcopy(ir), [_ToyTransform(stage)])
        rolled = stats.vectorized == 0
        same = [repr(i) for i in out] == [repr(i) for i in ir]
        check(f"fail at {stage}: not committed", rolled)
        check(f"fail at {stage}: scalar IR kept unchanged", same)
        check(f"fail at {stage}: reason reported",
              reps and reps[0].reason.startswith(reason_prefix))


def test_single_pipeline_multiple_clients():
    print("one pipeline serves both clients in a single pass")
    MIXED = (DOT8.replace('f(', 'dot(') + RED8.replace('f(', 'red(')
             + ADD8.replace('f(', 'vadd(') + CPY8.replace('f(', 'vcpy('))
    ir = _ir(MIXED)
    out, stats, reps = vectorize_all_module(ir)
    check("all four kernels vectorized", stats.vectorized == 4)
    check("both clients contributed",
          stats.by_transform.get('dot-reduction') == 2
          and stats.by_transform.get('elementwise') == 2)
    m = _mcode(out)
    check("emits $dot, $vreduce and $v from one pass",
          '$dot' in m and '$vreduce' in m and '$v ' in m)
    check("behaviour identical", _correct(ir, out))


# ── Phase 5: R4.1 converted with no regressions ─────────────────────────────────

def test_r41_unchanged_by_conversion():
    print("R4.1 dot/reduction is byte-identical through the framework")
    for code in (DOT8, RED8):
        ir = _ir(code)
        a, sa, _ = vectorize_module(copy.deepcopy(ir))          # R4.1 client only
        b, sb, _ = vectorize_all_module(copy.deepcopy(ir))      # full client set
        check(f"{code[:14]}..: still vectorized", sa.vectorized == sb.vectorized == 1)
        check(f"{code[:14]}..: identical IR",
              [repr(i) for i in a] == [repr(i) for i in b])
    ir = _ir(DOT8)
    out, stats, _ = vectorize_module(ir)
    check("R4.1 still emits $dot", '$dot' in _mcode(out))
    check("R4.1 still behaviour-identical", _correct(ir, out))


# ── Phase 2-3: elementwise recognition + lowering ───────────────────────────────

def test_elementwise_shapes():
    print("the four supported elementwise shapes vectorize and stay correct")
    for name, code, wants_valu in (('add', ADD8, True), ('sub', SUB8, True),
                                   ('mul', MUL8, True), ('copy', CPY8, False)):
        ir = _ir(code)
        out, stats, reps = vectorize_elementwise_module(ir)
        check(f"{name}: vectorized", stats.vectorized == 1)
        m = _mcode(out)
        check(f"{name}: {'emits' if wants_valu else 'needs no'} $v",
              ('$v ' in m) == wants_valu)
        check(f"{name}: behaviour identical", _correct(ir, out))


def test_element_types():
    print("elementwise works across the packed element types")
    for name, code, lanes in (('vi8', ADD8, 8), ('vu8', ADDU8, 8),
                              ('vi16', ADD16, 4), ('vi32', ADD32, 2)):
        ir = _ir(code)
        out, stats, reps = vectorize_elementwise_module(ir)
        check(f"{name}: vectorized with {lanes} lanes",
              stats.vectorized == 1 and reps[0].lanes == lanes)
        check(f"{name}: behaviour identical", _correct(ir, out))


def test_packed_store_writes_every_lane():
    print("the packed store writes all lanes (not just the first)")
    ir = _ir(ADD8)
    out, stats, _ = vectorize_elementwise_module(ir)
    check("vectorized", stats.vectorized == 1)
    # the differential compares FULL memory, so a scatter that dropped lanes would
    # mismatch; assert it explicitly against the scalar form
    lo, hi = next(iter(func_slices(ir)))
    verdict, detail = differential_packed(ir, out, lo, hi)
    check(f"full-memory differential is a definite match ({detail})",
          verdict == 'match')


def test_scalar_remainder():
    print("a non-multiple trip keeps a scalar remainder and stays correct")
    ir = _ir(ADDREM)                            # N=20, lanes=8 -> 2 chunks + 4 tail
    out, stats, reps = vectorize_elementwise_module(ir)
    check("vectorized with remainder",
          stats.vectorized == 1 and reps[0].chunks == 2 and reps[0].remainder == 4)
    check("still correct", _correct(ir, out))


# ── Phase 4: rejection + rollback ───────────────────────────────────────────────

def test_rejections():
    print("unsupported elementwise shapes are rejected (scalar kept)")
    cases = {
        # ordinary (unpacked) arrays cannot be gathered/scattered
        'unpacked': ("long long f(){int a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}",
                     'contiguous-store'),
        # divide is not an elementwise VALU op
        'divide': ("long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]/b[i];return c[0];}",
                   'unsupported-operator'),
        # a second array store in the body
        'two-stores': ("long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++){c[i]=a[i]+b[i];b[i]=a[i];}return c[0];}",
                       'expect-exactly-one-contiguous-store'),
        # trip smaller than 2*lanes is not worth the remainder
        'small-trip': ("long long f(){vi8_t a[8],b[8],c[8];int i;for(i=0;i<8;i++)c[i]=a[i]+b[i];return c[0];}",
                       'unprofitable'),
        # unknown trip count
        'symbolic': ("long long f(int n){vi8_t a[32],b[32],c[32];int i;for(i=0;i<n;i++)c[i]=a[i]+b[i];return c[0];}",
                     'unprofitable'),
    }
    for name, (code, reason) in cases.items():
        ir = _ir(code)
        out, stats, reps = vectorize_elementwise_module(copy.deepcopy(ir))
        check(f"{name}: not vectorized", stats.vectorized == 0)
        check(f"{name}: IR unchanged (scalar kept)",
              [repr(i) for i in out] == [repr(i) for i in ir])
        check(f"{name}: reason mentions '{reason}'",
              bool(reps) and reason in reps[0].reason)


def test_r45_newly_accepted():
    print("shapes R4.2 rejected are ACCEPTED since R4.5 expression trees")
    for n, c in (('a[i]*3 (const scalar)',
                  "long long f(){vi8_t a[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]*3;return c[0];}"),
                 ('a[i+1]+b[i] (shifted access, R4.6)',
                  "long long f(){vi8_t a[33],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i+1]+b[i];return c[0];}"),
                 ('a[i]+b[i]+c[i] (3 operands)',
                  "long long f(){vi8_t a[32],b[32],c[32],d[32];int i;for(i=0;i<32;i++)d[i]=a[i]+b[i]+c[i];return d[0];}")):
        ir = _ir(c)
        out, stats, _r = vectorize_all_module(ir)
        check(f"{n}: now vectorized", stats.vectorized == 1)
        check(f"{n}: correct", _correct(ir, out))


def test_no_regression_non_kernel():
    print("a program with no packed kernel is byte-identical (no regression)")
    ir = _ir(NOKERNEL)
    out, stats, _ = vectorize_all_module(copy.deepcopy(ir))
    check("nothing vectorized", stats.vectorized == 0)
    check("generated code identical", _mcode(ir) == _mcode(out))


# ── measurement + determinism ───────────────────────────────────────────────────

def test_dynamic_reduction():
    print("elementwise vectorization reduces dynamic operation count")
    worse = 0
    total = 0
    for code in (ADD8, SUB8, MUL8, CPY8, ADD16, ADD32, ADDU8):
        ir = _ir(code)
        _out, stats, reps = vectorize_elementwise_module(ir)
        if stats.vectorized:
            total += 1
            r = reps[0]
            if r.vector_dynamic >= r.scalar_dynamic:
                worse += 1
    check(f"all {total} committed kernels execute fewer ops",
          total == 7 and worse == 0)


def test_determinism():
    print("vectorization is deterministic")
    o1, _s1, _r1 = vectorize_all_module(_ir(ADD8))
    o2, _s2, _r2 = vectorize_all_module(_ir(ADD8))
    check("identical output twice", [repr(i) for i in o1] == [repr(i) for i in o2])


def test_hundred_percent_validation():
    print("every committed vectorization passes the differential (100%)")
    total = mism = 0
    for code in (ADD8, SUB8, MUL8, CPY8, ADD16, ADD32, ADDU8, ADDREM, DOT8, RED8):
        ir = _ir(code)
        out, stats, _ = vectorize_all_module(ir)
        if stats.vectorized:
            total += 1
            if not _correct(ir, out):
                mism += 1
    check("all committed kernels validated", total >= 10 and mism == 0)


def main():
    for t in (test_framework_is_generic,
              test_framework_gates_apply_to_every_client,
              test_single_pipeline_multiple_clients,
              test_r41_unchanged_by_conversion,
              test_elementwise_shapes,
              test_element_types,
              test_packed_store_writes_every_lane,
              test_scalar_remainder,
              test_rejections,
              test_r45_newly_accepted,
              test_no_regression_non_kernel,
              test_dynamic_reduction,
              test_determinism,
              test_hundred_percent_validation):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R4.2 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

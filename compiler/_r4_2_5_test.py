"""
_r4_2_5_test.py -- unit tests for R4.2.5 Compact Vector Loop Generation.

Verifies: compact loops are generated and are behaviour-identical, the
realisation is chosen by MEASURED size (compact for many chunks, unrolled for
few), the IV hand-off to the scalar remainder is correct (the subtle part -- the
compact loop must leave the induction variable at exactly chunks*lanes), static
size falls, coverage and correctness are preserved, output stays spill-free and
deterministic, and the vector pipeline architecture is untouched.

Run:  python3 compiler/_r4_2_5_test.py
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pycparser
from compiler import _FAKE_TYPEDEFS
from ir import Temp, IRLabel
from ir_gen import IRGenerator
from ir_utils import func_slices
from codegen import CodeGen
from bundler import bundle_mcode
from dot_vectorizer import vectorize_all_module
from vector_lowering import differential_packed
import vector_compact_loop as _vcl
import vector_pipeline

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


def _bundles(ir):
    return bundle_mcode(_mcode(ir), schedule=True)[2]


def _spills(ir):
    cg = CodeGen(global_base=0x400)
    cg.generate(copy.deepcopy(ir), global_base=0x400)
    return bool(cg.spilled)


def _correct(ir, out):
    return all(differential_packed(ir, out, lo, hi)[0] != 'mismatch'
               for lo, hi in func_slices(ir))


def _forced(code, realisation):
    """Vectorize `code` with the realisation forced, restoring the environment."""
    old = os.environ.get('APARA_VECTOR_REALISATION')
    os.environ['APARA_VECTOR_REALISATION'] = realisation
    try:
        ir = _ir(code)
        out, stats, reps = vectorize_all_module(ir)
        return ir, out, stats, reps
    finally:
        if old is None:
            os.environ.pop('APARA_VECTOR_REALISATION', None)
        else:
            os.environ['APARA_VECTOR_REALISATION'] = old


# ── sources: MANY chunks (compact should win) vs FEW chunks (unrolled wins) ─────

ADD16_8C = "long long f(){vi16_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}"
ADD32_8C = "long long f(){vi32_t a[16],b[16],c[16];int i;for(i=0;i<16;i++)c[i]=a[i]+b[i];return c[0];}"
MUL16_8C = "long long f(){vi16_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]*b[i];return c[0];}"
DOT16_8C = "long long f(){vi16_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}"
ADD8_4C = "long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}"
DOT8_4C = "long long f(){vi8_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}"
CPY8_4C = "long long f(){vi8_t a[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i];return c[0];}"
RED8_6C = "long long f(){vi8_t a[48];int i;long long s=0;for(i=0;i<48;i++)s+=a[i];return s;}"
# remainders: the IV hand-off from the compact loop to the scalar tail
ADDREM = "long long f(){vi8_t a[20],b[20],c[20];int i;for(i=0;i<20;i++)c[i]=a[i]+b[i];return c[0];}"
DOTREM = "long long f(){vi8_t a[20],b[20];int i;long long s=0;for(i=0;i<20;i++)s+=a[i]*b[i];return s;}"
REDREM = "long long f(){vi16_t a[30];int i;long long s=0;for(i=0;i<30;i++)s+=a[i];return s;}"
NOKERNEL = "long long f(int n){int i;long long s=0;for(i=0;i<n;i++)s+=i*3;return s;}"

_ALL = [ADD16_8C, ADD32_8C, MUL16_8C, DOT16_8C, ADD8_4C, DOT8_4C, CPY8_4C,
        RED8_6C, ADDREM, DOTREM, REDREM]


def test_compact_loop_is_generated():
    print("compact vector loops are generated and emit real vector ops")
    for name, code in (('add vi16', ADD16_8C), ('dot vi16', DOT16_8C),
                       ('mul vi16', MUL16_8C)):
        ir, out, stats, _ = _forced(code, 'compact')
        check(f"{name}: vectorized", stats.vectorized == 1)
        check(f"{name}: emitted a compact loop",
              _vcl.realisation_of(out) == 'compact')
        m = _mcode(out)
        check(f"{name}: vector op survives to mcode",
              ('$v ' in m) or ('$dot' in m) or ('$vreduce' in m))


def test_both_realisations_are_correct():
    print("BOTH realisations are behaviour-identical (the oracle checks each)")
    for code in _ALL:
        for real in ('compact', 'unrolled'):
            ir, out, stats, _ = _forced(code, real)
            if not stats.vectorized:
                continue
            check(f"{real} {code[16:30]}..: differential match", _correct(ir, out))


def test_iv_handoff_to_scalar_remainder():
    """The subtle part. The compact loop counts the kernel's OWN induction
    variable up to chunks*lanes and the scalar tail resumes from there, so a
    wrong hand-off would either redo or skip elements. The differential compares
    the FULL final memory, so it catches both."""
    print("compact loop hands the IV to the scalar remainder correctly")
    for name, code, chunks, rem in (('add rem', ADDREM, 2, 4),
                                    ('dot rem', DOTREM, 2, 4),
                                    ('red rem', REDREM, 7, 2)):
        ir, out, stats, reps = _forced(code, 'compact')
        check(f"{name}: vectorized as compact",
              stats.vectorized == 1 and _vcl.realisation_of(out) == 'compact')
        check(f"{name}: chunks/remainder as expected",
              reps[0].chunks == chunks and reps[0].remainder == rem)
        verdict, detail = differential_packed(ir, out, *next(iter(func_slices(ir))))
        check(f"{name}: full-memory differential is a definite match ({detail})",
              verdict == 'match')


def test_realisation_chosen_by_measurement():
    """R6.2 NOTE: this used to hard-code which side of the crossover each kernel
    lands on (8 chunks -> compact, 4 chunks -> unrolled), a snapshot of where
    R4.2.5 measured it. Symbolic memory disambiguation made the unrolled form
    pack considerably tighter, so the crossover MOVED and the 8-chunk vi16/vi32
    kernels now choose unrolled. The invariant this test exists to protect --
    that the realisation is MEASURED rather than assumed, and that the compiler
    keeps the smaller of the two candidates -- is unchanged, and is what is
    asserted now. Hard-coding the outcome again would just re-freeze today's
    crossover."""
    print("the realisation is chosen by MEASURED bundle count, not assumed")
    for name, code in (('add vi16 (8 chunks)', ADD16_8C),
                       ('add vi32 (8 chunks)', ADD32_8C),
                       ('add vi8 (4 chunks)', ADD8_4C),
                       ('dot vi8 (4 chunks)', DOT8_4C),
                       ('copy vi8 (4 chunks)', CPY8_4C)):
        ir = _ir(code)
        out, stats, _ = vectorize_all_module(ir)
        got = _vcl.realisation_of(out)
        check(f"{name}: vectorized with a real realisation ({got})",
              stats.vectorized == 1 and got.startswith(('compact', 'unrolled')))
    # and the choice is genuinely the smaller one -- the actual invariant
    for code in (ADD16_8C, ADD32_8C, ADD8_4C, DOT8_4C, CPY8_4C):
        _i, comp, sc, _ = _forced(code, 'compact')
        _i, unro, su, _ = _forced(code, 'unrolled')
        auto = vectorize_all_module(_ir(code))[0]
        best = min(_bundles(comp), _bundles(unro))
        check(f"{code[16:28]}..: auto choice matches the smaller candidate",
              _bundles(auto) == best)


def test_static_size_reduced():
    # R6.4 NOTE: vector loop unrolling deliberately trades STATIC SIZE for ILP,
    # so with the default factor the compact form is no longer the smaller one.
    # The property this test guards -- that the realisation CHOICE reduces size
    # versus always-unrolled -- is about R4.2.5, so the factor is pinned to 1
    # here rather than the assertion being dropped.
    _old = os.environ.get('APARA_VECTOR_UNROLL')
    os.environ['APARA_VECTOR_UNROLL'] = '1'
    try:
        return _test_static_size_reduced_body()
    finally:
        if _old is None: os.environ.pop('APARA_VECTOR_UNROLL', None)
        else: os.environ['APARA_VECTOR_UNROLL'] = _old


def _test_static_size_reduced_body():
    print("static size falls versus always-unrolled (R4.2 behaviour)")
    tot_u = tot_a = 0
    chars_u = chars_a = 0
    for code in _ALL:
        _i, unro, su, _ = _forced(code, 'unrolled')
        auto = vectorize_all_module(_ir(code))[0]
        if not su.vectorized:
            continue
        tot_u += _bundles(unro)
        tot_a += _bundles(auto)
        chars_u += len(_mcode(unro))
        chars_a += len(_mcode(auto))
    check(f"bundle count reduced ({tot_u} -> {tot_a})", tot_a < tot_u)
    check(f"code size reduced ({chars_u} -> {chars_a} chars)", chars_a < chars_u)


def test_coverage_preserved():
    print("vector coverage is unchanged by compaction")
    n_auto = n_unrolled = 0
    for code in _ALL:
        n_auto += vectorize_all_module(_ir(code))[1].vectorized
        n_unrolled += _forced(code, 'unrolled')[2].vectorized
    check(f"same kernels vectorized ({n_auto} vs {n_unrolled})",
          n_auto == n_unrolled and n_auto == len(_ALL))


def test_dynamic_reduction_preserved():
    print("dynamic operation reduction is still overwhelming")
    worst = 1.0
    for code in _ALL:
        _ir0, out, stats, reps = _forced(code, 'compact')
        if not stats.vectorized:
            continue
        r = reps[0]
        check_ratio = r.vector_dynamic / r.scalar_dynamic
        worst = max(worst if worst < 1 else 0.0, check_ratio)
        check(f"{code[16:28]}..: fewer dynamic ops "
              f"({r.scalar_dynamic}->{r.vector_dynamic})",
              r.vector_dynamic < r.scalar_dynamic)
    check(f"worst-case compact kernel still under 50% of scalar ops "
          f"({worst:.0%})", worst < 0.5)


def test_spill_free_and_deterministic():
    print("compact output is spill-free and deterministic")
    for code in _ALL:
        _i, out, stats, _ = _forced(code, 'compact')
        if not stats.vectorized:
            continue
        check(f"{code[16:28]}..: no spills", not _spills(out))
    a = vectorize_all_module(_ir(ADD16_8C))[0]
    b = vectorize_all_module(_ir(ADD16_8C))[0]
    check("identical output twice", [repr(i) for i in a] == [repr(i) for i in b])


def test_no_regression():
    print("non-kernel programs are byte-identical (no regression)")
    ir = _ir(NOKERNEL)
    out, stats, _ = vectorize_all_module(copy.deepcopy(ir))
    check("nothing vectorized", stats.vectorized == 0)
    check("generated code identical", _mcode(ir) == _mcode(out))


def test_pipeline_architecture_untouched():
    print("the vector pipeline architecture is unchanged")
    tf = vector_pipeline.VectorTransform
    check("client interface is still kinds/match/lower/validate/dynamic_model",
          all(hasattr(tf, h) for h in
              ('kinds', 'match', 'lower', 'validate', 'dynamic_model', 'reset')))
    import inspect
    sig = inspect.signature(tf.lower)
    check("lower() signature unchanged",
          list(sig.parameters) == ['self', 'instrs', 'lo', 'hi', 'desc',
                                   'kernel', 'legality', 'match'])
    src = inspect.getsource(vector_pipeline._vectorize_function)
    check("pipeline knows nothing about realisations",
          'compact' not in src and 'unrolled' not in src)


def main():
    for t in (test_compact_loop_is_generated,
              test_both_realisations_are_correct,
              test_iv_handoff_to_scalar_remainder,
              test_realisation_chosen_by_measurement,
              test_static_size_reduced,
              test_coverage_preserved,
              test_dynamic_reduction_preserved,
              test_spill_free_and_deterministic,
              test_no_regression,
              test_pipeline_architecture_untouched):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R4.2.5 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

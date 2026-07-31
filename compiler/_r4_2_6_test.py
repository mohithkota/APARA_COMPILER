"""
_r4_2_6_test.py -- unit tests for R4.2.6 Post-Optimizer Size Gate & Remainder
Peeling.

Two quality fixes to R4.2.5's realisation selection:

  * the size probe now measures the candidate AS PRODUCTION WOULD BUILD IT
    (tier-1 scalar optimizer + superblock scheduling), closing the documented
    gap where the ranking flipped after lowering had already chosen;
  * a peeled-remainder realisation is offered, replacing the residual scalar
    tail loop with straight-line iterations at constant offsets;
  * a MARGIN stops a challenger taking the switch for a trivial size win, since
    every alternative realisation trades dynamic operations for static size.

Run:  python3 compiler/_r4_2_6_test.py
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
from dot_vectorizer import vectorize_all_module
from vector_lowering import differential_packed
from vector_size_probe import probe_bundles, _optimize_like_production
from vector_pipeline import _bundles
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


def _correct(ir, out):
    return all(differential_packed(ir, out, lo, hi)[0] != 'mismatch'
               for lo, hi in func_slices(ir))


def _forced(code, realisation):
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


# remainder-bearing kernels (the peeling targets) and exact-multiple ones
ADDREM = "long long f(){vi8_t a[20],b[20],c[20];int i;for(i=0;i<20;i++)c[i]=a[i]+b[i];return c[0];}"
CPYREM = "long long f(){vi8_t a[20],c[20];int i;for(i=0;i<20;i++)c[i]=a[i];return c[0];}"
MULREM16 = "long long f(){vi16_t a[30],b[30],c[30];int i;for(i=0;i<30;i++)c[i]=a[i]*b[i];return c[0];}"
REDREM16 = "long long f(){vi16_t a[30];int i;long long s=0;for(i=0;i<30;i++)s+=a[i];return s;}"
DOTREM = "long long f(){vi8_t a[20],b[20];int i;long long s=0;for(i=0;i<20;i++)s+=a[i]*b[i];return s;}"
REDREM8 = "long long f(){vi8_t a[52];int i;long long s=0;for(i=0;i<52;i++)s+=a[i];return s;}"
ADD16 = "long long f(){vi16_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}"
ADD8 = "long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}"
NOKERNEL = "long long f(int n){int i;long long s=0;for(i=0;i<n;i++)s+=i*3;return s;}"

_REM = [ADDREM, CPYREM, MULREM16, REDREM16, DOTREM, REDREM8]
_ALL = _REM + [ADD16, ADD8]
_REALS = ('unrolled', 'compact', 'unrolled+peeled', 'compact+peeled')


# ── R4.2.6a: the post-optimizer probe ───────────────────────────────────────────

def test_probe_models_production():
    print("the size probe measures the candidate as production would build it")
    ir = _ir(ADD16)
    cheap, _ = _bundles(ir, 0x400)
    post, _ = probe_bundles(ir, 0x400)
    check(f"post-optimizer size differs from the raw one ({cheap} vs {post})",
          post != cheap)
    check("post-optimizer size is smaller (the optimizer helps)", post < cheap)
    opt = _optimize_like_production(copy.deepcopy(ir))
    check("the optimizer sequence produces runnable IR", len(opt) > 0)


def test_fast_probe_knob():
    print("APARA_VECTOR_FAST_PROBE reverts to the cheap pre-optimizer probe")
    ir = _ir(ADD16)
    os.environ['APARA_VECTOR_FAST_PROBE'] = '1'
    try:
        fast, _ = probe_bundles(ir, 0x400)
    finally:
        os.environ.pop('APARA_VECTOR_FAST_PROBE', None)
    cheap, _ = _bundles(ir, 0x400)
    check("fast probe == the raw backend probe", fast == cheap)


def test_probe_picks_the_production_winner():
    """The R4.2.5 report documented a case where the pre-optimizer probe chose
    the realisation that was LARGER after the optimizer ran. The post-optimizer
    probe must now pick the one that is genuinely smaller in production."""
    print("the probe picks the realisation that is smaller AFTER optimization")
    for code in (ADD8, ADD16, DOTREM):
        sizes = {}
        for real in _REALS:
            _i, out, st, _ = _forced(code, real)
            if st.vectorized:
                b, spilled = probe_bundles(out, 0x400)
                if b is not None and not spilled:
                    sizes[real] = b
        auto_out = vectorize_all_module(_ir(code))[0]
        auto_b, _ = probe_bundles(auto_out, 0x400)
        inc = sizes.get('unrolled')
        # the chosen size must never exceed the incumbent's
        check(f"{code[16:28]}..: choice is no worse than unrolled "
              f"({auto_b} <= {inc})", inc is None or auto_b <= inc)


# ── R4.2.6b: the margin ─────────────────────────────────────────────────────────

def test_margin_rejects_trivial_wins():
    print("a challenger must EARN the switch with a meaningful size win")
    # with a 100% margin nothing can ever clear it -> the incumbent always wins
    old = os.environ.get('APARA_VECTOR_COMPACT_MARGIN')
    os.environ['APARA_VECTOR_COMPACT_MARGIN'] = '1.0'
    try:
        out = vectorize_all_module(_ir(ADD16))[0]
        check("margin 1.0 keeps the unrolled incumbent",
              _vcl.realisation_of(out) == 'unrolled')
    finally:
        if old is None:
            os.environ.pop('APARA_VECTOR_COMPACT_MARGIN', None)
        else:
            os.environ['APARA_VECTOR_COMPACT_MARGIN'] = old
    # With no margin the genuinely-smaller candidate is taken.
    # R6.2 NOTE: this used to assert that candidate is COMPACT for ADD16. That
    # was true when R4.2.6 measured it; symbolic memory disambiguation then made
    # the unrolled form pack tighter, so compact is no longer the smaller one
    # here. The margin MECHANISM is what this test guards, so it now asserts the
    # mechanism -- dropping the margin never yields a LARGER result than keeping
    # it -- instead of re-freezing which form happens to win today.
    os.environ['APARA_VECTOR_COMPACT_MARGIN'] = '0.0'
    try:
        out0 = vectorize_all_module(_ir(ADD16))[0]
        b0, sp0 = probe_bundles(out0, 0x400)
    finally:
        os.environ.pop('APARA_VECTOR_COMPACT_MARGIN', None)
    os.environ['APARA_VECTOR_COMPACT_MARGIN'] = '1.0'
    try:
        out1 = vectorize_all_module(_ir(ADD16))[0]
        b1, sp1 = probe_bundles(out1, 0x400)
    finally:
        os.environ.pop('APARA_VECTOR_COMPACT_MARGIN', None)
    check(f"margin 0.0 never yields a larger result than margin 1.0 "
          f"({b0} <= {b1})", b0 is not None and b1 is not None and b0 <= b1)


# ── R4.2.6c: remainder peeling ──────────────────────────────────────────────────

def test_peeled_realisations_are_offered_and_correct():
    print("peeled realisations are built and are behaviour-identical")
    for code in _REM:
        for real in ('unrolled+peeled', 'compact+peeled'):
            ir, out, st, _ = _forced(code, real)
            check(f"{real} {code[16:26]}..: vectorized", st.vectorized == 1)
            check(f"{real} {code[16:26]}..: differential match", _correct(ir, out))
            check(f"{real} {code[16:26]}..: realisation reported",
                  _vcl.realisation_of(out) == real)


def test_peeling_deletes_the_tail_loop():
    """A peeled kernel must contain no residual scalar tail loop -- that is the
    whole point -- and must still leave the IV at `trip` so memory matches."""
    print("peeling deletes the scalar tail loop and fixes the IV")
    from ir import IRLabel
    ir, out, st, reps = _forced(ADDREM, 'unrolled+peeled')
    check("vectorized", st.vectorized == 1)
    scalar_loops = sum(1 for i in ir if isinstance(i, IRLabel))
    peeled_loops = sum(1 for i in out if isinstance(i, IRLabel))
    check(f"fewer labels than the scalar form ({peeled_loops} < {scalar_loops})",
          peeled_loops < scalar_loops)
    verdict, detail = differential_packed(ir, out, *next(iter(func_slices(ir))))
    check(f"full-memory differential is a definite match ({detail})",
          verdict == 'match')


def test_peeling_is_chosen_where_it_helps():
    print("peeling is chosen exactly where it is genuinely smaller")
    chosen = {}
    for code in _REM:
        out = vectorize_all_module(_ir(code))[0]
        chosen[code[16:28]] = _vcl.realisation_of(out)
    peeled = [k for k, v in chosen.items() if 'peeled' in v]
    check(f"peeling wins on some remainder kernels ({len(peeled)} of {len(_REM)})",
          len(peeled) >= 1)
    check("peeling is NOT applied blindly to every remainder kernel",
          len(peeled) < len(_REM))
    # and where it is chosen, it really is no worse than the incumbent
    for code in _REM:
        auto = vectorize_all_module(_ir(code))[0]
        _i, unro, st, _ = _forced(code, 'unrolled')
        if not st.vectorized:
            continue
        a, _ = probe_bundles(auto, 0x400)
        u, _ = probe_bundles(unro, 0x400)
        check(f"{code[16:26]}..: chosen size {a} <= unrolled {u}", a <= u)


# ── invariants that must survive both changes ───────────────────────────────────

def test_all_realisations_validate():
    print("every realisation of every kernel passes the differential")
    total = mism = 0
    for code in _ALL:
        for real in _REALS:
            ir, out, st, _ = _forced(code, real)
            if not st.vectorized:
                continue
            total += 1
            if not _correct(ir, out):
                mism += 1
    check(f"{total} realisations validated, {mism} mismatches",
          total >= 24 and mism == 0)


def test_spill_free_and_deterministic():
    print("output stays spill-free and deterministic")
    for code in _ALL:
        out, st, _ = vectorize_all_module(_ir(code))
        if not st.vectorized:
            continue
        cg = CodeGen(global_base=0x400)
        cg.generate(copy.deepcopy(out), global_base=0x400)
        check(f"{code[16:26]}..: no spills", not cg.spilled)
    a = vectorize_all_module(_ir(MULREM16))[0]
    b = vectorize_all_module(_ir(MULREM16))[0]
    check("identical output twice", [repr(i) for i in a] == [repr(i) for i in b])


def test_no_regression():
    print("non-kernel programs are byte-identical (no regression)")
    ir = _ir(NOKERNEL)
    out, stats, _ = vectorize_all_module(copy.deepcopy(ir))
    check("nothing vectorized", stats.vectorized == 0)
    check("generated code identical", _mcode(ir) == _mcode(out))


def test_pipeline_architecture_untouched():
    print("the vector pipeline architecture is still unchanged")
    import inspect
    tf = vector_pipeline.VectorTransform
    check("client interface unchanged",
          all(hasattr(tf, h) for h in
              ('kinds', 'match', 'lower', 'validate', 'dynamic_model', 'reset')))
    check("lower() signature unchanged",
          list(inspect.signature(tf.lower).parameters) ==
          ['self', 'instrs', 'lo', 'hi', 'desc', 'kernel', 'legality', 'match'])
    src = inspect.getsource(vector_pipeline._vectorize_function)
    check("pipeline knows nothing about realisations or peeling",
          all(w not in src for w in ('compact', 'unrolled', 'peel')))


def main():
    for t in (test_probe_models_production,
              test_fast_probe_knob,
              test_probe_picks_the_production_winner,
              test_margin_rejects_trivial_wins,
              test_peeled_realisations_are_offered_and_correct,
              test_peeling_deletes_the_tail_loop,
              test_peeling_is_chosen_where_it_helps,
              test_all_realisations_validate,
              test_spill_free_and_deterministic,
              test_no_regression,
              test_pipeline_architecture_untouched):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R4.2.6 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

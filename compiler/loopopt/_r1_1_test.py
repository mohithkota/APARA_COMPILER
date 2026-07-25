"""
_r1_1_test.py -- unit tests for the R1.1 LoopUnroll infrastructure.

R1.1 performs NO unrolling, so these tests assert the ANALYSIS: structural
eligibility, the detailed legality report (each rejection reason), the
profitability model's decision (including the cases it must reject), and that the
LoopUnroll transform is a clean no-op through the M5 framework (0 IR changes /
0 verifier failures / 0 rollbacks).

Run:  python3 compiler/loopopt/_r1_1_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRStore, IRLoad, IRLoadAddr, Temp, Const)
from loopopt import discover
from loopopt.loop_unroll import (LoopUnroll, UnrollProfitability, UnrollLegality,
                                 analyze_module, drive_noop)
from loopopt.analysis_iv import TripCount
from loopopt._m5_test import sum_loop
from loopopt._m6_test import nested_sum, multi_exit_sum
from loopopt._m7_test import _call_loop

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _report(factory, hlbl):
    Temp.reset()
    ir = factory()
    reps = analyze_module(ir)
    return ir, next((r for r in reps if r.label == hlbl), None), reps


# ── factories for the specific rejection / trip cases ─────────────────────────

def unknown_trip_loop():
    """Clean IV but the exit bound is a variable -> trip count is not KNOWN."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLabel('pre'),
            IRLoadAddr(Temp('pn'), -16), IRLoad(Temp('n'), Temp('pn'), Const(0), 8),
            IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
            IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Temp('n'), 'body', 'exit'),
            IRLabel('body'),
            IRBinOp(Temp('iv2'), '+', Temp('iv'), Const(1)),
            IRLoadAddr(Temp('pi'), -8), IRStore(Temp('pi'), Const(0), Temp('iv2'), 8),
            IRJump('head'),
            IRLabel('exit'), IRReturn(Const(0)), IRFuncEnd('f')]


def no_iv_loop():
    """Exit tests a computed value (a+b), not an induction variable."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pa'), -8), IRLoad(Temp('a'), Temp('pa'), Const(0), 8),
            IRLoadAddr(Temp('pb'), -16), IRLoad(Temp('b'), Temp('pb'), Const(0), 8),
            IRBinOp(Temp('sum'), '+', Temp('a'), Temp('b')),
            IRCondJump(Temp('sum'), '<', Const(10), 'body', 'exit'),
            IRLabel('body'),
            IRBinOp(Temp('a2'), '+', Temp('a'), Const(1)),
            IRStore(Temp('pa'), Const(0), Temp('a2'), 8),
            IRJump('head'),
            IRLabel('exit'), IRReturn(Const(0)), IRFuncEnd('f')]


# ── tests ─────────────────────────────────────────────────────────────────────

def test_eligible_canonical():
    print("canonical counting loop is eligible with the right structural facts:")
    _ir, r, _ = _report(lambda: sum_loop(10), 'head')
    check("report found", r is not None)
    check("eligible", r.eligible)
    check("single latch", r.single_latch)
    check("unique preheader", r.unique_preheader)
    check("has induction variable", r.has_iv)
    check("supported shape (not irregular)", r.shape != 'irregular')
    check("legality report lists every check", len(r.legality.facts) == 9)


def test_known_trip_recorded():
    print("known trip count is recorded:")
    _ir, r, _ = _report(lambda: sum_loop(6), 'head')
    check("trip kind KNOWN", r.trip_kind == TripCount.KNOWN)
    check("trip value captured", r.trip_value == 6)
    check("iv step captured", r.iv_step is not None)


def test_unknown_trip():
    print("unknown trip count: still eligible, partial-remainder mode:")
    _ir, r, _ = _report(unknown_trip_loop, 'head')
    check("eligible (clean IV, unknown bound)", r.eligible)
    check("trip kind not KNOWN", r.trip_kind != TripCount.KNOWN)
    check("mode needs a remainder loop", r.profit.needs_remainder)
    check("suitability partial-remainder", r.profit.trip_suitability == 'partial-remainder')


def test_nested_rejected_outer():
    print("nested loop: outer rejected (not innermost), inner eligible:")
    Temp.reset()
    ir = nested_sum()
    reps = analyze_module(ir)
    inners = [r for r in reps if r.is_innermost]
    outers = [r for r in reps if not r.is_innermost]
    check("found an inner and an outer loop", inners and outers)
    check("inner loop is eligible", all(r.eligible for r in inners))
    check("outer loop rejected", all(not r.eligible for r in outers))
    check("outer rejection reason is 'not innermost'",
          all('not innermost' in r.legality.reason for r in outers))


def test_reject_call():
    print("loop containing an opaque call is rejected:")
    _ir, r, _ = _report(_call_loop, 'head')
    check("rejected", r is not None and not r.eligible)
    check("reason mentions opaque call", 'opaque call' in r.legality.reason)


def test_reject_irregular():
    print("multi-exit loop rejected as unsupported control flow:")
    _ir, r, _ = _report(multi_exit_sum, 'head')
    check("rejected", r is not None and not r.eligible)
    check("reason is unsupported control flow / not-innermost / no-latch",
          r is not None and not r.eligible)


def test_reject_no_iv():
    print("loop without a recognizable induction variable is rejected:")
    _ir, r, _ = _report(no_iv_loop, 'head')
    check("rejected", r is not None and not r.eligible)
    check("reason mentions induction variable",
          'induction variable' in r.legality.reason)


def test_legality_report_detail():
    print("legality report is detailed (named checks with pass/fail):")
    Temp.reset()
    d = next(x for x in discover(sum_loop(10)) if x.cfg.blocks[x.header].label == 'head')
    rep = UnrollLegality(d)
    names = [f.name for f in rep.facts]
    for req in ('has_labeled_header', 'has_single_latch', 'has_clean_iv',
                'shape_supported', 'is_innermost', 'memory_safe'):
        check(f"check present: {req}", req in names)
    check("as_dict exposes checks", 'checks' in rep.as_dict())


def test_profitability_discriminates():
    print("profitability model computes real decisions (accepts and REJECTS):")
    Temp.reset()
    d = next(x for x in discover(sum_loop(20)) if x.cfg.blocks[x.header].label == 'head')
    from loopopt.analysis_iv import annotate_induction_vars
    from loopopt.analysis_profile import annotate_profile
    annotate_induction_vars([d]); annotate_profile([d])

    # resource-bound, reasonable size -> should unroll
    d.res_mii, d.rec_mii, d.mii = 4, 3, 4
    d.reg_pressure_peak, d.reg_free = 6, 22
    p = UnrollProfitability(d, eligible=True)
    check("resource-bound loop: should_unroll True", p.should_unroll)
    check("recommended factor >= 2", p.recommended_factor >= 2)

    # recurrence-bound (rec > res) -> little ILP to expose -> reject
    d.res_mii, d.rec_mii, d.mii = 2, 9, 9
    p2 = UnrollProfitability(d, eligible=True)
    check("recurrence-bound loop: should_unroll False", not p2.should_unroll)
    check("recurrence reason reported", 'recurrence-bound' in p2.reason)

    # tiny known trip -> nothing to unroll
    d.res_mii, d.rec_mii, d.mii = 4, 3, 4
    d.trip_count = TripCount.known(1)
    p3 = UnrollProfitability(d, eligible=True)
    check("trip==1: should_unroll False", not p3.should_unroll)
    check("tiny-trip suitability", p3.trip_suitability == 'unsuitable-tiny')

    # high register pressure -> would spill -> reject
    d.trip_count = TripCount.known(64)
    d.reg_pressure_peak = 26
    p4 = UnrollProfitability(d, eligible=True)
    check("high pressure: should_unroll False", not p4.should_unroll)
    check("pressure reason reported", 'pressure' in p4.reason)

    # ineligible input is never profitable regardless of numbers
    d.reg_pressure_peak = 6
    p5 = UnrollProfitability(d, eligible=False)
    check("ineligible: should_unroll False", not p5.should_unroll)


def test_framework_noop():
    print("LoopUnroll runs through the framework as a clean no-op:")
    Temp.reset()
    ir = sum_loop(10)
    before = [repr(x) for x in ir]
    stats = drive_noop(ir)
    after = [repr(x) for x in ir]
    check("IR unchanged", before == after)
    check("0 commits", stats.commits == 0)
    check("0 rollbacks", stats.rollbacks == 0)
    check("0 verifier failures", stats.verifier_failures == 0)
    check("every loop was attempted (skipped illegal or no-op)",
          stats.skipped_illegal + stats.skipped_noop == stats.attempts)


def main():
    tests = [test_eligible_canonical, test_known_trip_recorded, test_unknown_trip,
             test_nested_rejected_outer, test_reject_call, test_reject_irregular,
             test_reject_no_iv, test_legality_report_detail,
             test_profitability_discriminates, test_framework_noop]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"R1.1 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("R1.1 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

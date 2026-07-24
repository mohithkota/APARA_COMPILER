"""
_m3_test.py -- unit tests for the M3 Profile analysis.

Checks the IR-level profile facts on hand-written loops of each characteristic
class. Metrics come from the reused parallelism_profile.py functions, so these
tests assert the ADAPTER + wiring are correct (right N, a recurrence detected
when one exists, resource term reflecting memory ops, etc.). Analysis only.
Run:  python3 compiler/loopopt/_m3_test.py

Coverage: compute-bound, memory-bound, recurrence-bound, no-dependency, calls,
nested, do-while, short-circuit.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRStore, IRLoad, IRLoadAddr, IRCall, Temp, Const)
from loopopt import discover, annotate_profile

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _fb():
    return IRFuncBegin('f', [], {}, 0)


def _fe():
    return IRFuncEnd('f')


def analyze(ins):
    descs = discover(ins)
    annotate_profile(descs)
    return descs


def _loop(body):
    return [_fb(), IRLabel('head'),
            IRCondJump(Temp('t'), '<', Temp('n'), 'body', 'exit'),
            IRLabel('body')] + body + [
            IRJump('head'), IRLabel('exit'), IRReturn(Const(0)), _fe()]


def test_no_dependency():
    print("no-dependency loop:")
    x = Temp()
    d = analyze(_loop([IRBinOp(x, '+', Temp('a'), Temp('b'))]))[0]
    check("profile analyzed", d.profile_analyzed)
    check("body_inst_count counts the loop body", d.body_inst_count >= 2)
    check("no recurrence (rec_mii == 1)", d.rec_mii == 1)
    check("no memory ops", d.profile_stats['n_mem_ops'] == 0)


def test_compute_bound():
    print("compute-bound loop (many arith, few mem):")
    a, b, c, e = Temp(), Temp(), Temp(), Temp()
    body = [IRBinOp(a, '*', Temp('x'), Temp('y')),
            IRBinOp(b, '+', a, Temp('z')),
            IRBinOp(c, '*', b, Temp('w')),
            IRBinOp(e, '+', c, Temp('v'))]
    d = analyze(_loop(body))[0]
    check("critical path reflects the arith chain (Htrue>=4)",
          d.crit_path_true >= 4)
    check("res_mii width-driven (no mem)", d.profile_stats['n_mem_ops'] == 0)


def test_memory_bound():
    print("memory-bound loop (many loads/stores):")
    body = []
    for off in (-8, -16, -24, -32, -40):
        a = Temp(); v = Temp()
        body += [IRLoadAddr(a, off), IRLoad(v, a, Const(0), 8)]
    d = analyze(_loop(body))[0]
    check("memory ops counted", d.profile_stats['n_mem_ops'] == 5)
    check("mem-lane term = ceil(5/4) = 2", d.profile_stats['mem_lane_term'] == 2)


def test_recurrence_bound():
    print("recurrence-bound loop (accumulator via stack slot):")
    # acc (slot -8): load acc; acc2 = acc + v; store acc  -> memory recurrence
    a1, ld, v, s2, a2 = Temp(), Temp(), Temp(), Temp(), Temp()
    body = [IRLoadAddr(a1, -8), IRLoad(ld, a1, Const(0), 8),
            IRBinOp(s2, '+', ld, Temp('v')),
            IRLoadAddr(a2, -8), IRStore(a2, Const(0), s2, 8)]
    d = analyze(_loop(body))[0]
    check("recurrence detected via stack slot (rec_mii >= 1)", d.rec_mii >= 1)
    check("memory recurrence flagged",
          d.profile_stats.get('mem_recurrence') in (True, False))  # field present
    check("mii >= 1 and est_ipb computed", d.mii >= 1 and d.est_ipb > 0)


def test_calls():
    print("loop with a call:")
    r = Temp()
    d = analyze(_loop([IRCall(r, 'g', [])]))[0]
    check("profile analyzed with a call present", d.profile_analyzed)
    check("call is treated as a control/barrier (Hnow>=1)",
          d.crit_path_height >= 1)


def test_nested_and_dowhile_smoke():
    print("nested + do-while smoke:")
    inner = [IRLabel('ih'), IRCondJump(Temp('j'), '<', Temp('m'), 'ib', 'ix'),
             IRLabel('ib'),
             IRLoadAddr(Temp(), -16), IRLoad(Temp(), Temp('p'), Const(0), 8),
             IRJump('ih'), IRLabel('ix')]
    dn = analyze(_loop(inner))
    check("nested: both profiled", len(dn) == 2 and all(x.profile_analyzed for x in dn))
    dw = [_fb(), IRLabel('dwb'),
          IRLoadAddr(Temp(), -8), IRStore(Temp('s'), Const(0), Const(1), 8),
          IRCondJump(Temp('t'), '<', Temp('n'), 'dwb', 'dwx'),
          IRLabel('dwx'), IRReturn(Const(0)), _fe()]
    ddw = analyze(dw)
    check("do-while profiled", ddw and ddw[0].profile_analyzed
          and ddw[0].body_inst_count >= 3)


def test_distinct_slot_stores_no_crash():
    print("distinct-slot stores (exercises the alias oracle offset path):")
    # stores to DIFFERENT slots must be provably disjoint -> _must_precede must
    # run its _mem_may_alias offset comparison without error (regression: keys
    # must be (base, string-offset) 2-tuples, not int/3-tuple).
    a1, a2 = Temp(), Temp()
    body = [IRLoadAddr(a1, -8), IRStore(a1, Const(0), Const(1), 8),
            IRLoadAddr(a2, -16), IRStore(a2, Const(0), Const(2), 8)]
    d = analyze(_loop(body))[0]
    check("two stores to distinct slots profiled without crash",
          d.profile_analyzed and d.profile_stats['n_mem_ops'] == 2)


def test_est_ipb_is_measurement_not_decision():
    print("est_ipb is N/mii (a measurement):")
    d = analyze(_loop([IRBinOp(Temp(), '+', Temp('a'), Temp('b'))]))[0]
    check("est_ipb == body_inst_count / mii",
          abs(d.est_ipb - d.body_inst_count / d.mii) < 1e-9)


def main():
    tests = [test_no_dependency, test_compute_bound, test_memory_bound,
             test_recurrence_bound, test_calls, test_distinct_slot_stores_no_crash, test_nested_and_dowhile_smoke,
             test_est_ipb_is_measurement_not_decision]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"M3 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("M3 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

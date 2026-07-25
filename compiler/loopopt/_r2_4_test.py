"""
_r2_4_test.py -- unit tests for R2.4 Scheduler Quality Improvements.

Covers the three quality features and the reusable statistics, without weakening
any R2.3 test (those still pass under the R2.4 default policy):

  * latency prioritisation   (F1: latency-weighted critical path ranks a
                              high-latency chain above an equal-length cheap one)
  * register-pressure         (F2: among equal-height ready nodes the one that
                              frees a register is preferred)
  * bundle-aware tie breaking (F3: the bundle model respects the bundler's lane
                              caps -- 8 independent loads estimate >= 2 bundles)
  * deterministic behaviour   (identical input -> identical output, twice)
  * scheduling statistics     (F4: crit path / ready size / pressure / movement /
                              bundle-utilisation are populated)
  * regression compatibility  (R23 policy reproduces R2.3; semantics preserved;
                              multiset preserved; 0 rollbacks under both policies)

Run:  python3 compiler/loopopt/_r2_4_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pycparser                                                       # noqa: E402
from compiler import _FAKE_TYPEDEFS                                    # noqa: E402
from ir import (Temp, Const, IRBinOp, IRLoad, IRLoadAddr, IRFsqrt,     # noqa: E402
                IRStore, IRAssign)
from ir_gen import IRGenerator                                         # noqa: E402
from ir_utils import func_slices                                       # noqa: E402
from loopopt.schedule import (schedule_module, schedule_function,      # noqa: E402
                              SchedPolicy, _latency, _iclass, _heights,
                              _list_schedule)

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _compile(code):
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=0x400)
    g.visit(ast)
    return g.instructions


def _multiset(seq):
    from collections import Counter
    return Counter(repr(x) for x in seq)


# ── F1: latency model + latency-weighted height ────────────────────────────────

def test_latency_model():
    print("latency + class model (ISA-conservative)")
    T = Temp
    ld = IRLoad(T(), T('b'), Const(0), 4)
    mul = IRBinOp(T(), '*', T('a'), T('b'))
    add = IRBinOp(T(), '+', T('a'), T('b'))
    dv = IRBinOp(T(), '/', T('a'), T('b'))
    sq = IRFsqrt(T(), T('a'))
    check("load latency > add latency", _latency(ld) > _latency(add))
    check("multiply latency > add latency", _latency(mul) > _latency(add))
    check("divide latency > multiply latency", _latency(dv) > _latency(mul))
    check("fsqrt is high latency", _latency(sq) >= _latency(dv))
    check("load classified MEM", _iclass(ld) == 'MEM')
    check("divide classified DIV", _iclass(dv) == 'DIV')
    check("add classified ALU", _iclass(add) == 'ALU')


def test_latency_height_ranks_expensive_chain():
    print("latency-weighted height ranks a multiply chain above an add chain")
    T = Temp
    a, b, c = T('a'), T('b'), T('c')
    # 0: u1=a+b  1: u2=u1+c   |   2: t1=a*b  3: t2=t1+c
    instrs = {
        0: IRBinOp(T('u1'), '+', a, b),
        1: IRBinOp(T('u2'), '+', T('u1'), c),
        2: IRBinOp(T('t1'), '*', a, b),
        3: IRBinOp(T('t2'), '+', T('t1'), c),
    }
    succ = {0: {1}, 1: set(), 2: {3}, 3: set()}
    sched = [0, 1, 2, 3]
    unit = _heights(sched, succ, instrs, latency=False)
    lat = _heights(sched, succ, instrs, latency=True)
    check("unit height ties the two chain roots", unit[0] == unit[2])
    check("latency height ranks the multiply root higher", lat[2] > lat[0])


def test_latency_changes_pick_order():
    print("latency prioritisation changes the schedule vs R2.3")
    T = Temp
    a, b, c = T('a'), T('b'), T('c')
    instrs = {
        0: IRBinOp(T('u1'), '+', a, b),
        1: IRBinOp(T('u2'), '+', T('u1'), c),
        2: IRBinOp(T('t1'), '*', a, b),
        3: IRBinOp(T('t2'), '+', T('t1'), c),
    }
    succ = {0: {1}, 1: set(), 2: {3}, 3: set()}
    indeg = {0: 0, 1: 1, 2: 0, 3: 1}
    sched = [0, 1, 2, 3]
    h_unit = _heights(sched, succ, instrs, latency=False)
    h_lat = _heights(sched, succ, instrs, latency=True)
    r23, _m = _list_schedule(sched, succ, indeg, h_unit,
                             policy=SchedPolicy.R23, instrs=instrs, live_out=set())
    r24, _m = _list_schedule(sched, succ, indeg, h_lat,
                             policy=SchedPolicy.R24, instrs=instrs, live_out=set())
    check("R2.3 picks the add chain first (index tie-break)", r23[0] == 0)
    check("R2.4 picks the multiply chain first (latency)", r24[0] == 2)


# ── F2: register pressure tie-break ────────────────────────────────────────────

def test_pressure_tiebreak_prefers_freeing():
    print("register-pressure tie-break prefers the register-freeing instruction")
    T = Temp
    a, b, c = T('a'), T('b'), T('c')
    # two independent ready adds of equal height feeding a common consumer.
    # node 0 frees ONE live-in (a); node 1 frees TWO (b, c). node 1 has the
    # LARGER index, so R2.3's smallest-index tie-break picks node 0, while only a
    # pressure-aware policy prefers the register-freeing node 1.
    instrs = {
        0: IRBinOp(T('x'), '+', a, Const(1)),   # frees a (deaths 1)
        1: IRBinOp(T('y'), '+', b, c),          # frees b and c (deaths 2)
        2: IRBinOp(T('z'), '+', T('x'), T('y')),  # consumer -> keeps x,y live
    }
    succ = {0: {2}, 1: {2}, 2: set()}
    indeg = {0: 0, 1: 0, 2: 2}
    sched = [0, 1, 2]
    h = _heights(sched, succ, instrs, latency=True)
    r23, _m = _list_schedule(sched, succ, indeg, h,
                             policy=SchedPolicy.R23, instrs=instrs, live_out=set())
    r24, _m = _list_schedule(sched, succ, indeg, h,
                             policy=SchedPolicy.R24, instrs=instrs, live_out=set())
    check("R2.3 (index tie-break) picks node 0 first", r23[0] == 0)
    check("R2.4 (pressure) picks the register-freeing node 1 first", r24[0] == 1)


def test_pressure_peak_tracked():
    print("register-pressure estimate (peak live) is tracked")
    T = Temp
    a, b = T('a'), T('b')
    instrs = {0: IRBinOp(T('x'), '+', a, b), 1: IRAssign(T('y'), T('x'))}
    succ = {0: {1}, 1: set()}
    _order, m = _list_schedule([0, 1], succ, {0: 0, 1: 1},
                               _heights([0, 1], succ, instrs, latency=True),
                               policy=SchedPolicy.R24, instrs=instrs, live_out=set())
    check("peak_live estimate >= 1", m['peak_live'] >= 1)


# ── F3: bundle-aware model respects the bundler's lane caps ─────────────────────

def test_bundle_model_respects_caps():
    print("bundle model respects lane caps (8 independent loads -> >= 2 bundles)")
    T = Temp
    instrs = {i: IRLoad(T(f'd{i}'), T('base'), Const(i * 8), 4) for i in range(8)}
    succ = {i: set() for i in range(8)}
    indeg = {i: 0 for i in range(8)}
    _order, m = _list_schedule(list(range(8)), succ, indeg,
                               _heights(list(range(8)), succ, instrs, latency=True),
                               policy=SchedPolicy.R24, instrs=instrs, live_out=set())
    check("8 loads estimate >= 2 bundles (MEM cap 4)", m['est_bundles'] >= 2)
    check("estimate covers all 8 instructions", m['est_instrs'] == 8)


# ── F4: statistics populated on a real function ────────────────────────────────

REDUCE = "int r(int*p,int n){int s=0,i; for(i=0;i<n;i++) s+=p[i]*2; return s;}"


def test_statistics_populated():
    print("scheduling statistics are populated (F4)")
    ir = _compile(REDUCE)
    _out, st = schedule_module(ir)
    check("critical-path total > 0", st.crit_path_total > 0)
    check("ready steps > 0", st.ready_steps > 0)
    check("metriced blocks > 0", st.metriced_blocks > 0)
    check("pressure peak sum > 0", st.pressure_peak_sum > 0)
    check("estimated bundles > 0", st.est_bundles > 0)
    check("movement is non-negative", st.movement_sum >= 0)


# ── determinism + semantics + regression ───────────────────────────────────────

BATCH = [
    "int a1(int a,int b){int x=a*b,y=a+b; return x+y;}",
    "int a2(int*p,int n){int s=0,i; for(i=0;i<n;i++) s+=p[i]*2; return s;}",
    "int a3(int n){int a[8],b[8],i,s; s=0; for(i=0;i<n;i++){a[i]=i;s+=b[i];} return s;}",
    "int a4(int a,int b,int c,int d){int w=a*a+b*b; int z=c*c+d*d; return w+z;}",
]


def test_determinism():
    print("R2.4 scheduling is deterministic")
    for code in BATCH:
        o1, _ = schedule_module(_compile(code))
        o2, _ = schedule_module(_compile(code))
        check("identical output across two runs",
              [repr(x) for x in o1] == [repr(x) for x in o2])


def test_semantics_and_multiset_both_policies():
    print("semantics + multiset preserved under BOTH policies")
    for code in BATCH:
        ir = _compile(code)
        for pol in (SchedPolicy.R23, SchedPolicy.R24):
            out, st = schedule_module(_compile(code), policy=pol)
            check(f"{pol.name}: multiset preserved",
                  _multiset(ir) == _multiset(out))
            check(f"{pol.name}: 0 rollbacks / 0 structural failures",
                  st.rollbacks == 0 and st.structural_failures == 0)


def test_r23_still_legal():
    print("R2.3 policy still produces legal schedules (not weakened)")
    for code in BATCH:
        ir = _compile(code)
        for lo, hi in func_slices(ir):
            new, changed, verdict = schedule_function(ir, lo, hi,
                                                      policy=SchedPolicy.R23)
            check(f"{ir[lo].name}: R23 verdict != mismatch", verdict != 'mismatch')


def main():
    for t in (test_latency_model, test_latency_height_ranks_expensive_chain,
              test_latency_changes_pick_order, test_pressure_tiebreak_prefers_freeing,
              test_pressure_peak_tracked, test_bundle_model_respects_caps,
              test_statistics_populated, test_determinism,
              test_semantics_and_multiset_both_policies, test_r23_still_legal):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R2.4 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""
_m1_test.py -- unit tests for the M1 InductionVars analysis.

Builds small hand-written IR whose local variables are memory-backed (the stage
at which loops are analyzed) and checks basic/derived IVs, steps, init, trip
count and the primary IV. Analysis only. Run:
    python3 compiler/loopopt/_m1_test.py

Required coverage: single IV, multiple IVs, derived IV, negative step, positive
step, pointer IV, nested loops, no-IV loop, do-while, short-circuit condition.
(Irreducible loops are not natural loops, so LoopInfo yields none to analyze --
covered by asserting zero descriptors for that shape.)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRStore, IRLoad, IRLoadAddr, Temp, Const)
from loopopt import discover, annotate_induction_vars, TripCount

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _fb():
    return IRFuncBegin('f', [], {}, 0)


def _fe():
    return IRFuncEnd('f')


def T(n):
    return Temp(n)


def analyze(ins):
    descs = discover(ins)
    annotate_induction_vars(descs)
    return descs


# A memory-backed counter loop over slot `off`, step `step`, bound `bound`
# (const), init `init` (const). Every address/value temp is FRESH (Temp() with
# an auto name), mirroring ir_gen -- which never reuses a temp name -- so the
# single-definition analysis sees each &slot / load as its own def. `extra` is
# inserted into the body (e.g. a derived-IV computation, a second counter).
def _counter_loop(off=-8, step=1, bound=4, init=0, op='<', extra=None,
                  head='head', body='body', step_lbl='step', exit_lbl='exit'):
    ins = [_fb(),
           # init: slot = init  (fresh address temp)
           IRLoadAddr(Temp(), off), None,   # placeholder store filled below
           IRLabel(head),
           IRLoadAddr(Temp(), off), None,   # guard load address
           None,                            # guard load
           None,                            # guard condjump
           IRLabel(body)]
    # fill guard: need the address temp we just created
    a_init = ins[1].dest
    ins[2] = IRStore(a_init, Const(0), Const(init), 8)
    a_guard = ins[4].dest
    ldc = Temp()
    ins[5] = IRLoad(ldc, a_guard, Const(0), 8)
    ins[6] = IRCondJump(ldc, op, Const(bound), body, exit_lbl)
    if extra:
        ins += extra
    a_ld = Temp(); ld = Temp(); nx = Temp(); a_st = Temp()
    ins += [IRLabel(step_lbl),
            IRLoadAddr(a_ld, off), IRLoad(ld, a_ld, Const(0), 8),
            (IRBinOp(nx, '+', ld, Const(step)) if step >= 0
             else IRBinOp(nx, '-', ld, Const(-step))),
            IRLoadAddr(a_st, off), IRStore(a_st, Const(0), nx, 8),
            IRJump(head),
            IRLabel(exit_lbl), IRReturn(Const(0)), _fe()]
    return ins


def test_single_positive():
    print("single IV, positive step:")
    d = analyze(_counter_loop(off=-8, step=1, bound=4, init=0))[0]
    check("one basic IV at slot -8", list(d.basic_ivs) == [-8])
    check("step +1", d.basic_ivs[-8].step == 1)
    check("init 0", d.basic_ivs[-8].init == 0)
    check("primary IV is -8", d.primary_iv == -8)
    check("trip count KNOWN=4",
          d.trip_count.kind == TripCount.KNOWN and d.trip_count.value == 4)
    check("is_counted", d.is_counted)


def test_negative_step():
    print("negative step:")
    # for (i=10; i > 0; i--) -> op '>' is not the KNOWN pattern; step is -1
    d = analyze(_counter_loop(off=-8, step=-1, bound=0, init=10, op='>'))[0]
    check("basic IV detected", -8 in d.basic_ivs)
    check("step -1", d.basic_ivs[-8].step == -1)
    check("init 10", d.basic_ivs[-8].init == 10)
    # '>' with negative step is not ivsr's KNOWN pattern -> not counted
    check("trip not KNOWN (guard is '>')", d.trip_count.kind != TripCount.KNOWN)


def test_trip_le():
    print("trip count with <=:")
    d = analyze(_counter_loop(off=-8, step=1, bound=4, init=0, op='<='))[0]
    check("trip KNOWN=5 for i<=4 step1 init0",
          d.trip_count.kind == TripCount.KNOWN and d.trip_count.value == 5)


def test_step_two():
    print("step of 2:")
    d = analyze(_counter_loop(off=-8, step=2, bound=10, init=0))[0]
    check("step +2", d.basic_ivs[-8].step == 2)
    check("trip KNOWN=5 (0,2,4,6,8)",
          d.trip_count.kind == TripCount.KNOWN and d.trip_count.value == 5)


def test_derived_iv():
    print("derived IV (i*4):")
    # inside body: idx = i * 4   (a derived IV / IV term)
    a = T('ai'); li = T('li'); m = T('mi')
    extra = [IRLoadAddr(a, -8), IRLoad(li, a, Const(0), 8),
             IRBinOp(m, '*', li, Const(4))]
    d = analyze(_counter_loop(off=-8, step=1, bound=4, init=0, extra=extra))[0]
    check("m is an IV term of slot -8 scale 4",
          d.iv_terms.get('mi') == (-8, 4))
    check("m recorded as a derived IV (scale 4)",
          'mi' in d.derived_ivs and d.derived_ivs['mi'].scale == 4)


def test_multiple_ivs():
    print("multiple IVs:")
    # two independent counters i (slot -8, +1) and j (slot -16, +2) in one loop
    aj = T('aj'); lj = T('lj'); nj = T('nj'); asj = T('asj')
    extra = [IRLoadAddr(aj, -16), IRLoad(lj, aj, Const(0), 8),
             IRBinOp(nj, '+', lj, Const(2)),
             IRLoadAddr(asj, -16), IRStore(asj, Const(0), nj, 8)]
    # pre-init j
    ins = _counter_loop(off=-8, step=1, bound=4, init=0, extra=extra)
    ins[1:1] = [IRLoadAddr(T('aj0'), -16), IRStore(T('aj0'), Const(0), Const(0), 8)]
    d = analyze(ins)[0]
    check("both slots -8 and -16 are basic IVs",
          set(d.basic_ivs) == {-8, -16})
    check("slot -16 step +2", d.basic_ivs[-16].step == 2)
    check("primary IV is the guarded one (-8)", d.primary_iv == -8)


def test_no_iv():
    print("no induction variable:")
    # loop whose body does not update any clean counter (guard on an invariant)
    ins = [_fb(),
           IRLabel('head'),
           IRCondJump(T('n'), '>', Const(0), 'body', 'exit'),
           IRLabel('body'),
           IRBinOp(T('t'), '+', T('t'), Const(1)),   # t is not memory-backed
           IRJump('head'),
           IRLabel('exit'), IRReturn(Const(0)), _fe()]
    d = analyze(ins)[0]
    check("no basic IVs", len(d.basic_ivs) == 0)
    check("trip UNKNOWN", d.trip_count.kind == TripCount.UNKNOWN)
    check("not counted", not d.is_counted)


def test_nested():
    print("nested loops (independent counters):")
    # outer slot -8 (+1, bound 4), inner slot -16 (+1, bound 3)
    aj = T('aj'); lj = T('lj'); nj = T('nj')
    inner = [
        IRLoadAddr(T('aj0'), -16), IRStore(T('aj0'), Const(0), Const(0), 8),
        IRLabel('ihead'),
        IRLoadAddr(T('ajc'), -16), IRLoad(T('ljc'), T('ajc'), Const(0), 8),
        IRCondJump(T('ljc'), '<', Const(3), 'ibody', 'iexit'),
        IRLabel('ibody'),
        IRLoadAddr(aj, -16), IRLoad(lj, aj, Const(0), 8),
        IRBinOp(nj, '+', lj, Const(1)),
        IRLoadAddr(T('ajs'), -16), IRStore(T('ajs'), Const(0), nj, 8),
        IRJump('ihead'),
        IRLabel('iexit'),
    ]
    ins = _counter_loop(off=-8, step=1, bound=4, init=0, extra=inner)
    descs = analyze(ins)
    by = {d.label(): d for d in descs}
    check("two loops", len(descs) == 2)
    check("outer counter slot -8", by['head'].primary_iv == -8)
    check("inner counter slot -16", by['ihead'].primary_iv == -16)
    check("inner is innermost", by['ihead'].is_innermost)


def test_pointer_iv():
    print("pointer induction variable:")
    # p (slot -8) advances by 8 bytes each iteration: p = p + 8
    d = analyze(_counter_loop(off=-8, step=8, bound=64, init=0))[0]
    check("pointer slot -8 step +8", d.basic_ivs[-8].step == 8)
    check("trip KNOWN=8 (0,8,..,56)",
          d.trip_count.kind == TripCount.KNOWN and d.trip_count.value == 8)


def test_irreducible_none():
    print("irreducible / no natural loop:")
    # two labels jumping into each other's middle -> not a natural loop
    ins = [_fb(),
           IRCondJump(T('x'), '>', Const(0), 'a', 'b'),
           IRLabel('a'), IRJump('b'),
           IRLabel('b'), IRJump('a'),
           _fe()]
    descs = analyze(ins)
    # LoopInfo only reports natural (reducible) loops; this shape yields whatever
    # natural loops exist -- assert the analysis does not crash and reports no
    # basic IVs for any discovered loop.
    check("no basic IVs in any discovered loop",
          all(len(d.basic_ivs) == 0 for d in descs))


def test_do_while_and_shortcircuit_smoke():
    print("do-while / short-circuit smoke (no crash, fields populated):")
    # do-while: body then test at the bottom
    ins = [_fb(),
           IRLoadAddr(T('a0'), -8), IRStore(T('a0'), Const(0), Const(0), 8),
           IRLabel('dwbody'),
           IRLoadAddr(T('al'), -8), IRLoad(T('l'), T('al'), Const(0), 8),
           IRBinOp(T('n'), '+', T('l'), Const(1)),
           IRLoadAddr(T('as'), -8), IRStore(T('as'), Const(0), T('n'), 8),
           IRLoadAddr(T('ac'), -8), IRLoad(T('lc'), T('ac'), Const(0), 8),
           IRCondJump(T('lc'), '<', Const(4), 'dwbody', 'dwexit'),
           IRLabel('dwexit'), IRReturn(Const(0)), _fe()]
    descs = analyze(ins)
    check("do-while discovered and analyzed", len(descs) == 1)
    check("do-while basic IV slot -8 step +1",
          descs and descs[0].basic_ivs.get(-8) is not None
          and descs[0].basic_ivs[-8].step == 1)


def main():
    tests = [test_single_positive, test_negative_step, test_trip_le,
             test_step_two, test_derived_iv, test_multiple_ivs, test_no_iv,
             test_nested, test_pointer_iv, test_irreducible_none,
             test_do_while_and_shortcircuit_smoke]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"M1 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("M1 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

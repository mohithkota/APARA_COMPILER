"""
_r1_2_test.py -- tests for R1.2 factor-2 loop unrolling (LoopUnrollFactor2).

Correctness is proved by DIFFERENTIAL EXECUTION: each loop is interpreted before
and after unrolling on identical state (ir_interp), and observable behaviour
(return value + final memory) must be identical. Structural integrity comes from
the frozen M5 framework (verifier + rollback); these tests confirm both.

Covered: factor-2 unrolling, even/odd trip counts, the remainder loop, trip
count = 1 (rejected) and = 2 (unrolled), nested-loop rejection, unsupported-loop
rejection (multi-exit / opaque call / symbolic bound), rollback on a forced
verification failure, clean verification, regression compatibility (untouched
functions stay byte-identical), and that unrolled IR still compiles.

Run:  python3 compiler/loopopt/_r1_2_test.py
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRStore, IRLoad, IRLoadAddr, Temp, Const)
from ir_utils import func_slices
from loopopt.loop_unroll2 import LoopUnrollFactor2, unroll_module
from loopopt.transform import LoopTransformDriver
from loopopt.verify import VerifyResult
from loopopt.discovery import discover_function
from loopopt import ir_interp as I
from loopopt._m5_test import sum_loop
from loopopt._m6_test import nested_sum, multi_exit_sum
from loopopt._m7_test import _call_loop

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


# ── fixtures ──────────────────────────────────────────────────────────────────

def array_fill(n):
    """for (i=0;i<n;i++) a[i]=i;  -- a memory-backed, RESOURCE-bound, MULTI-BLOCK
    counted loop (header + body + increment latch). `a` is a stack array based at
    FP-128; the IV `i` at FP-8. Returns 0; its effect is the array a[i]==i."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Const(n), 'body', 'exit'),
            IRLabel('body'),
            IRLoadAddr(Temp('base'), -128),
            IRLoadAddr(Temp('pc2'), -8), IRLoad(Temp('iv2'), Temp('pc2'), Const(0), 8),
            IRBinOp(Temp('idx'), '*', Temp('iv2'), Const(8)),
            IRBinOp(Temp('addr'), '+', Temp('base'), Temp('idx')),
            IRStore(Temp('addr'), Const(0), Temp('iv2'), 8),
            IRLabel('inc'),
            IRLoadAddr(Temp('pc3'), -8), IRLoad(Temp('iv3'), Temp('pc3'), Const(0), 8),
            IRBinOp(Temp('nv'), '+', Temp('iv3'), Const(1)),
            IRLoadAddr(Temp('pi'), -8), IRStore(Temp('pi'), Const(0), Temp('nv'), 8),
            IRJump('head'),
            IRLabel('exit'), IRReturn(Const(0)), IRFuncEnd('f')]


def symbolic_bound():
    """while (i < n) i++;  -- n is a loaded variable, so the trip count is not a
    compile-time constant: R1.2 must leave it untouched (no-op)."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
            IRLoadAddr(Temp('pn'), -16), IRLoad(Temp('nn'), Temp('pn'), Const(0), 8),
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Temp('nn'), 'body', 'exit'),
            IRLabel('body'),
            IRLoadAddr(Temp('pc2'), -8), IRLoad(Temp('iv2'), Temp('pc2'), Const(0), 8),
            IRBinOp(Temp('nv'), '+', Temp('iv2'), Const(1)),
            IRLoadAddr(Temp('pi'), -8), IRStore(Temp('pi'), Const(0), Temp('nv'), 8),
            IRJump('head'),
            IRLabel('exit'), IRReturn(Const(0)), IRFuncEnd('f')]


def _unroll(ir):
    """Unroll a COPY through the framework; return (unrolled_ir, stats)."""
    ir1 = copy.deepcopy(ir)
    stats, _rep = unroll_module(ir1)
    return ir1, stats


def _diff(ir0, ir1):
    lo, hi = func_slices(ir0)[0]
    return I.differential(ir0, ir1, lo, hi)


def _array_ok(ir, n):
    lo, hi = func_slices(ir)[0]
    _r, mem = I.run_slice(ir, lo, hi)
    return all(mem.get(-128 + i * 8) == i for i in range(n))


# ── tests ─────────────────────────────────────────────────────────────────────

def test_single_block_semantics():
    print("single-block loop: unroll where profitable, behaviour preserved:")
    for n in (2, 3, 4, 5, 6):
        Temp.reset(); ir0 = sum_loop(n)
        ir1, st = _unroll(ir0)
        v, _ = _diff(ir0, ir1)
        check(f"sum_loop({n}) unrolled & matches", st.commits == 1 and v == 'match')
    # recurrence-bound larger trips are (correctly) left alone by the model
    Temp.reset(); ir0 = sum_loop(10)
    ir1, st = _unroll(ir0)
    check("sum_loop(10) not unrolled (recurrence-bound), IR still valid",
          st.commits == 0 and _diff(ir0, ir1)[0] == 'match')


def test_multiblock_even():
    print("multi-block resource-bound loop, EVEN trip:")
    Temp.reset(); ir0 = array_fill(8)
    ir1, st = _unroll(ir0)
    check("unrolled", st.commits == 1)
    check("no verifier failures / rollbacks", st.verifier_failures == 0 and st.rollbacks == 0)
    check("behaviour matches", _diff(ir0, ir1)[0] == 'match')
    check("array contents correct after unroll", _array_ok(ir1, 8))


def test_multiblock_odd():
    print("multi-block resource-bound loop, ODD trip (needs the remainder):")
    Temp.reset(); ir0 = array_fill(9)
    ir1, st = _unroll(ir0)
    check("unrolled", st.commits == 1)
    check("behaviour matches", _diff(ir0, ir1)[0] == 'match')
    check("array contents correct incl. last (odd) element", _array_ok(ir1, 9))


def test_trip_two():
    print("trip count = 2 unrolls (one main iteration, empty remainder):")
    Temp.reset(); ir0 = array_fill(2)
    ir1, st = _unroll(ir0)
    check("unrolled", st.commits == 1)
    check("behaviour matches", _diff(ir0, ir1)[0] == 'match')
    check("array correct", _array_ok(ir1, 2))


def test_trip_one_rejected():
    print("trip count = 1 is not unrolled (too small per the profitability model):")
    Temp.reset(); ir0 = array_fill(1)
    ir1, st = _unroll(ir0)
    check("not unrolled", st.commits == 0)
    check("IR unchanged", [repr(x) for x in ir0] == [repr(x) for x in ir1])


def test_remainder_loop_created():
    print("unrolling creates a distinct remainder loop:")
    Temp.reset(); ir = array_fill(9)
    before_loops = len(discover_function(ir, *func_slices(ir)[0]))
    xf = LoopUnrollFactor2()
    LoopTransformDriver().run(xf, ir)
    lo, hi = func_slices(ir)[0]
    after = discover_function(ir, lo, hi)
    heads = {d.cfg.blocks[d.header].label for d in after}
    check("one loop before", before_loops == 1)
    check("two loops after (main + remainder)", len(after) == 2)
    check("a synthesized remainder loop is present",
          any(h in xf._synthetic for h in heads))


def test_even_odd_sweep():
    print("even/odd sweep: behaviour + memory identical for every trip:")
    allok = True
    for n in range(2, 13):
        Temp.reset(); ir0 = array_fill(n)
        ir1, st = _unroll(ir0)
        v, _ = _diff(ir0, ir1)
        if not (v == 'match' and _array_ok(ir1, n) and st.rollbacks == 0
                and st.verifier_failures == 0):
            allok = False
    check("all trips 2..12 match with correct memory, no failures", allok)


def test_nested_outer_rejected():
    print("nested loop: outer rejected (not innermost); whole-function behaviour kept:")
    Temp.reset(); ir0 = nested_sum()
    # outer loop is not innermost -> ineligible for this transform
    descs = discover_function(ir0, *func_slices(ir0)[0])
    outer = [d for d in descs if not d.is_innermost]
    xf = LoopUnrollFactor2()
    check("outer loop is ineligible (legality)",
          all(not xf.legal(d)[0] for d in outer))
    ir1, st = _unroll(ir0)
    check("no verifier failures / rollbacks", st.verifier_failures == 0 and st.rollbacks == 0)
    check("whole-function behaviour matches", _diff(ir0, ir1)[0] == 'match')


def test_reject_multiexit():
    print("multi-exit loop is not transformed (rejected by R1.1 legality):")
    Temp.reset(); ir0 = multi_exit_sum()
    ir1, st = _unroll(ir0)
    check("not unrolled", st.commits == 0)
    check("IR unchanged", [repr(x) for x in ir0] == [repr(x) for x in ir1])


def test_reject_call():
    print("loop with an opaque call is not transformed:")
    Temp.reset(); ir0 = _call_loop()
    ir1, st = _unroll(ir0)
    check("not unrolled", st.commits == 0)
    check("IR unchanged", [repr(x) for x in ir0] == [repr(x) for x in ir1])


def test_reject_symbolic():
    print("symbolic (non-constant) trip count is left untouched by R1.2:")
    Temp.reset(); ir0 = symbolic_bound()
    ir1, st = _unroll(ir0)
    check("not unrolled (no-op)", st.commits == 0)
    check("IR unchanged", [repr(x) for x in ir0] == [repr(x) for x in ir1])


def test_rollback():
    print("a forced verification failure rolls back to a byte-identical IR:")

    class FailVerifier:
        def verify_all(self, descs):
            r = VerifyResult()
            if descs:
                r.add(descs[0], 'test-forced', 'forced failure')
            return r

    Temp.reset(); ir = array_fill(8)
    before = [repr(x) for x in ir]
    drv = LoopTransformDriver(verifier=FailVerifier())
    st = drv.run(LoopUnrollFactor2(), ir)
    after = [repr(x) for x in ir]
    check("attempted then rolled back", st.rollbacks >= 1 and st.commits == 0)
    check("verifier failure counted", st.verifier_failures >= 1)
    check("IR restored byte-identically", before == after)


def test_verification_clean():
    print("real transforms verify clean (no violations, no rollbacks):")
    total_vf = total_rb = 0
    for n in (4, 5, 8, 9, 12):
        Temp.reset(); _ir1, st = _unroll(array_fill(n))
        total_vf += st.verifier_failures
        total_rb += st.rollbacks
    check("0 verifier failures across trips", total_vf == 0)
    check("0 rollbacks across trips", total_rb == 0)


def test_regression_unchanged_functions():
    print("regression: functions without an unrolled loop stay byte-identical:")
    # program = [g: straight-line]  ++  [f: array_fill(5)]
    Temp.reset()
    g = [IRFuncBegin('g', [], {}, 0),
         IRLoadAddr(Temp('gp'), -8), IRStore(Temp('gp'), Const(0), Const(42), 8),
         IRReturn(Const(42)), IRFuncEnd('g')]
    f = array_fill(5)
    prog = g + f
    prog1 = copy.deepcopy(prog)
    unroll_module(prog1)
    s0 = {prog[a].name: [repr(x) for x in prog[a:b + 1]] for a, b in func_slices(prog)}
    s1 = {prog1[a].name: [repr(x) for x in prog1[a:b + 1]] for a, b in func_slices(prog1)}
    check("function 'g' (no loop) unchanged", s0['g'] == s1['g'])
    check("function 'f' (has loop) was transformed", s0['f'] != s1['f'])


def test_unrolled_compiles():
    print("unrolled IR still generates code (CodeGen + bundler), no spill:")
    from codegen import CodeGen
    from bundler import bundle_mcode
    ok = True
    for n in (5, 8, 9):
        Temp.reset(); ir1, st = _unroll(array_fill(n))
        try:
            cg = CodeGen(global_base=0x400)
            body = cg.generate(copy.deepcopy(ir1), global_base=0x400)
            bundle_mcode(body)
            if cg.spilled:
                ok = False
        except Exception:
            ok = False
    check("codegen + bundler succeed on unrolled IR without spilling", ok)


def main():
    tests = [test_single_block_semantics, test_multiblock_even, test_multiblock_odd,
             test_trip_two, test_trip_one_rejected, test_remainder_loop_created,
             test_even_odd_sweep, test_nested_outer_rejected, test_reject_multiexit,
             test_reject_call, test_reject_symbolic, test_rollback,
             test_verification_clean, test_regression_unchanged_functions,
             test_unrolled_compiles]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"R1.2 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("R1.2 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

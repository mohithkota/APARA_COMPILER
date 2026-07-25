"""
_r1_3_test.py -- tests for R1.3 improvements to factor-2 unrolling
(LoopUnrollFactor2R13). Extends the R1.2 tests (which remain, unchanged); this
adds coverage for the four R1.3 improvements while re-proving correctness by
differential execution.

Covered: IV substitution (no cross-copy reload; behaviour identical), symbolic
bounds (preheader arithmetic), dead-remainder elimination (known even trips ->
no remainder loop), compile-time even & odd trips, rollback, verification,
regression compatibility, and that improved IR still compiles.

Run:  python3 compiler/loopopt/_r1_3_test.py
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRStore, IRLoad, IRLoadAddr, Temp, Const)
from ir_utils import func_slices
from loopopt.loop_unroll3 import LoopUnrollFactor2R13, unroll_module
from loopopt.loop_unroll2 import unroll_module as unroll_module_r12
from loopopt.transform import LoopTransformDriver
from loopopt.verify import VerifyResult
from loopopt.discovery import discover_function
from loopopt import ir_interp as I
from loopopt._m5_test import sum_loop
from loopopt._m6_test import nested_sum, multi_exit_sum

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


# ── fixtures ──────────────────────────────────────────────────────────────────

def array_fill(n, symbolic=False):
    """for (i=0;i<BOUND;i++) a[i]=i; multi-block, resource-bound. When symbolic,
    BOUND is a loop-invariant temp loaded from FP-16 (seed it with the trip)."""
    bound = Temp('nn') if symbolic else Const(n)
    pre = ([IRLoadAddr(Temp('pn'), -16), IRLoad(Temp('nn'), Temp('pn'), Const(0), 8)]
           if symbolic else [])
    return [IRFuncBegin('f', [], {}, 0),
            IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8)] + pre + [
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', bound, 'body', 'exit'),
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


def _unroll(ir):
    ir1 = copy.deepcopy(ir)
    stats, _rep = unroll_module(ir1)
    return ir1, stats


def _diff(ir0, ir1, seed=None):
    """Differential over each program's OWN function slice."""
    lo0, hi0 = func_slices(ir0)[0]
    lo1, hi1 = func_slices(ir1)[0]
    seed = seed or {}
    g0 = I._preload_globals(ir0); g0.update(seed)
    g1 = I._preload_globals(ir1); g1.update(seed)
    try:
        r0, m0 = I.run_slice(ir0, lo0, hi0, init_mem=g0)
        r1, m1 = I.run_slice(ir1, lo1, hi1, init_mem=g1)
    except (I.Unsupported, I.StepLimit):
        return None
    return r0 == r1 and m0 == m1


def _array_ok(ir, n, seed=None):
    lo, hi = func_slices(ir)[0]
    g = I._preload_globals(ir); g.update(seed or {})
    _r, mem = I.run_slice(ir, lo, hi, init_mem=g)
    return all(mem.get(-128 + i * 8) == i for i in range(n))


def _n_loops(ir):
    return len(discover_function(ir, *func_slices(ir)[0]))


def _count(ir, cls):
    return sum(1 for x in ir if type(x).__name__ == cls)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_correctness_const():
    print("const-bound loops: unrolled behaviour identical (even & odd):")
    for n in (2, 3, 4, 5, 8, 9, 12, 13):
        Temp.reset(); ir0 = array_fill(n)
        ir1, st = _unroll(ir0)
        ok = st.commits == 1 and _diff(ir0, ir1) and _array_ok(ir1, n)
        check(f"array_fill({n}) correct", ok and st.rollbacks == 0 and st.verifier_failures == 0)


def test_correctness_single_block():
    print("single-block loop still correct under R1.3 (IV substitution path):")
    allok = True
    for n in (2, 3, 4, 5, 6):
        Temp.reset(); ir0 = sum_loop(n)
        ir1, st = _unroll(ir0)
        if not (st.commits == 1 and _diff(ir0, ir1)):
            allok = False
    check("sum_loop 2..6 unrolled & identical", allok)


def test_iv_substitution():
    print("IV substitution removes the cross-copy reload (fewer loads than R1.2):")
    # odd trip so BOTH R1.2 and R1.3 keep a remainder -> the only difference is
    # the substitution/cleanup, isolating its effect.
    Temp.reset(); ir_r12 = copy.deepcopy(array_fill(9)); unroll_module_r12(ir_r12)
    Temp.reset(); ir_r13 = copy.deepcopy(array_fill(9)); unroll_module(ir_r13)
    check("R1.3 has strictly fewer IV loads than R1.2",
          _count(ir_r13, 'IRLoad') < _count(ir_r12, 'IRLoad'))
    check("R1.3 has fewer address computations (cleanup)",
          _count(ir_r13, 'IRLoadAddr') < _count(ir_r12, 'IRLoadAddr'))
    # correctness preserved
    Temp.reset(); ir0 = array_fill(9); ir1, _ = _unroll(ir0)
    check("behaviour still identical", _diff(ir0, ir1) and _array_ok(ir1, 9))


def test_dead_remainder_even():
    print("dead-remainder elimination: known EVEN trip -> no remainder loop:")
    for n in (4, 8, 12):
        Temp.reset(); ir = array_fill(n)
        _u, st = _unroll(ir)
        # transform in place to inspect loop count
        Temp.reset(); ir2 = array_fill(n); LoopTransformDriver().run(LoopUnrollFactor2R13(), ir2)
        check(f"trip {n}: exactly one loop remains (remainder omitted)", _n_loops(ir2) == 1)
        check(f"trip {n}: behaviour correct", _diff(array_fill(n), ir2) and _array_ok(ir2, n))


def test_remainder_kept_odd():
    print("known ODD trip keeps the remainder loop (correctly):")
    for n in (5, 9, 13):
        Temp.reset(); ir = array_fill(n)
        LoopTransformDriver().run(LoopUnrollFactor2R13(), ir)
        check(f"trip {n}: two loops (main + remainder)", _n_loops(ir) == 2)
        check(f"trip {n}: behaviour correct", _diff(array_fill(n), ir) and _array_ok(ir, n))


def test_symbolic_bounds():
    print("symbolic (loop-invariant) bound: unrolled with preheader arithmetic:")
    allok = True
    for n in (0, 1, 2, 3, 4, 7, 8, 11):
        Temp.reset(); ir0 = array_fill(n, symbolic=True)
        ir1, st = _unroll(ir0)
        ok = (st.commits == 1 and _diff(ir0, ir1, seed={-16: n})
              and _array_ok(ir1, n, seed={-16: n}) and st.rollbacks == 0)
        if not ok:
            allok = False
    check("symbolic array loop correct for trips 0..11 (odd & even)", allok)
    # the preheader must contain the (bound - step) computation, once
    Temp.reset(); ir = array_fill(5, symbolic=True)
    LoopTransformDriver().run(LoopUnrollFactor2R13(), ir)
    lo, hi = func_slices(ir)[0]
    d = next(x for x in discover_function(ir, lo, hi)
             if ir[x.cfg.blocks[x.header].lo].name == 'head')
    ph = d.cfg.blocks[d.preheader]
    has_sub = any(type(ir[i]).__name__ == 'IRBinOp' and ir[i].op == '-'
                  for i in range(ph.lo, ph.hi + 1))
    check("preheader computes (bound - step) once", has_sub)


def test_reject_unsupported():
    print("unsupported loops are still rejected / left untouched:")
    for name, fac in (('multi_exit', multi_exit_sum),):
        Temp.reset(); ir0 = fac()
        ir1, st = _unroll(ir0)
        check(f"{name}: not transformed, IR unchanged",
              st.commits == 0 and [repr(x) for x in ir0] == [repr(x) for x in ir1])
    # trip==1 rejected by the (unchanged) profitability model
    Temp.reset(); ir0 = array_fill(1)
    ir1, st = _unroll(ir0)
    check("trip==1 not unrolled", st.commits == 0)


def test_nested_outer_rejected():
    print("nested loop: outer still rejected, whole-function behaviour preserved:")
    Temp.reset(); ir0 = nested_sum()
    ir1, st = _unroll(ir0)
    check("no verifier failures / rollbacks", st.verifier_failures == 0 and st.rollbacks == 0)
    check("behaviour matches", _diff(ir0, ir1))


def test_rollback():
    print("forced verification failure rolls back to byte-identical IR:")

    class FailVerifier:
        def verify_all(self, descs):
            r = VerifyResult()
            if descs:
                r.add(descs[0], 'test-forced', 'forced failure')
            return r

    Temp.reset(); ir = array_fill(8)
    before = [repr(x) for x in ir]
    st = LoopTransformDriver(verifier=FailVerifier()).run(LoopUnrollFactor2R13(), ir)
    after = [repr(x) for x in ir]
    check("rolled back", st.rollbacks >= 1 and st.commits == 0)
    check("IR restored byte-identically", before == after)


def test_verification_clean():
    print("real transforms verify clean:")
    vf = rb = 0
    for n in (4, 5, 8, 9, 12):
        Temp.reset(); _ir, st = _unroll(array_fill(n))
        vf += st.verifier_failures; rb += st.rollbacks
    for n in (4, 7, 8):
        Temp.reset(); _ir, st = _unroll(array_fill(n, symbolic=True))
        vf += st.verifier_failures; rb += st.rollbacks
    check("0 verifier failures", vf == 0)
    check("0 rollbacks", rb == 0)


def test_regression_unchanged_functions():
    print("regression: functions without an unrolled loop stay byte-identical:")
    Temp.reset()
    g = [IRFuncBegin('g', [], {}, 0),
         IRLoadAddr(Temp('gp'), -8), IRStore(Temp('gp'), Const(0), Const(42), 8),
         IRReturn(Const(42)), IRFuncEnd('g')]
    prog = g + array_fill(5)
    prog1 = copy.deepcopy(prog)
    unroll_module(prog1)
    s0 = {prog[a].name: [repr(x) for x in prog[a:b + 1]] for a, b in func_slices(prog)}
    s1 = {prog1[a].name: [repr(x) for x in prog1[a:b + 1]] for a, b in func_slices(prog1)}
    check("function 'g' unchanged", s0['g'] == s1['g'])
    check("function 'f' transformed", s0['f'] != s1['f'])


def test_improved_compiles():
    print("R1.3 IR still generates code (CodeGen + bundler), no spill:")
    from codegen import CodeGen
    from bundler import bundle_mcode
    ok = True
    for n in (4, 5, 8, 9):
        Temp.reset(); ir1, _ = _unroll(array_fill(n))
        try:
            cg = CodeGen(global_base=0x400)
            bundle_mcode(cg.generate(copy.deepcopy(ir1), global_base=0x400))
            if cg.spilled:
                ok = False
        except Exception:
            ok = False
    # symbolic too
    Temp.reset(); ir1, _ = _unroll(array_fill(6, symbolic=True))
    try:
        cg = CodeGen(global_base=0x400)
        bundle_mcode(cg.generate(copy.deepcopy(ir1), global_base=0x400))
        if cg.spilled:
            ok = False
    except Exception:
        ok = False
    check("codegen + bundler succeed without spilling", ok)


def main():
    tests = [test_correctness_const, test_correctness_single_block, test_iv_substitution,
             test_dead_remainder_even, test_remainder_kept_odd, test_symbolic_bounds,
             test_reject_unsupported, test_nested_outer_rejected, test_rollback,
             test_verification_clean, test_regression_unchanged_functions,
             test_improved_compiles]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"R1.3 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("R1.3 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

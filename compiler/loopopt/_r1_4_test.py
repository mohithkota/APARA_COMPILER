"""
_r1_4_test.py -- tests for R1.4 (LoopUnrollFactorN): factors 2/4/8, automatic
factor selection, wider (header-defined, invariant) symbolic bounds, remainder
handling per factor, and generalised dead-remainder elimination -- all with the
R1.3 quality wins active. Extends the R1.1/R1.2/R1.3 suites (which remain,
unchanged). Correctness is re-proved by differential execution.

Run:  python3 compiler/loopopt/_r1_4_test.py
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRStore, IRLoad, IRLoadAddr, Temp, Const)
from ir_utils import func_slices
from loopopt.loop_unroll4 import LoopUnrollFactorN, unroll_module
from loopopt.transform import LoopTransformDriver
from loopopt.verify import VerifyResult
from loopopt.discovery import discover_function
from loopopt import ir_interp as I

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


# ── fixtures ──────────────────────────────────────────────────────────────────

def array_fill(n, hdr_sym=False):
    """for(i=0;i<BOUND;i++) a[i]=i; resource-bound, multi-block. hdr_sym loads the
    (invariant) bound in the header from FP-16."""
    bound = Temp('nn') if hdr_sym else Const(n)
    bload = ([IRLoadAddr(Temp('pn'), -16), IRLoad(Temp('nn'), Temp('pn'), Const(0), 8)]
             if hdr_sym else [])
    return [IRFuncBegin('f', [], {}, 0),
            IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8)] + bload + [
            IRCondJump(Temp('iv'), '<', bound, 'body', 'exit'),
            IRLabel('body'),
            IRLoadAddr(Temp('base'), -4096),
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


def comma(n):
    """for(i=0,s=0;i<n;i++,s+=i) -- `s+=i` reloads i AFTER `i++` (post-store);
    returns s."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
            IRLoadAddr(Temp('zs'), -16), IRStore(Temp('zs'), Const(0), Const(0), 8),
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Const(n), 'body', 'exit'),
            IRLabel('body'), IRLabel('inc'),
            IRLoadAddr(Temp('pc3'), -8), IRLoad(Temp('iv3'), Temp('pc3'), Const(0), 8),
            IRBinOp(Temp('nv'), '+', Temp('iv3'), Const(1)),
            IRLoadAddr(Temp('pi'), -8), IRStore(Temp('pi'), Const(0), Temp('nv'), 8),
            IRLoadAddr(Temp('pi2'), -8), IRLoad(Temp('inew'), Temp('pi2'), Const(0), 8),
            IRLoadAddr(Temp('ps'), -16), IRLoad(Temp('sv'), Temp('ps'), Const(0), 8),
            IRBinOp(Temp('sn'), '+', Temp('sv'), Temp('inew')),
            IRLoadAddr(Temp('ps2'), -16), IRStore(Temp('ps2'), Const(0), Temp('sn'), 8),
            IRJump('head'),
            IRLabel('exit'),
            IRLoadAddr(Temp('psr'), -16), IRLoad(Temp('sr'), Temp('psr'), Const(0), 8),
            IRReturn(Temp('sr')), IRFuncEnd('f')]


def moving_bound(n):
    """while(lo<hi){lo++; hi--;} -- the bound `hi` DECREMENTS each iteration, so it
    is NOT loop-invariant. R1.4 must reject it (no-op). Returns lo."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLoadAddr(Temp('zl'), -8), IRStore(Temp('zl'), Const(0), Const(0), 8),
            IRLoadAddr(Temp('zh'), -16), IRStore(Temp('zh'), Const(0), Const(n), 8),
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pl'), -8), IRLoad(Temp('lo'), Temp('pl'), Const(0), 8),
            IRLoadAddr(Temp('ph'), -16), IRLoad(Temp('hi'), Temp('ph'), Const(0), 8),
            IRCondJump(Temp('lo'), '<', Temp('hi'), 'body', 'exit'),
            IRLabel('body'),
            IRLoadAddr(Temp('pl2'), -8), IRLoad(Temp('lo2'), Temp('pl2'), Const(0), 8),
            IRBinOp(Temp('ln'), '+', Temp('lo2'), Const(1)),
            IRLoadAddr(Temp('pl3'), -8), IRStore(Temp('pl3'), Const(0), Temp('ln'), 8),
            IRLoadAddr(Temp('ph2'), -16), IRLoad(Temp('hi2'), Temp('ph2'), Const(0), 8),
            IRBinOp(Temp('hn'), '-', Temp('hi2'), Const(1)),
            IRLoadAddr(Temp('ph3'), -16), IRStore(Temp('ph3'), Const(0), Temp('hn'), 8),
            IRJump('head'),
            IRLabel('exit'),
            IRLoadAddr(Temp('plr'), -8), IRLoad(Temp('lr'), Temp('plr'), Const(0), 8),
            IRReturn(Temp('lr')), IRFuncEnd('f')]


def _unroll(ir, factor=None):
    ir1 = copy.deepcopy(ir)
    stats, rep = unroll_module(ir1, force_factor=factor)
    return ir1, stats, rep


def _diff(ir0, ir1, seed=None):
    lo0, hi0 = func_slices(ir0)[0]
    lo1, hi1 = func_slices(ir1)[0]
    g0 = I._preload_globals(ir0); g0.update(seed or {})
    g1 = I._preload_globals(ir1); g1.update(seed or {})
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
    return all(mem.get(-4096 + i * 8) == i for i in range(n))


def _n_loops(ir):
    return len(discover_function(ir, *func_slices(ir)[0]))


# ── tests ─────────────────────────────────────────────────────────────────────

def test_factor4():
    print("factor 4: correct for even/odd/small trips (const):")
    ok = True
    for n in range(0, 14):
        Temp.reset(); ir0 = array_fill(n)
        ir1, st, _ = _unroll(ir0, factor=4)
        if not (_diff(ir0, ir1) and _array_ok(ir1, n)):
            ok = False
    check("array_fill 0..13 correct at factor 4", ok)


def test_factor8():
    print("factor 8: correct for even/odd/small trips (const):")
    ok = True
    for n in range(0, 20):
        Temp.reset(); ir0 = array_fill(n)
        ir1, st, _ = _unroll(ir0, factor=8)
        if not (_diff(ir0, ir1) and _array_ok(ir1, n)):
            ok = False
    check("array_fill 0..19 correct at factor 8", ok)


def test_automatic_factor_selection():
    print("automatic factor selection uses the profitability model (no force):")
    Temp.reset(); ir0 = array_fill(20)          # resource-bound partial -> model picks 4
    ir1, st, rep = _unroll(ir0)
    check("model-driven unroll committed", st.commits == 1)
    check("selected factor is 4 (model recommendation)", rep.factors.get(4, 0) == 1)
    check("behaviour identical", _diff(ir0, ir1) and _array_ok(ir1, 20))


def test_symbolic_header_bound():
    print("wider symbolic: invariant bound loaded in the HEADER (factors 4 & 8):")
    ok = True
    for F in (4, 8):
        for n in range(0, 14):
            Temp.reset(); ir0 = array_fill(n, hdr_sym=True)
            ir1, st, _ = _unroll(ir0, factor=F)
            if not (st.commits == 1 and _diff(ir0, ir1, seed={-16: n})):
                ok = False
    check("header-symbolic array loop correct at factors 4 & 8", ok)


def test_post_store_reload():
    print("post-store IV reload (s+=i after i++) correct at factor 4:")
    ok = all(_diff(comma(n), _unroll(comma(n), factor=4)[0]) for n in range(0, 14))
    check("comma loop correct at factor 4", ok)


def test_moving_bound_rejected():
    print("non-invariant bound (while(lo<hi){lo++;hi--;}) is rejected:")
    for n in (4, 7, 10):
        Temp.reset(); ir0 = moving_bound(n)
        ir1, st, _ = _unroll(ir0)
        check(f"n={n}: not transformed, IR unchanged",
              st.commits == 0 and [repr(x) for x in ir0] == [repr(x) for x in ir1])


def test_remainder_per_factor():
    print("remainder handling per factor (dead when T % F == 0, else present):")
    # factor 4: T=8 -> no remainder (1 loop); T=10 -> remainder (2 loops)
    Temp.reset(); ir = array_fill(8); LoopTransformDriver().run(_mk(4), ir)
    check("factor 4, T=8: remainder omitted (1 loop)", _n_loops(ir) == 1)
    Temp.reset(); ir = array_fill(10); LoopTransformDriver().run(_mk(4), ir)
    check("factor 4, T=10: remainder present (2 loops)", _n_loops(ir) == 2)
    Temp.reset(); ir = array_fill(16); LoopTransformDriver().run(_mk(8), ir)
    check("factor 8, T=16: remainder omitted (1 loop)", _n_loops(ir) == 1)
    Temp.reset(); ir = array_fill(19); LoopTransformDriver().run(_mk(8), ir)
    check("factor 8, T=19: remainder present (2 loops)", _n_loops(ir) == 2)


def test_iv_substitution_active():
    print("IV substitution stays active at factor 4 (few IV-slot loads):")
    Temp.reset(); ir = array_fill(12); LoopTransformDriver().run(_mk(4), ir)
    # 4 copies, but copies 2..4 make no IV-slot loads; only copy1 + remainder do.
    iv_loads = sum(1 for x in ir if type(x).__name__ == 'IRLoad'
                   and getattr(x.base, 'name', None) is not None)
    check("factor-4 body still correct", _diff(array_fill(12), ir) and _array_ok(ir, 12))
    # sanity: far fewer loads than 4x the original body's IV loads
    check("copies avoid IV-slot reloads (load count stays modest)",
          sum(1 for x in ir if type(x).__name__ == 'IRLoad') < 4 * 3)


def test_rollback():
    print("forced verification failure rolls back to byte-identical IR:")

    class FailVerifier:
        def verify_all(self, descs):
            r = VerifyResult()
            if descs:
                r.add(descs[0], 'test-forced', 'forced failure')
            return r

    Temp.reset(); ir = array_fill(12)
    before = [repr(x) for x in ir]
    st = LoopTransformDriver(verifier=FailVerifier()).run(_mk(4), ir)
    after = [repr(x) for x in ir]
    check("rolled back", st.rollbacks >= 1 and st.commits == 0)
    check("IR restored byte-identically", before == after)


def test_verification_clean():
    print("real transforms verify clean at every factor:")
    vf = rb = 0
    for F in (2, 4, 8):
        for n in (4, 5, 8, 9, 12, 13):
            Temp.reset(); _ir, st, _ = _unroll(array_fill(n), factor=F)
            vf += st.verifier_failures; rb += st.rollbacks
        for n in (5, 8):
            Temp.reset(); _ir, st, _ = _unroll(array_fill(n, hdr_sym=True), factor=F)
            vf += st.verifier_failures; rb += st.rollbacks
    check("0 verifier failures", vf == 0)
    check("0 rollbacks", rb == 0)


def test_regression_unchanged_functions():
    print("regression: functions without an unrolled loop stay byte-identical:")
    Temp.reset()
    g = [IRFuncBegin('g', [], {}, 0),
         IRLoadAddr(Temp('gp'), -8), IRStore(Temp('gp'), Const(0), Const(42), 8),
         IRReturn(Const(42)), IRFuncEnd('g')]
    prog = g + array_fill(12)
    prog1 = copy.deepcopy(prog)
    unroll_module(prog1)
    s0 = {prog[a].name: [repr(x) for x in prog[a:b + 1]] for a, b in func_slices(prog)}
    s1 = {prog1[a].name: [repr(x) for x in prog1[a:b + 1]] for a, b in func_slices(prog1)}
    check("function 'g' unchanged", s0['g'] == s1['g'])
    check("function 'f' transformed", s0['f'] != s1['f'])


def test_compiles():
    print("R1.4 IR still generates code at every factor (no spill):")
    from codegen import CodeGen
    from bundler import bundle_mcode
    ok = True
    for F in (2, 4, 8):
        for n in (5, 8, 9):
            Temp.reset(); ir1, _st, _ = _unroll(array_fill(n), factor=F)
            try:
                cg = CodeGen(global_base=0x400)
                bundle_mcode(cg.generate(copy.deepcopy(ir1), global_base=0x400))
                if cg.spilled:
                    ok = False
            except Exception:
                ok = False
    check("codegen + bundler succeed at factors 2/4/8", ok)


def _mk(factor):
    xf = LoopUnrollFactorN()
    xf.force_factor = factor
    return xf


def main():
    tests = [test_factor4, test_factor8, test_automatic_factor_selection,
             test_symbolic_header_bound, test_post_store_reload,
             test_moving_bound_rejected, test_remainder_per_factor,
             test_iv_substitution_active, test_rollback, test_verification_clean,
             test_regression_unchanged_functions, test_compiles]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"R1.4 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("R1.4 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

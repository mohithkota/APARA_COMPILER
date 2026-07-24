"""
_m6_test.py -- unit tests for M6 Loop Rotation (the first concrete transform).

Every rotation runs THROUGH the M5 framework (MutationTransaction / rebuild /
LoopVerifier / rollback / CFGDiff / TransformStats); these tests never mutate IR
directly. Each case checks the four things the milestone requires:

  * CFGDiff              -- the structural change (or absence of one) is observed
  * LoopVerifier         -- passes on the regenerated descriptors
  * rebuilt descriptors  -- discovery re-runs; loop identity (header label) kept
  * semantic equivalence -- the branch-executing interpreter from _m4_test gives
                            identical (return, memory) for the original program
                            and the canonicalized+rotated program

Coverage: simple while, already-rotated (skip), nested, do-while (skip),
short-circuit condition, multiple exits, illegal rotation (skip), rollback path.
Run:  python3 compiler/loopopt/_m6_test.py
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRStore, IRLoad, IRLoadAddr, Temp, Const)
from ir_utils import func_slices
from loopopt import discover, LoopCanonicalizer
from loopopt.discovery import discover_function
from loopopt.canonicalize import _slice_end
from loopopt.verify import LoopVerifier
from loopopt.descriptor import TOP_TESTED, BOTTOM_TESTED
from loopopt.transform import LoopTransformDriver, TransformStats, TransformResult
from loopopt.rotate import LoopRotation, rotate_module
from loopopt.cfgdiff import diff_loop
from loopopt._m4_test import run
from loopopt._m5_test import sum_loop        # a canonical top-tested while loop

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _find(descs, hlbl):
    return next((d for d in descs if d.cfg.blocks[d.header].label == hlbl), None)


def rotate_and_check(factory, hlbl, expect_rotated, extra=None):
    """Canonicalize (reusing M4) then rotate through the framework; verify diff,
    verifier, descriptor regen and semantic equivalence."""
    Temp.reset(); raw = factory()                         # pristine, for semantics
    Temp.reset(); work = factory()
    LoopCanonicalizer().canonicalize(work)                # present canonical shape
    before_snapshot = copy.deepcopy(work)                 # independent pre-rotation view

    stats, rep = rotate_module(work)

    after_descs = discover(work)
    bd = _find(discover(before_snapshot), hlbl)
    ad = _find(after_descs, hlbl)

    check("descriptor regenerated (loop identity/header label preserved)", ad is not None)
    check("LoopVerifier passes after rotation", LoopVerifier().verify_all(after_descs).ok)
    check("no verifier failures / rollbacks", rep.verifier_failures == 0 and rep.rollbacks == 0)

    if bd is not None and ad is not None:
        ld = diff_loop(bd, ad)
        check("header identity preserved in CFG diff", ld.identity_preserved)
        if expect_rotated:
            check("CFG diff shows a structural change", ld.structurally_changed)
        else:
            check("CFG diff shows NO change (skipped)", not ld.structurally_changed)

    if expect_rotated:
        check("loop rotated (report)", rep.loops_rotated >= 1)
        check("rotated shape is bottom-tested", ad is not None and ad.shape == BOTTOM_TESTED)
    else:
        check("loop NOT rotated (report)", rep.loops_rotated == 0)

    check(f"semantics preserved (raw={run(raw)[0]} rotated={run(work)[0]})",
          run(raw) == run(work))

    if extra:
        extra(rep, ad, work)
    return rep


# ── factories (all executable by the interpreter) ─────────────────────────────

def do_while_sum():
    """Bottom-tested do-while -> already rotated; must be skipped."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
            IRLoadAddr(Temp('za'), -16), IRStore(Temp('za'), Const(0), Const(0), 8),
            IRLabel('body'),
            IRLoadAddr(Temp('pa'), -16), IRLoad(Temp('av'), Temp('pa'), Const(0), 8),
            IRLoadAddr(Temp('pi'), -8), IRLoad(Temp('iv'), Temp('pi'), Const(0), 8),
            IRBinOp(Temp('av2'), '+', Temp('av'), Temp('iv')),
            IRLoadAddr(Temp('pa2'), -16), IRStore(Temp('pa2'), Const(0), Temp('av2'), 8),
            IRBinOp(Temp('iv2'), '+', Temp('iv'), Const(1)),
            IRLoadAddr(Temp('pi2'), -8), IRStore(Temp('pi2'), Const(0), Temp('iv2'), 8),
            IRCondJump(Temp('iv2'), '<', Const(5), 'body', 'exit'),
            IRLabel('exit'),
            IRLoadAddr(Temp('pr'), -16), IRLoad(Temp('rv'), Temp('pr'), Const(0), 8),
            IRReturn(Temp('rv')),
            IRFuncEnd('f')]


def nested_sum():
    """Outer i<2, inner j<2, acc += 1 -> 4. Both loops top-tested & canonical."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
            IRLoadAddr(Temp('za'), -16), IRStore(Temp('za'), Const(0), Const(0), 8),
            IRJump('oh'),
            IRLabel('oh'),
            IRLoadAddr(Temp('pi'), -8), IRLoad(Temp('iv'), Temp('pi'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Const(2), 'ob', 'ox'),
            IRLabel('ob'),
            IRLoadAddr(Temp('zj'), -24), IRStore(Temp('zj'), Const(0), Const(0), 8),
            IRJump('ih'),
            IRLabel('ih'),
            IRLoadAddr(Temp('pj'), -24), IRLoad(Temp('jv'), Temp('pj'), Const(0), 8),
            IRCondJump(Temp('jv'), '<', Const(2), 'ib', 'ix'),
            IRLabel('ib'),
            IRLoadAddr(Temp('pa'), -16), IRLoad(Temp('av'), Temp('pa'), Const(0), 8),
            IRBinOp(Temp('av2'), '+', Temp('av'), Const(1)),
            IRLoadAddr(Temp('pa2'), -16), IRStore(Temp('pa2'), Const(0), Temp('av2'), 8),
            IRBinOp(Temp('jv2'), '+', Temp('jv'), Const(1)),
            IRLoadAddr(Temp('pj2'), -24), IRStore(Temp('pj2'), Const(0), Temp('jv2'), 8),
            IRJump('ih'),
            IRLabel('ix'),
            IRLoadAddr(Temp('pi2'), -8), IRLoad(Temp('iv3'), Temp('pi2'), Const(0), 8),
            IRBinOp(Temp('iv2'), '+', Temp('iv3'), Const(1)),
            IRLoadAddr(Temp('pi3'), -8), IRStore(Temp('pi3'), Const(0), Temp('iv2'), 8),
            IRJump('oh'),
            IRLabel('ox'),
            IRLoadAddr(Temp('pr'), -16), IRLoad(Temp('rv'), Temp('pr'), Const(0), 8),
            IRReturn(Temp('rv')),
            IRFuncEnd('f')]


def short_circuit_sum():
    """while (i<5 && i<3) acc+=i,i++  -> acc = 0+1+2 = 3. Split header head/head2."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
            IRLoadAddr(Temp('za'), -16), IRStore(Temp('za'), Const(0), Const(0), 8),
            IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Const(5), 'head2', 'exit'),
            IRLabel('head2'),
            IRLoadAddr(Temp('pc2'), -8), IRLoad(Temp('iv0'), Temp('pc2'), Const(0), 8),
            IRCondJump(Temp('iv0'), '<', Const(3), 'body', 'exit'),
            IRLabel('body'),
            IRLoadAddr(Temp('pa'), -16), IRLoad(Temp('av'), Temp('pa'), Const(0), 8),
            IRLoadAddr(Temp('pi'), -8), IRLoad(Temp('bv'), Temp('pi'), Const(0), 8),
            IRBinOp(Temp('av2'), '+', Temp('av'), Temp('bv')),
            IRLoadAddr(Temp('pa2'), -16), IRStore(Temp('pa2'), Const(0), Temp('av2'), 8),
            IRBinOp(Temp('bi2'), '+', Temp('bv'), Const(1)),
            IRLoadAddr(Temp('pi2'), -8), IRStore(Temp('pi2'), Const(0), Temp('bi2'), 8),
            IRJump('head'),
            IRLabel('exit'),
            IRLoadAddr(Temp('pr'), -16), IRLoad(Temp('rv'), Temp('pr'), Const(0), 8),
            IRReturn(Temp('rv')),
            IRFuncEnd('f')]


def multi_exit_sum():
    """while(i<9){ acc+=i; if(acc>=10) break; i++ } -> returns 10 (break path)."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
            IRLoadAddr(Temp('za'), -16), IRStore(Temp('za'), Const(0), Const(0), 8),
            IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Const(9), 'body', 'exit'),
            IRLabel('body'),
            IRLoadAddr(Temp('pa'), -16), IRLoad(Temp('av'), Temp('pa'), Const(0), 8),
            IRLoadAddr(Temp('pi'), -8), IRLoad(Temp('bv'), Temp('pi'), Const(0), 8),
            IRBinOp(Temp('av2'), '+', Temp('av'), Temp('bv')),
            IRLoadAddr(Temp('pa2'), -16), IRStore(Temp('pa2'), Const(0), Temp('av2'), 8),
            IRCondJump(Temp('av2'), '>=', Const(10), 'exit2', 'cont'),
            IRLabel('cont'),
            IRBinOp(Temp('bi2'), '+', Temp('bv'), Const(1)),
            IRLoadAddr(Temp('pi2'), -8), IRStore(Temp('pi2'), Const(0), Temp('bi2'), 8),
            IRJump('head'),
            IRLabel('exit2'),
            IRLoadAddr(Temp('pr2'), -16), IRLoad(Temp('rv2'), Temp('pr2'), Const(0), 8),
            IRReturn(Temp('rv2')),
            IRLabel('exit'),
            IRLoadAddr(Temp('pr'), -16), IRLoad(Temp('rv'), Temp('pr'), Const(0), 8),
            IRReturn(Temp('rv')),
            IRFuncEnd('f')]


def header_side_effect():
    """Header contains a store -> not a pure guard -> rotation is ILLEGAL."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
            IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRLoadAddr(Temp('ps'), -16), IRStore(Temp('ps'), Const(0), Temp('iv'), 8),  # side effect
            IRCondJump(Temp('iv'), '<', Const(3), 'body', 'exit'),
            IRLabel('body'),
            IRLoadAddr(Temp('pi'), -8), IRLoad(Temp('bv'), Temp('pi'), Const(0), 8),
            IRBinOp(Temp('bi2'), '+', Temp('bv'), Const(1)),
            IRLoadAddr(Temp('pi2'), -8), IRStore(Temp('pi2'), Const(0), Temp('bi2'), 8),
            IRJump('head'),
            IRLabel('exit'),
            IRLoadAddr(Temp('pi'), -8), IRLoad(Temp('rv'), Temp('pi'), Const(0), 8),
            IRReturn(Temp('rv')),
            IRFuncEnd('f')]


# ── tests ─────────────────────────────────────────────────────────────────────

def test_simple_while():
    print("simple while loop -> rotated to do-while:")
    def extra(rep, ad, work):
        check("single latch after rotation", ad is not None and len(ad.latches) == 1)
        check("guard moved / preheader reused / header-latch swap all == 1",
              rep.guard_conditions_moved == 1 and rep.preheaders_reused == 1
              and rep.header_latch_swaps == 1)
    rotate_and_check(lambda: sum_loop(5), 'head', expect_rotated=True, extra=extra)


def test_already_rotated_skipped():
    print("already-rotated (do-while) -> skipped, idempotent:")
    rotate_and_check(do_while_sum, 'body', expect_rotated=False)
    # a while loop rotated once must not rotate again
    Temp.reset(); work = sum_loop(5)
    LoopCanonicalizer().canonicalize(work)
    r1 = rotate_module(work)[1]
    r2 = rotate_module(work)[1]
    check("first pass rotates", r1.loops_rotated == 1)
    check("second pass rotates nothing (idempotent)", r2.loops_rotated == 0)
    check("second pass skips as illegal (already rotated)", r2.loops_skipped >= 1)


def test_nested():
    print("nested loops -> both rotated, nesting preserved:")
    def extra(rep, ad, work):
        after = discover(work)
        oh = _find(after, 'oh'); ih = _find(after, 'ih')
        check("both loops present after rotation", oh is not None and ih is not None)
        check("both rotated to bottom-tested",
              oh.shape == BOTTOM_TESTED and ih.shape == BOTTOM_TESTED)
        check("inner still nested in outer", ih.body_blocks <= oh.body_blocks)
        check("two rotations performed", rep.loops_rotated == 2)
    rotate_and_check(nested_sum, 'oh', expect_rotated=True, extra=extra)


def test_short_circuit():
    print("short-circuit (&&) loop -> outer test rotated, inner test kept in loop:")
    def extra(rep, ad, work):
        check("inner condition block still present (head2)",
              any(getattr(x, 'name', None) == 'head2' for x in work))
    rotate_and_check(short_circuit_sum, 'head', expect_rotated=True, extra=extra)


def test_multiple_exits():
    print("multiple-exit loop -> rotated, extra exit preserved:")
    def extra(rep, ad, work):
        check("loop still has >= 2 exit targets", len(ad.exit_blocks) >= 2)
        check("break target exit2 preserved",
              any(getattr(x, 'name', None) == 'exit2' for x in work))
    rotate_and_check(multi_exit_sum, 'head', expect_rotated=True, extra=extra)


def test_illegal_rotation():
    print("illegal rotation (header has a side effect) -> skipped, IR unchanged:")
    Temp.reset(); work = header_side_effect()
    LoopCanonicalizer().canonicalize(work)
    snap = [repr(x) for x in work]
    stats, rep = rotate_module(work)
    check("not rotated", rep.loops_rotated == 0)
    check("skipped as illegal", rep.loops_skipped >= 1)
    check("IR unchanged", [repr(x) for x in work] == snap)
    check("verifier still clean", LoopVerifier().verify_all(discover(work)).ok)


def test_rollback_path():
    print("rollback path: a rotation whose postcondition fails is fully reverted:")
    class RejectedRotation(LoopRotation):
        name = 'test-rejected-rotation'
        def postcondition(self, before_desc, after_desc):
            return False
    Temp.reset(); work = sum_loop(5)
    LoopCanonicalizer().canonicalize(work)
    before = [repr(x) for x in work]
    lo, _ = func_slices(work)[0]
    d = discover_function(work, lo, _slice_end(work, lo))[0]
    drv = LoopTransformDriver(); stats = TransformStats()
    res = drv.run_transform(RejectedRotation(), work, lo, d, stats)
    check("outcome rolled-back", res.rolled_back)
    check("reason is postcondition", res.reason == 'postcondition')
    check("IR restored byte-for-byte", [repr(x) for x in work] == before)
    check("stats: 0 commits, 1 rollback", stats.commits == 0 and stats.rollbacks == 1)
    check("rebuilt loop still verifies + still top-tested (unrotated)",
          LoopVerifier().verify_all(discover(work)).ok
          and discover(work)[0].shape == TOP_TESTED)


def main():
    tests = [test_simple_while, test_already_rotated_skipped, test_nested,
             test_short_circuit, test_multiple_exits, test_illegal_rotation,
             test_rollback_path]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"M6 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("M6 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

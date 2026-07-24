"""
_m5_test.py -- unit tests for the M5 LoopTransform framework.

M5 ships NO optimization, so these tests drive the framework with DUMMY,
trivially semantics-preserving transforms and assert that the framework -- not
the transform -- correctly performs:

  * transactions      (begin / apply / commit)
  * rollback          (postcondition failure, lost loop identity, verifier veto)
  * verifier          (run on every attempt; a failing verifier vetoes a commit)
  * analysis rebuild  (no stale descriptors; loop re-verifies clean after commit)
  * statistics        (attempts / commits / rollbacks / per-transform diff totals)
  * CFG-diff          (each committed mutation is observable via LoopDiff)
  * registration      (PassRegistry)

The single "good" dummy inserts an unconditional forwarding block on the loop's
back edge -- a redundant `goto`, i.e. NOT an optimization -- which is enough to
exercise splice + retarget + rebuild + verify + diff. NO loop rotation is
implemented. Semantic equivalence reuses the branch-executing interpreter from
_m4_test.

Run:  python3 compiler/loopopt/_m5_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRStore, IRLoad, IRLoadAddr, Temp, Const)
from ir_utils import func_slices
from loopopt.discovery import discover_function
from loopopt.canonicalize import _slice_end
from loopopt.verify import LoopVerifier, VerifyResult
from loopopt.transform import (LoopTransform, LoopTransformDriver, MutationTransaction,
                               TransformResult, TransformStats, PassRegistry)
from loopopt._m4_test import run                      # reuse the semantic oracle

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


# ── dummy transforms (test fixtures only) ─────────────────────────────────────

class BackEdgeForwarder(LoopTransform):
    """GOOD dummy: reroute the single back edge through a fresh forwarding block
    that unconditionally jumps to the header. Semantics-preserving; adds one
    block + reroutes one edge. Not an optimization."""
    name = 'test-backedge-forwarder'

    def legal(self, desc):
        if desc.cfg.blocks[desc.header].label is None:
            return False, 'header has no label'
        if len(desc.latches) != 1:
            return False, 'needs exactly one latch'
        term = desc.cfg.instrs[desc.cfg.blocks[desc.latches[0]].hi]
        hlbl = desc.cfg.blocks[desc.header].label
        # only legal when the back edge is an explicit branch we can retarget
        from loopopt.canonicalize import _targets
        if not _targets(term, hlbl):
            return False, 'back edge is not an explicit branch'
        return True, ''

    def run(self, instrs, lo, desc, txn):
        hlbl = desc.cfg.blocks[desc.header].label
        latch = desc.latches[0]
        term = desc.cfg.instrs[desc.cfg.blocks[latch].hi]
        fl = txn.fresh_label(f'__fwd_{hlbl}')
        txn.splice(txn.slice_end(), [IRLabel(fl), IRJump(hlbl)])   # end-of-slice seam
        return txn.retarget(term, hlbl, fl)

    def postcondition(self, before_desc, after_desc):
        return len(after_desc.latches) == 1


class RejectedForwarder(BackEdgeForwarder):
    """Identical mutation but its postcondition always fails -> forces rollback."""
    name = 'test-rejected'

    def postcondition(self, before_desc, after_desc):
        return False


class LoopBreaker(LoopTransform):
    """Retarget the back edge to a DANGLING label -> the edge is dropped, the
    loop dissolves, the framework loses loop identity and must roll back."""
    name = 'test-loop-breaker'

    def legal(self, desc):
        return (len(desc.latches) == 1), 'needs single latch'

    def run(self, instrs, lo, desc, txn):
        hlbl = desc.cfg.blocks[desc.header].label
        term = desc.cfg.instrs[desc.cfg.blocks[desc.latches[0]].hi]
        return txn.retarget(term, hlbl, '__dangling_nowhere__')


class NoopTransform(LoopTransform):
    name = 'test-noop'

    def run(self, instrs, lo, desc, txn):
        return False        # intentional no-op


class AlwaysIllegal(LoopTransform):
    name = 'test-illegal'

    def legal(self, desc):
        return False, 'never legal'

    def run(self, instrs, lo, desc, txn):
        raise AssertionError('run() must not be called when legal() is False')


class FailingVerifier:
    """Verifier stub that always reports a violation, to prove the framework
    rolls back when verification fails."""
    def verify_all(self, descs):
        r = VerifyResult()
        if descs:
            r.add(descs[0], 'test-injected', 'forced verification failure')
        return r


# ── executable test loop (sum 0..n-1 into slot -16) ───────────────────────────

def sum_loop(n=5):
    return [IRFuncBegin('f', [], {}, 0),
            IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
            IRLoadAddr(Temp('za'), -16), IRStore(Temp('za'), Const(0), Const(0), 8),
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Const(n), 'body', 'exit'),
            IRLabel('body'),
            IRLoadAddr(Temp('pa'), -16), IRLoad(Temp('av'), Temp('pa'), Const(0), 8),
            IRBinOp(Temp('av2'), '+', Temp('av'), Temp('iv')),
            IRLoadAddr(Temp('pa2'), -16), IRStore(Temp('pa2'), Const(0), Temp('av2'), 8),
            IRBinOp(Temp('iv2'), '+', Temp('iv'), Const(1)),
            IRLoadAddr(Temp('pi2'), -8), IRStore(Temp('pi2'), Const(0), Temp('iv2'), 8),
            IRJump('head'),
            IRLabel('exit'),
            IRLoadAddr(Temp('pr'), -16), IRLoad(Temp('rv'), Temp('pr'), Const(0), 8),
            IRReturn(Temp('rv')),
            IRFuncEnd('f')]


def _slice0(ir):
    lo, _hi = func_slices(ir)[0]
    return lo


def _first_loop(ir, lo):
    return discover_function(ir, lo, _slice_end(ir, lo))[0]


def _snap(ir):
    return [repr(x) for x in ir]


# ── tests ─────────────────────────────────────────────────────────────────────

def test_commit_diff_stats():
    print("commit path: mutation applied, diff + stats recorded:")
    Temp.reset(); ir = sum_loop()
    lo = _slice0(ir)
    d = _first_loop(ir, lo)
    drv = LoopTransformDriver()
    e0 = drv.epoch
    stats = TransformStats()
    res = drv.run_transform(BackEdgeForwarder(), ir, lo, d, stats)
    check("outcome committed", res.committed)
    check("framework ran the verifier and it passed", res.verify is not None and res.verify.ok)
    check("epoch advanced on commit", drv.epoch == e0 + 1)
    check("LoopDiff reports a structural change", res.loop_diff is not None
          and res.loop_diff.structurally_changed)
    check("header identity preserved", res.loop_diff.identity_preserved)
    check("a forwarder block was added", any(b.startswith('L:__fwd_head')
          for b in res.loop_diff.cfg_diff.blocks_added))
    check("stats: 1 attempt / 1 commit / 0 rollback",
          stats.attempts == 1 and stats.commits == 1 and stats.rollbacks == 0)
    check("stats: per-transform blocks_added counted",
          stats.per_transform['test-backedge-forwarder']['blocks_added'] >= 1)
    check("mutated IR verifies clean (rebuilt, non-stale)",
          LoopVerifier().verify_all(discover_function(
              ir, lo, _slice_end(ir, lo))).ok)


def test_rollback_postcondition():
    print("rollback path: postcondition failure restores IR exactly:")
    Temp.reset(); ir = sum_loop()
    lo = _slice0(ir); d = _first_loop(ir, lo)
    before = _snap(ir)
    drv = LoopTransformDriver(); e0 = drv.epoch
    stats = TransformStats()
    res = drv.run_transform(RejectedForwarder(), ir, lo, d, stats)
    check("outcome rolled-back", res.rolled_back)
    check("reason is postcondition", res.reason == 'postcondition')
    check("IR restored byte-for-byte", _snap(ir) == before)
    check("epoch NOT advanced on rollback", drv.epoch == e0)
    check("stats: 0 commits, 1 rollback", stats.commits == 0 and stats.rollbacks == 1)


def test_rollback_identity_lost():
    print("rollback path: dissolving the loop is caught (identity lost):")
    Temp.reset(); ir = sum_loop()
    lo = _slice0(ir); d = _first_loop(ir, lo)
    before = _snap(ir)
    drv = LoopTransformDriver()
    stats = TransformStats()
    res = drv.run_transform(LoopBreaker(), ir, lo, d, stats)
    check("outcome rolled-back", res.rolled_back)
    check("reason is loop-identity-lost", res.reason == 'loop-identity-lost')
    check("IR restored byte-for-byte", _snap(ir) == before)
    check("stats: 1 rollback", stats.rollbacks == 1)


def test_verifier_veto():
    print("verifier integration: a failing verifier vetoes the commit:")
    Temp.reset(); ir = sum_loop()
    lo = _slice0(ir); d = _first_loop(ir, lo)
    before = _snap(ir)
    drv = LoopTransformDriver(verifier=FailingVerifier())
    stats = TransformStats()
    res = drv.run_transform(BackEdgeForwarder(), ir, lo, d, stats)
    check("outcome rolled-back", res.rolled_back)
    check("reason is verifier", res.reason == 'verifier')
    check("verifier_failures counted", stats.verifier_failures == 1)
    check("IR restored byte-for-byte", _snap(ir) == before)


def test_noop_and_illegal_skips():
    print("skip paths: no-op and illegal transforms leave IR untouched:")
    Temp.reset(); ir = sum_loop()
    lo = _slice0(ir); d = _first_loop(ir, lo)
    before = _snap(ir)
    drv = LoopTransformDriver(); stats = TransformStats()
    r1 = drv.run_transform(NoopTransform(), ir, lo, d, stats)
    check("no-op -> skipped-noop", r1.outcome == TransformResult.SKIPPED_NOOP)
    check("no-op leaves IR unchanged", _snap(ir) == before)
    r2 = drv.run_transform(AlwaysIllegal(), ir, lo, d, stats)
    check("illegal -> skipped-illegal", r2.outcome == TransformResult.SKIPPED_ILLEGAL)
    check("illegal reason surfaced", r2.reason == 'never legal')
    check("illegal leaves IR unchanged (run never called)", _snap(ir) == before)
    check("stats: no commits/rollbacks on skips",
          stats.commits == 0 and stats.rollbacks == 0
          and stats.skipped_noop == 1 and stats.skipped_illegal == 1)


def test_driver_run_and_semantics():
    print("driver.run over all loops: terminates, commits once, preserves semantics:")
    Temp.reset(); before_ir = sum_loop()
    Temp.reset(); after_ir = sum_loop()
    drv = LoopTransformDriver()
    stats = drv.run(BackEdgeForwarder(), after_ir)
    check("exactly one commit (one loop)", stats.commits == 1)
    check("no rollbacks", stats.rollbacks == 0)
    check("timing recorded", stats.elapsed_ms >= 0.0)
    lo = _slice0(after_ir)
    check("post-run loop verifies clean",
          LoopVerifier().verify_all(discover_function(
              after_ir, lo, _slice_end(after_ir, lo))).ok)
    rb = run(before_ir); ra = run(after_ir)
    check(f"semantics preserved (before={rb[0]} after={ra[0]})", rb == ra)


def test_registration():
    print("pass registration:")
    reg = PassRegistry()
    t = BackEdgeForwarder()
    reg.register(t)
    check("registered transform retrievable by name", reg.get('test-backedge-forwarder') is t)
    check("name listed", 'test-backedge-forwarder' in reg.names())
    check("__contains__ works", 'test-backedge-forwarder' in reg)
    dup_raised = False
    try:
        reg.register(BackEdgeForwarder())
    except ValueError:
        dup_raised = True
    check("duplicate registration raises", dup_raised)
    check("empty registry has no passes (M5 ships none)", PassRegistry().names() == [])


def test_transaction_unit():
    print("MutationTransaction unit: field edit + splice rollback:")
    Temp.reset(); ir = sum_loop()
    lo = _slice0(ir)
    before = _snap(ir)
    txn = MutationTransaction(ir, lo)
    cj = next(x for x in ir if type(x).__name__ == 'IRCondJump')
    txn.set_field(cj, 'true_label', 'ZZZ')
    txn.splice(txn.slice_end(), [IRLabel('ZZ_extra'), IRJump('head')])
    check("edits are visible before rollback",
          cj.true_label == 'ZZZ' and any(getattr(x, 'name', None) == 'ZZ_extra' for x in ir))
    txn.rollback()
    check("rollback restores field edit", cj.true_label == 'body')
    check("rollback removes spliced instructions", _snap(ir) == before)


def main():
    tests = [test_commit_diff_stats,
             test_rollback_postcondition,
             test_rollback_identity_lost,
             test_verifier_veto,
             test_noop_and_illegal_skips,
             test_driver_run_and_semantics,
             test_registration,
             test_transaction_unit]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"M5 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("M5 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

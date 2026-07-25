"""
loop_ivsr.py -- LoopIVSR (Loop Optimization Framework, Milestone M9).

Migration of Induction-Variable Strength Reduction / pointer strength reduction
(ivsr.py) onto the M5 LoopTransform framework. Like the M8 LICM migration this is
an ARCHITECTURAL CONSOLIDATION, not an optimization change: it produces
instruction-for-instruction identical IR to `ivsr.induction_strength_reduce`
(proven by ivsr_crosscheck.py across the corpus). What changes is only the
MACHINERY:

  * loop discovery comes from the shared M0 descriptors (discover_function),
    enumerated in ivsr's exact traversal order (see below), instead of the ad-hoc
    textual `_find_loops`;
  * every IR mutation flows through the M5 MutationTransaction (via the new
    additive `replace_span()` primitive -- the sole framework addition M9 needed);
  * the framework owns analysis rebuild / verification / rollback / statistics;
  * the shared DefUse analysis supplies the single-def map / multi-name set
    (ivsr already consumed these).

The IVSR DECISION + REWRITE logic is NOT reimplemented. `ivsr._process_loop` is a
PURE planner (it reads the IR and RETURNS a new list; it never mutates in place),
so LoopIVSR calls it directly as its planner and hands the resulting region
rewrite to the framework as one reversible `replace_span`. This maximises reuse
(no duplicated heuristics, profitability, invariance, IV detection, cloning or
fresh-temp numbering -- all identical by construction) and confines M9 to the
framework plumbing. ivsr.py remains the SPECIFICATION and the A/B baseline;
`ivsr.dead_temp_elim` (a whole-program cleanup, not a loop transform) is reused
verbatim as the post-pass, exactly as in the original.

--------------------------------------------------------------------------------
Faithful reproduction of ivsr's control flow
--------------------------------------------------------------------------------
`induction_strength_reduce` repeatedly (fixpoint, <=200 rounds): scan every loop
smallest-region-first; call `_process_loop` on each; apply the FIRST that
progresses and restart the scan; when none progresses, run `dead_temp_elim` once.

CRITICAL fidelity point: `_process_loop` advances a MODULE-GLOBAL fresh-temp
counter (`ivsr._iv_n`) whenever it reaches candidate construction -- INCLUDING for
loops it ultimately rejects on profitability, and INCLUDING re-attempts of the
same loop on later fixpoint rounds. The generated preheader temp NAMES therefore
depend on the exact sequence of attempts. LoopIVSR reproduces that sequence
exactly: `ivsr_module` re-enumerates every round, attempts loops in the identical
order, and calls the identical `_process_loop` per attempt (its side effect on
`ivsr._iv_n` is preserved because the framework rolls back the IR on a no-op /
verify failure but never the module counter -- precisely as ivsr never "un-bumps"
it). Enumeration order is reproduced from the descriptors, not from `_find_loops`:
each loop's region is (header-label index .. back-edge index); sorting these by
(region-size, back-edge-index, header-index) yields ivsr's smallest-region-first
order (verified identical to `_find_loops` on the whole corpus).
"""

import ivsr
from ir_utils import func_slices
from analysis import DefUse

from .transform import (LoopTransform, LoopTransformDriver, TransformStats)


# ── the transform ─────────────────────────────────────────────────────────────

class LoopIVSR(LoopTransform):
    """Strength-reduce induction-variable addresses in ONE loop per attempt,
    through the MutationTransaction. The framework rebuilds analyses, verifies and
    commits/rolls back; the outer fixpoint + enumeration order that make this
    equivalent to ivsr live in ivsr_module(). The IVSR decision/rewrite logic is
    reused verbatim from ivsr._process_loop (a pure planner)."""

    name = 'loop-ivsr'

    def __init__(self):
        # the (s, e) region (header-label index .. back-edge index) this attempt
        # targets, set by the driver just before each run_transform call. A single
        # descriptor with N latches maps to N ivsr regions; _region disambiguates.
        self._region = None

    # -- legality: none is framework-expressible ------------------------------
    # ivsr's admissibility (single-entry via jump-targets, no clobbering call /
    # wide store, clean-slot memory-backed IV, per-candidate profitability) is
    # entirely IVSR-specific and inseparable from planning -- it lives INSIDE
    # _process_loop, which returns the input unchanged when the loop is
    # inadmissible. The M7 predicates (unique-preheader by predecessor, etc.) test
    # DIFFERENT properties, so substituting them would change which loops are
    # transformed. We therefore keep legality where ivsr keeps it: legal() is
    # trivially true, and run() reports a no-op when _process_loop declines. This
    # also preserves ivsr's fresh-counter side effects (legal() must not pre-empt
    # the _process_loop call that may bump ivsr._iv_n).
    def legal(self, desc):
        return True, ''

    # -- the rewrite, exclusively through the transaction ----------------------

    def run(self, instrs, lo, desc, txn):
        s, e = self._region
        fa, fb = desc.func_slice                      # enclosing FUNCTION slice
        du = DefUse(instrs, fa, fb)

        # ivsr._process_loop is PURE: it returns a NEW list on success or the SAME
        # `instrs` object when the loop is inadmissible/unprofitable. It may bump
        # ivsr._iv_n as a side effect (identical to the spec).
        before_len = len(instrs)
        new_list = ivsr._process_loop(instrs, s, e,
                                      du.single_defs(), du.multi_names(), fa, fb)
        if new_list is instrs:
            return False                              # no-op (declined): identical to spec

        # _process_loop returns instrs[:s] + preheader + new_region + instrs[e+1:],
        # i.e. it rewrites exactly the inclusive span [s, e] (everything before s
        # and after e is untouched). Recover that replacement span from the length
        # delta and apply it as one reversible edit through the transaction.
        delta = len(new_list) - before_len
        new_span = new_list[s:e + 1 + delta]
        txn.replace_span(s, e, new_span)
        return True


# ── IVSR-specific report (derived entirely from framework TransformStats) ─────

class IVSRReport:
    """IVSR-specific view of the framework's TransformStats. One commit == one loop
    strength-reduced; `attempts` counts every run_transform call, including the
    re-attempts of already-processed loops that ivsr's fixpoint performs each round
    (the spec re-scans identically)."""

    __slots__ = ('attempts', 'loops_reduced', 'skipped', 'verifier_failures',
                 'rollbacks', 'semantic_mismatches')

    def __init__(self, stats, semantic_mismatches=0):
        self.attempts = stats.attempts
        self.loops_reduced = stats.commits
        self.skipped = stats.skipped_illegal + stats.skipped_noop
        self.verifier_failures = stats.verifier_failures
        self.rollbacks = stats.rollbacks
        self.semantic_mismatches = semantic_mismatches

    def report(self):
        return "\n".join([
            "LoopIVSR report:",
            f"  attempts (incl re-scans) : {self.attempts}",
            f"  loops strength-reduced   : {self.loops_reduced}",
            f"  attempts skipped         : {self.skipped}",
            f"  verifier failures        : {self.verifier_failures}",
            f"  rollbacks                : {self.rollbacks}",
            f"  semantic mismatches      : {self.semantic_mismatches}",
        ])


# ── driver: ivsr's outer fixpoint + smallest-region-first enumeration ─────────

def _enumerate_regions(instrs, drv):
    """Every loop's (slice_lo, s, e, descriptor), where s = header-label index and
    e = back-edge index, DISCOVERED FROM THE SHARED M0 DESCRIPTORS and ordered as
    ivsr orders them: globally smallest-region-first. The (size, e, s) key
    reproduces `_find_loops`'s stable smallest-first sort exactly (verified
    identical across the corpus). A multi-latch loop yields one region per latch,
    matching `_find_loops`'s one-region-per-back-edge behaviour."""
    regs = []
    for lo, hi in func_slices(instrs):
        for d in drv._rebuild(instrs, lo, drv.epoch):
            hidx = d.cfg.blocks[d.header].lo
            for lt in d.latches:
                eidx = d.cfg.blocks[lt].hi
                regs.append((lo, hidx, eidx, d))
    regs.sort(key=lambda r: (r[2] - r[1], r[2], r[1]))
    return regs


def _one_reduction(drv, xform, instrs, stats):
    """Apply a SINGLE strength reduction across all loops (smallest region first),
    returning True if one was applied -- the framework analogue of ivsr's inner
    scan. Loops are re-enumerated from descriptors each call, so no attempt sees a
    stale descriptor."""
    for (lo, s, e, desc) in _enumerate_regions(instrs, drv):
        xform._region = (s, e)
        res = drv.run_transform(xform, instrs, lo, desc, stats)
        if res.committed:
            return True                               # restart the whole scan
    return False


def ivsr_module(instrs, verifier=None, stats=None):
    """Run induction-variable strength reduction over `instrs` THROUGH the M5
    framework, to a fixpoint, then the reused whole-program dead-temp elimination.
    Behaviourally identical to `ivsr.induction_strength_reduce`. Returns
    (new_instrs, TransformStats, IVSRReport). The strength-reduction phase mutates
    `instrs` in place through the transaction; dead_temp_elim returns the final
    NEW list (as the spec's public entry does)."""
    drv = LoopTransformDriver(verifier=verifier)
    stats = stats or TransformStats()
    xform = LoopIVSR()
    for _ in range(200):                              # ivsr's fixpoint bound
        if not _one_reduction(drv, xform, instrs, stats):
            break
    result = ivsr.dead_temp_elim(instrs)              # reused verbatim (post-pass)
    return result, stats, IVSRReport(stats)

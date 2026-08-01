"""
trace_scheduler.py -- Superblock / Trace Scheduling driver (Milestone R3.2,
Phase 3-5) + production integration.

Runs the EXISTING scheduler (loopopt/schedule.py, R2.4) over the enlarged regions
formed by superblock.py. No new scheduler, no speculation, no duplication: region
formation only merges single-entry/single-exit straight-line chains, so the
scheduler simply sees larger basic blocks and packs across former boundaries. The
existing scheduler already enforces dependence + resource legality and memory
ordering, and already validates each function with the differential oracle and
rolls back on any mismatch -- all reused unchanged.

Production integration (apply_superblock_scheduling) mirrors R3.1: gated by the
R3.0 oracle (scheduling headroom over a threshold), accepted only if the program
still compiles with ZERO spills AND the bundle count does not increase, otherwise
the proven input is kept verbatim. `APARA_NO_SUPERBLOCK=1` disables it.

PROFITABILITY GRANULARITY (R6.7)
--------------------------------
The metric above is unchanged -- compiles, no new spill, bundles not increased.
What changed is the SCOPE at which it is applied.

Through R3.2 the decision was whole-module: every available merge was formed at
once and the result accepted or rejected as a unit. That made unrelated regions
share a fate. R6.6 hit it concretely: accumulator expansion made one vector loop
denser, the module-wide candidate then failed the gate, and EVERY merge was
discarded -- including one in a 64-iteration scalar initialisation loop that had
nothing to do with the vector code, worth 64 dynamic bundles.

R6.7 decides per region:

  1. the whole set is tried FIRST, exactly as before. If it passes it is taken
     unchanged -- so R6.7 is a strict refinement and costs nothing in the common
     case. This step is load-bearing, not just an optimisation: greedy per-region
     acceptance can only reach a SUBSET of the merges, and a subset can be worse
     than the full set (if merge A alone increases bundles but A+B together do
     not, a greedy pass that offers A first rejects it and never recovers it).
     Measured: skipping this step regressed gemm vi8/vu8 by 256 ticks each.
  2. only when the whole set is rejected are regions offered one at a time, each
     accepted if it keeps the same metric relative to what has been accepted so
     far. Trials are always rebuilt from the unmodified input, so region identity
     (`superblock.MergeCandidate.block_lo`) stays valid across the search.

No correctness condition is weakened: the scheduler's own topological and
differential validation and its per-function rollback still run on every trial,
and a region is adopted only after a full codegen + bundle of the whole module.

`APARA_SUPERBLOCK_MODULE_SCOPE=1` restores the pre-R6.7 whole-module decision.
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir_utils import func_slices                                     # noqa: E402
from superblock import (superblock_module, RegionStats,               # noqa: E402
                        merge_candidates_module)
from loopopt.schedule import schedule_module, SchedPolicy           # noqa: E402
from loopopt.oracle_ilp import analyze_module as oracle_analyze     # noqa: E402


_DEFAULT_THRESHOLD = float(os.environ.get('APARA_SUPERBLOCK_THRESHOLD', '0.5'))


class SuperblockSummary:
    def __init__(self):
        self.scheduling_headroom = False
        self.regions_merged = 0
        self.blocks_before = 0
        self.blocks_after = 0
        self.avg_region_before = 0.0
        self.avg_region_after = 0.0
        self.max_region_after = 0
        self.reordered_functions = 0
        self.rollbacks = 0
        self.accepted = False
        self.reason = 'not-attempted'
        self.bundles_before = self.bundles_after = 0
        self.static_before = self.static_after = 0
        self.spills_before = self.spills_after = 0
        # R6.7: acceptance is per region, so how many were offered and taken is
        # now the interesting number, not a single module-wide yes/no.
        self.regions_considered = 0
        self.regions_accepted = 0
        self.regions_rejected = 0


def _module_scope():
    """R6.7 escape hatch: restore pre-R6.7 whole-module acceptance, for A/B."""
    return os.environ.get('APARA_SUPERBLOCK_MODULE_SCOPE', '') not in ('', '0')


def _record(summary, rstats, sstats):
    if rstats is not None:
        summary.regions_merged = rstats.regions_merged
        summary.blocks_before = rstats.blocks_before
        summary.blocks_after = rstats.blocks_after
        summary.avg_region_before = rstats.avg_region_before
        summary.avg_region_after = rstats.avg_region_after
        summary.max_region_after = rstats.max_region_after
    if sstats is not None:
        summary.reordered_functions = sstats.functions_changed
        summary.rollbacks = sstats.rollbacks


# ── the trace scheduler (region formation + existing scheduler) ─────────────────

def superblock_schedule(instrs, policy=SchedPolicy.R24, select=None):
    """Form superblocks, then run the existing scheduler over them. Returns
    (new_instrs, RegionStats, ScheduleStats). The scheduler's own topological +
    differential validation + per-function rollback are reused unchanged.

    `select` (R6.7) restricts which merges are formed; None = all of them."""
    merged, rstats = superblock_module(instrs, select=select)
    scheduled, sstats = schedule_module(merged, policy=policy)
    return scheduled, rstats, sstats


# ── oracle gate ─────────────────────────────────────────────────────────────────

def _has_scheduling_headroom(ir, threshold):
    """The R3.0 oracle predicts scheduling is worth attempting: some innermost
    loop has IPB headroom (theoretical above achieved) a larger region could
    capture. Pure analysis."""
    best = 0.0
    for r in oracle_analyze(ir):
        best = max(best, r.theoretical_ipb - r.achieved_ipb)
    return best >= threshold, best


# ── production integration ──────────────────────────────────────────────────────

def _metrics(ir, global_base):
    try:
        from codegen import CodeGen
        from bundler import bundle_mcode
        cg = CodeGen(global_base=global_base)
        body = cg.generate(copy.deepcopy(ir), global_base=global_base)
        _m, n, b = bundle_mcode(body, schedule=True)
        return True, bool(cg.spilled), n, b
    except Exception:
        return False, True, 0, 0


def apply_superblock_scheduling(prod_ir, global_base=0x400,
                                threshold=_DEFAULT_THRESHOLD, verbose=False):
    """Enlarge scheduling regions in the production IR and reschedule, gated by
    the oracle and accepted only if spill-safe and not bundle-increasing. Returns
    (final_ir, SuperblockSummary). `final_ir is prod_ir` when nothing is applied,
    so the caller can keep the proven output byte-for-byte."""
    summary = SuperblockSummary()

    headroom, _gain = _has_scheduling_headroom(prod_ir, threshold)
    summary.scheduling_headroom = headroom
    if not headroom:
        summary.reason = 'no-scheduling-headroom'
        return prod_ir, summary

    ok0, sp0, st0, bn0 = _metrics(prod_ir, global_base)
    summary.spills_before, summary.static_before, summary.bundles_before = int(sp0), st0, bn0

    def _passes(m, ref_bn):
        """The R3.2 profitability metric, UNCHANGED: it must compile, must not
        introduce a spill, and must not increase the bundle count. R6.7 changes
        only what this is applied TO -- one region increment instead of the whole
        module."""
        okc, spc, _stc, bnc = m
        return okc and not (spc and not sp0) and bnc <= ref_bn

    if _module_scope():
        # Pre-R6.7 behaviour, kept for A/B measurement: form every merge and
        # accept or reject the module as one unit.
        candidate, rstats, sstats = superblock_schedule(prod_ir)
        _record(summary, rstats, sstats)
        if rstats.regions_merged == 0 and sstats.functions_changed == 0:
            summary.reason = 'no-change'
            return prod_ir, summary
        m = _metrics(candidate, global_base)
        if not m[0]:
            summary.reason = 'compile-failed'
            return prod_ir, summary
        if m[1] and not sp0:
            summary.reason = 'spill-increase'
            return prod_ir, summary
        if m[3] > bn0:
            summary.reason = 'bundle-increase'
            return prod_ir, summary
        summary.spills_after, summary.static_after, summary.bundles_after = \
            int(m[1]), m[2], m[3]
        summary.accepted = True
        summary.reason = 'ok'
        return candidate, summary

    # ── R6.7: per-region acceptance ────────────────────────────────────────────
    # Step 0: try the WHOLE set first, which is exactly the pre-R6.7 decision.
    # This matters for more than compile time. Greedy per-region acceptance can
    # only ever reach a subset of the merges, and a subset can be WORSE than the
    # full set: if merge A alone increases bundles but A+B together do not, a
    # greedy pass that offers A first rejects it and can never recover it. So
    # whenever the module-scope answer is "accept", it is taken unchanged, and
    # R6.7 is a strict refinement -- it can only act where R3.2 previously
    # discarded EVERYTHING.  (Measured: without this step, gemm vi8/vu8 regress
    # by 256 ticks each because greedy settles on a smaller merge set.)
    cands = merge_candidates_module(prod_ir)

    whole_ir, whole_r, whole_s = superblock_schedule(prod_ir)
    if whole_r.regions_merged == 0 and whole_s.functions_changed == 0:
        summary.reason = 'no-change'
        return prod_ir, summary
    mw = _metrics(whole_ir, global_base)
    if _passes(mw, bn0):
        _record(summary, whole_r, whole_s)
        summary.regions_considered = len(cands)
        summary.regions_accepted = whole_r.regions_merged
        summary.spills_after, summary.static_after, summary.bundles_after = \
            int(mw[1]), mw[2], mw[3]
        summary.accepted = True
        summary.reason = 'ok'
        return whole_ir, summary

    # The whole set was rejected -- pre-R6.7 this discarded every merge in the
    # module, including profitable ones in regions unrelated to the offender.
    # Decide region by region instead. Trials are always rebuilt from the
    # unmodified `prod_ir`, so a candidate's `block_lo` identity stays valid.
    if len(cands) <= 1:
        # With at most one region there is nothing to decompose: the per-region
        # answer is the whole-module answer, already computed and rejected. Skip
        # the search rather than pay a build to re-derive the same verdict.
        _record(summary, whole_r, whole_s)
        summary.regions_considered = len(cands)
        summary.reason = ('spill-increase' if (mw[1] and not sp0)
                          else 'compile-failed' if not mw[0] else 'bundle-increase')
        return prod_ir, summary
    summary.reason = 'whole-module-rejected'
    best_ir, best_bn, best_sel = prod_ir, bn0, set()
    best_rstats = best_sstats = None

    # Rescheduling with NO merges. R3.2 accepts scheduling-only gains too, so
    # that has to remain available independently of any region decision.
    base_ir, base_r, base_s = superblock_schedule(prod_ir, select=set())
    if base_s.functions_changed:
        m = _metrics(base_ir, global_base)
        if _passes(m, best_bn):
            best_ir, best_bn = base_ir, m[3]
            best_rstats, best_sstats = base_r, base_s

    tried = accepted = 0
    for c in cands:
        tried += 1
        trial_sel = best_sel | {c.block_lo}
        try:
            cand_ir, rstats, sstats = superblock_schedule(prod_ir, select=trial_sel)
            m = _metrics(cand_ir, global_base)
        except Exception:
            continue                        # a region never breaks the build
        if not _passes(m, best_bn):
            continue
        best_ir, best_bn, best_sel = cand_ir, m[3], trial_sel
        best_rstats, best_sstats = rstats, sstats
        accepted += 1

    summary.regions_considered = tried
    summary.regions_accepted = accepted
    summary.regions_rejected = tried - accepted

    if best_ir is prod_ir:
        summary.reason = 'no-region-profitable' if tried else 'no-change'
        return prod_ir, summary

    _record(summary, best_rstats, best_sstats)
    mf = _metrics(best_ir, global_base)
    summary.spills_after, summary.static_after, summary.bundles_after = \
        int(mf[1]), mf[2], mf[3]
    summary.accepted = True
    summary.reason = 'ok'
    if verbose:
        print(f"[superblock] {accepted}/{tried} regions accepted, "
              f"rescheduled {summary.reordered_functions} fns, "
              f"bundles {bn0}->{summary.bundles_after}")
    return best_ir, summary


def format_superblock(summary):
    if not summary.accepted:
        return f"  superblock: not applied ({summary.reason})"
    return ("  superblock: merged {m} regions, rescheduled {r} fns, rollbacks {rb}; "
            "avg region {ab:.1f}->{aa:.1f} blocks; bundles {b0}->{b1}, spills {s0}->{s1}"
            .format(m=summary.regions_merged, r=summary.reordered_functions,
                    rb=summary.rollbacks, ab=summary.avg_region_before,
                    aa=summary.avg_region_after, b0=summary.bundles_before,
                    b1=summary.bundles_after, s0=summary.spills_before,
                    s1=summary.spills_after))

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
R3.0 oracle (scheduling headroom over a threshold), accepted only if the whole
program still compiles with ZERO spills AND the bundle count does not increase,
otherwise the proven input is kept verbatim. `APARA_NO_SUPERBLOCK=1` disables it.
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir_utils import func_slices                                     # noqa: E402
from superblock import superblock_module, RegionStats               # noqa: E402
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


# ── the trace scheduler (region formation + existing scheduler) ─────────────────

def superblock_schedule(instrs, policy=SchedPolicy.R24):
    """Form superblocks, then run the existing scheduler over them. Returns
    (new_instrs, RegionStats, ScheduleStats). The scheduler's own topological +
    differential validation + per-function rollback are reused unchanged."""
    merged, rstats = superblock_module(instrs)
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

    candidate, rstats, sstats = superblock_schedule(prod_ir)
    summary.regions_merged = rstats.regions_merged
    summary.blocks_before = rstats.blocks_before
    summary.blocks_after = rstats.blocks_after
    summary.avg_region_before = rstats.avg_region_before
    summary.avg_region_after = rstats.avg_region_after
    summary.max_region_after = rstats.max_region_after
    summary.reordered_functions = sstats.functions_changed
    summary.rollbacks = sstats.rollbacks

    if rstats.regions_merged == 0 and sstats.functions_changed == 0:
        summary.reason = 'no-change'
        return prod_ir, summary

    okc, spc, stc, bnc = _metrics(candidate, global_base)
    if not okc:
        summary.reason = 'compile-failed'
        return prod_ir, summary
    if spc and not sp0:
        summary.reason = 'spill-increase'
        return prod_ir, summary
    if bnc > bn0:
        summary.reason = 'bundle-increase'
        return prod_ir, summary

    summary.spills_after, summary.static_after, summary.bundles_after = int(spc), stc, bnc
    summary.accepted = True
    summary.reason = 'ok'
    if verbose:
        print(f"[superblock] merged {rstats.regions_merged} regions, "
              f"rescheduled {sstats.functions_changed} fns, bundles {bn0}->{bnc}")
    return candidate, summary


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

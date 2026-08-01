"""
superblock.py -- Superblock / Trace Region Formation (Milestone R3.2, Phase 1-2).

ANALYSIS + a semantics-preserving CFG simplification. It ENLARGES scheduling
regions so the existing scheduler (loopopt/schedule.py) and the bundler can pack
across former basic-block boundaries. It is NOT a scheduler and adds no new
scheduling algorithm.

The safe, no-speculation / no-duplication core of trace scheduling: merge a chain
of basic blocks that form a single-entry / single-exit straight-line region into
ONE region. A block B is merged into its layout-predecessor P iff

    * B has exactly ONE predecessor (P)              -- no side entry (no join)
    * P has exactly ONE successor (B)                -- no side exit  (no split)
    * B immediately follows P in layout (B.lo == P.hi+1)
    * the P->B connector is a fall-through OR a redundant `goto B` to the next line

Then the block boundary is removed (drop B's now-single-predecessor label, and a
redundant `goto`), which is a pure no-op: control already flowed P->B
unconditionally, so no instruction changes the path it executes on. Nothing is
speculated above a branch and nothing is duplicated onto an off-trace path.

The prime target is a counted loop whose body and IV-increment the front end
split with a DEAD label (nothing branches to it): merging them lets the scheduler
overlap the loop body with the increment. Multi-entry regions, conditional side
exits, and irreducible control flow are simply not merged (left as separate
blocks). Reuses analysis.CFG; duplicates no analysis.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir_utils import func_slices                                     # noqa: E402
from analysis import build_cfg                                             # noqa: E402

_CTRL = ('IRJump', 'IRCondJump', 'IRReturn', 'IRHalt')


class RegionStats:
    def __init__(self):
        self.functions = 0
        self.blocks_before = 0
        self.blocks_after = 0
        self.labels_removed = 0
        self.gotos_removed = 0
        self.regions_merged = 0          # blocks absorbed into a predecessor
        self.size_before = 0             # sum of block sizes (== instr count)
        self.max_region_before = 0
        self.max_region_after = 0

    @property
    def avg_region_before(self):
        return self.size_before / self.blocks_before if self.blocks_before else 0.0

    @property
    def avg_region_after(self):
        return self.size_before / self.blocks_after if self.blocks_after else 0.0


def _cname(x):
    return type(x).__name__


class MergeCandidate:
    """One independently acceptable superblock merge: block `block_lo` folded
    into its adjacent single-predecessor.

    R6.7 made acceptance per-region, so a merge needs an identity that survives
    being re-derived from the same unmodified IR. `block_lo` is the block's index
    in the MODULE instruction list (`build_cfg` is given module indices), so it is
    unique across functions and stable as long as the input IR is unchanged --
    which it is, because every trial is built from the original `prod_ir`."""

    __slots__ = ('block_lo', 'drop_label', 'drop_goto')

    def __init__(self, block_lo, drop_label, drop_goto):
        self.block_lo = block_lo
        self.drop_label = drop_label        # index of B's leading label, or None
        self.drop_goto = drop_goto          # index of the redundant goto, or None

    def indices(self):
        return {i for i in (self.drop_label, self.drop_goto) if i is not None}

    def __repr__(self):
        return f'<merge block@{self.block_lo}>'


def merge_candidates(instrs, lo, hi):
    """Every merge available in one function slice, as independent candidates.

    This is the enumeration `_mergeable_removals` used to collapse immediately
    into one set of indices; the loop and its legality conditions are unchanged."""
    cfg = build_cfg(instrs, lo, hi)
    out = []
    for b in cfg.blocks:
        if len(b.preds) != 1:
            continue                                # multi-entry (join) -> keep
        p = cfg.blocks[b.preds[0]]
        if len(p.succs) != 1 or p.succs[0] != b.id:
            continue                                # predecessor side-exits -> keep
        if b.lo != p.hi + 1:
            continue                                # not adjacent -> would move code
        phi = instrs[p.hi]
        goto_idx = None
        if _cname(phi) == 'IRJump':
            # redundant `goto B` to the very next block
            if phi.label != (instrs[b.lo].name if _cname(instrs[b.lo]) == 'IRLabel'
                             else None):
                continue
            goto_idx = p.hi
        elif _cname(phi) in _CTRL:
            continue                                # cond branch / return / halt
        # drop B's leading label (it now has a single fall-through predecessor)
        label_idx = b.lo if _cname(instrs[b.lo]) == 'IRLabel' else None
        out.append(MergeCandidate(b.lo, label_idx, goto_idx))
    return out, cfg.blocks


def merge_candidates_module(instrs):
    """Every merge available anywhere in the module (module-stable ids)."""
    out = []
    for (lo, hi) in func_slices(instrs):
        cands, _blocks = merge_candidates(instrs, lo, hi)
        out.extend(cands)
    return out


def _mergeable_removals(instrs, lo, hi, select=None):
    """Return (label_indices_to_drop, goto_indices_to_drop, n_merged, blocks).

    `select` (R6.7) restricts the merges applied to those whose `block_lo` is in
    the given set; None means "every available merge", which is the pre-R6.7
    behaviour exactly."""
    cands, blocks = merge_candidates(instrs, lo, hi)
    if select is not None:
        cands = [c for c in cands if c.block_lo in select]
    drop_label = {c.drop_label for c in cands if c.drop_label is not None}
    drop_goto = {c.drop_goto for c in cands if c.drop_goto is not None}
    return drop_label, drop_goto, len(cands), blocks


def form_superblocks(instrs, lo, hi, stats=None, select=None):
    """Form superblocks for one function slice. Returns the (possibly shortened)
    slice list. Pure CFG simplification; semantics-preserving."""
    if stats is None:
        stats = RegionStats()
    drop_label, drop_goto, merged, blocks = _mergeable_removals(instrs, lo, hi,
                                                                select=select)
    stats.functions += 1
    stats.blocks_before += len(blocks)
    stats.size_before += (hi - lo + 1)
    stats.max_region_before = max(stats.max_region_before,
                                  max((b.hi - b.lo + 1 for b in blocks), default=0))
    drop = drop_label | drop_goto
    new_slice = [instrs[k] for k in range(lo, hi + 1) if k not in drop]
    stats.labels_removed += len(drop_label)
    stats.gotos_removed += len(drop_goto)
    stats.regions_merged += merged
    stats.blocks_after += (len(blocks) - merged)
    # recompute the largest region after merging (rebuild CFG on the new slice)
    if new_slice:
        cfg2 = build_cfg(new_slice, 0, len(new_slice) - 1)
        stats.max_region_after = max(stats.max_region_after,
                                     max((b.hi - b.lo + 1 for b in cfg2.blocks),
                                         default=0))
    return new_slice


def superblock_module(instrs, select=None):
    """Form superblocks across a whole module. Returns (new_instrs, RegionStats).
    Rebuilds by concatenating each function's simplified slice so globals /
    inter-function code are preserved.

    `select` (R6.7) restricts which merges are applied, so acceptance can be
    decided per region instead of per module. None = every merge (pre-R6.7)."""
    stats = RegionStats()
    out = []
    prev_end = 0
    for (lo, hi) in func_slices(instrs):
        out.extend(instrs[prev_end:lo])
        prev_end = hi + 1
        out.extend(form_superblocks(instrs, lo, hi, stats, select=select))
    out.extend(instrs[prev_end:])
    return out, stats

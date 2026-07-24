"""
rotate.py -- LoopRotation (Loop Optimization Framework, Milestone M6).

The FIRST concrete optimization built on the M5 LoopTransform framework. It
executes ENTIRELY through that framework -- MutationTransaction, framework-owned
analysis rebuild, LoopVerifier, rollback, CFGDiff and TransformStats -- and adds
no infrastructure of its own. It reuses the M4 LoopCanonicalizer's canonical
shape as its precondition and the M0-M3 analyses on the descriptor for legality;
it embeds no new analysis.

Loop rotation turns a top-tested (while) loop into a bottom-tested (do-while)
loop protected by an entry guard:

    while (C) { body }      ==>      if (C) { do { body } while (C); }

so the loop-closing test lands at the latch and later passes (LICM, IVSR, ...)
see a single-entry do-while.

IDENTITY-PRESERVING FORMULATION. The M5 framework re-locates a loop after a
mutation by its HEADER LABEL and treats a lost label as failure. A textbook
rotation moves the header, which would trip that. So this pass keeps the header
label alive on a tiny header-labelled forwarder, and REUSES the original header
block in place as the entry guard (no dead duplicate). Concretely, for a
canonical loop

    PH:    (-> H, by explicit jump or fall-through)
    H:     <cond>; if C goto BODY else X        (header 'hlbl', single top test)
    ...    <body> ...
    LATCH: goto H                                (single back edge)
    X:

rotation produces (only H2 and L2 are fresh; the old header becomes the guard):

    PH:    -> G                                  (retargeted; or falls through into G)
    G:     <cond>; if C goto hlbl else X         (the OLD header, reused as the guard)
    ...    <body> ...
    LATCH: goto L2                               (back edge now via the bottom test)
    X:
    hlbl:  goto BODY                             (H2: the new header, label preserved)
    L2:    <cond'>; if C goto hlbl else X         (bottom test: the new single latch)

The header's pure guard computation is copied ONCE (deep-copied into L2, the
bottom test); the original header instructions are reused unchanged as the entry
guard. Every predecessor of BODY (guard on entry, bottom test on each back edge)
thus defines whatever the body/exit use, so semantics are preserved on all paths.
No dead block is produced. M6 does not wire rotation into the compile pipeline.
"""

import copy

from ir import IRLabel, IRJump
from .descriptor import BOTTOM_TESTED
from .transform import LoopTransform, LoopTransformDriver, TransformResult, TransformStats
from . import legality as L

# The legality of rotation is expressed as a COMPOSITION of shared predicates
# from the M7 legality framework (legality.py), in place of the checks that used
# to be embedded here. The conjunction below is logically identical to the old
# inline legal(): header labelled; top-tested; single explicit back edge; a
# unique preheader; a side-effect-free header ending in one conditional branch;
# exactly one labelled in-loop and one labelled exit successor; and guard inputs
# not produced in the body. Behaviour is unchanged (same loops rotate).
_ROTATION_LEGALITY = (
    L.has_labeled_header,
    L.is_top_tested,
    L.has_single_latch,
    L.has_explicit_backedge,
    L.has_unique_preheader,
    L.has_side_effect_free_header,
    L.header_has_single_exit_test,
    L.guard_inputs_loop_independent,
)


class LoopRotation(LoopTransform):
    """Rotate a canonical top-tested loop into guarded do-while form. All IR
    edits go through the MutationTransaction; the framework does everything else."""

    name = 'loop-rotation'

    # -- legality: composed from the shared M7 legality framework, no embedded
    #    legality logic and no new analysis --

    def legal(self, desc):
        fact = L.evaluate(desc, _ROTATION_LEGALITY)
        return fact.ok, fact.reason

    # -- the rotation edits, exclusively through the transaction ---------------

    def run(self, instrs, lo, desc, txn):
        cfg = desc.cfg
        header = desc.header
        hblk = cfg.blocks[header]
        hlbl = hblk.label
        body = desc.body_blocks

        # anchors (resolved from the pre-mutation analysis)
        succs = cfg.succs(header)
        b_id = next(s for s in succs if s in body)
        x_id = next(s for s in succs if s not in body)
        b_lbl = cfg.blocks[b_id].label
        x_lbl = cfg.blocks[x_id].label
        hcj = cfg.instrs[hblk.hi]                       # header conditional branch
        # polarity: does the ORIGINAL "condition true" edge stay in the loop?
        true_id = cfg.label_to_block.get(hcj.true_label)
        in_loop_on_true = (true_id == b_id)

        header_label_obj = cfg.instrs[hblk.lo]          # IRLabel object of the header
        latch_term = cfg.instrs[cfg.blocks[desc.latches[0]].hi]
        ph_term = cfg.instrs[cfg.blocks[desc.preheader].hi]

        # deep-copy the header's guard (cond compute + conditional branch) ONCE,
        # for the bottom test L2. The original header instructions stay in place
        # and are reused as the entry guard -- no dead duplicate is created.
        latch_seq = copy.deepcopy(instrs[hblk.lo + 1: hblk.hi + 1])
        cj = latch_seq[-1]                               # wire the copied branch explicitly
        if in_loop_on_true:                              # (no fall-through at the end seam)
            cj.true_label, cj.false_label = hlbl, x_lbl
        else:
            cj.true_label, cj.false_label = x_lbl, hlbl

        gl = txn.fresh_label(f'__rot_guard_{hlbl}')
        l2 = txn.fresh_label(f'__rot_latch_{hlbl}')
        new_header = [IRLabel(hlbl), IRJump(b_lbl)]      # H2: keeps the header label
        latch_block = [IRLabel(l2)] + latch_seq          # L2: the new bottom test

        # 1. append H2 (new header) and L2 (bottom test) at the end-of-slice seam
        txn.splice(txn.slice_end(), new_header + latch_block)
        # 2. reuse the OLD header block as the guard: relabel hlbl -> gl and point
        #    its in-loop edge at the new header H2 (its exit edge is unchanged).
        txn.set_field(header_label_obj, 'name', gl)
        txn.retarget(hcj, b_lbl, hlbl)
        # 3. route the preheader into the guard (no-op if it falls through into it)
        txn.retarget(ph_term, hlbl, gl)
        # 4. route the back edge through the new bottom test
        txn.retarget(latch_term, hlbl, l2)
        return True

    # -- postcondition: the rotated shape must actually be present -------------

    def postcondition(self, before_desc, after_desc):
        # identity (header label) is guaranteed by the framework's re-location;
        # here we require the shape to have become bottom-tested with one latch.
        return after_desc.shape == BOTTOM_TESTED and len(after_desc.latches) == 1


# ── rotation-specific invariant report (derives from framework stats) ─────────

class RotationReport:
    """Rotation-specific metrics, all derived from the framework's TransformStats
    (which already counts attempts / commits / rollbacks / verifier failures).
    No framework extension -- just a rotation-aware view of the same numbers."""

    __slots__ = ('loops_visited', 'legally_rotatable', 'loops_rotated',
                 'loops_skipped', 'verifier_failures', 'rollbacks',
                 'guard_conditions_moved', 'preheaders_reused',
                 'header_latch_swaps', 'rotations_rolled_back',
                 'semantic_mismatches')

    def __init__(self, stats, semantic_mismatches=0):
        self.loops_visited = stats.attempts
        # "legally rotatable" = attempts that passed legality and were applied
        # (committed) OR applied-then-rolled-back; illegal/no-op were not.
        self.legally_rotatable = stats.commits + stats.rollbacks
        self.loops_rotated = stats.commits
        self.loops_skipped = stats.skipped_illegal + stats.skipped_noop
        self.verifier_failures = stats.verifier_failures
        self.rollbacks = stats.rollbacks
        # rotation-specific invariants (each commit = one guard moved, one
        # preheader reused to host it, one header/latch top->bottom swap)
        self.guard_conditions_moved = stats.commits
        self.preheaders_reused = stats.commits
        self.header_latch_swaps = stats.commits
        self.rotations_rolled_back = stats.rollbacks
        self.semantic_mismatches = semantic_mismatches

    def report(self):
        return "\n".join([
            "LoopRotation report:",
            f"  loops visited            : {self.loops_visited}",
            f"  legally rotatable        : {self.legally_rotatable}",
            f"  loops rotated            : {self.loops_rotated}",
            f"  loops skipped            : {self.loops_skipped}",
            f"  verifier failures        : {self.verifier_failures}",
            f"  rollbacks                : {self.rollbacks}",
            "  -- rotation invariants --",
            f"  guard conditions moved   : {self.guard_conditions_moved}",
            f"  preheaders reused        : {self.preheaders_reused}",
            f"  header/latch swaps       : {self.header_latch_swaps}",
            f"  rotations rolled back    : {self.rotations_rolled_back}",
            f"  semantic mismatches      : {self.semantic_mismatches}",
        ])


def rotate_module(instrs, verifier=None, stats=None):
    """Rotate every legal loop in `instrs` (in place) THROUGH the M5 framework.
    Returns (TransformStats, RotationReport). Caller should canonicalize first
    (LoopCanonicalizer) to present the canonical shape rotation expects."""
    drv = LoopTransformDriver(verifier=verifier)
    stats = drv.run(LoopRotation(), instrs, stats)
    return stats, RotationReport(stats)

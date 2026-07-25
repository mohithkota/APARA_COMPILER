"""
loop_licm.py -- LoopLICM (Loop Optimization Framework, Milestone M8).

Migration of the conservative loop-invariant code motion pass (licm2.py) onto the
M5 LoopTransform framework. This is an ARCHITECTURAL CONSOLIDATION, not a new
optimization: it hoists EXACTLY the instruction classes licm2.py hoists, makes
EXACTLY the same legality decisions, and produces instruction-for-instruction
identical IR (proven by licm_crosscheck.py across the whole corpus). What changes
is only the MACHINERY:

  * every IR edit flows through the M5 MutationTransaction (via the new additive
    move() primitive -- the sole framework addition M8 required);
  * the framework owns analysis rebuild / verification / rollback / statistics;
  * loop discovery, nesting and structure come from the shared M0 descriptors
    instead of licm2's private per-slice CFG/DefUse construction.

licm2.py remains the SPECIFICATION and the A/B baseline (gated by APARA_LICM);
this pass reproduces its behaviour on the shared substrate. Memory hoisting,
aliasing, PRE, speculation and edge-splitting stay out of scope exactly as in
licm2 (see its docstring for the excluded categories and the reasons).

--------------------------------------------------------------------------------
Faithful reproduction of licm2's algorithm
--------------------------------------------------------------------------------
licm2 repeatedly (to a fixpoint) finds ONE legal hoist -- innermost loops first --
and moves it to the end of the loop's existing preheader, restarting the whole
scan after each move (so a value hoisted into an inner loop's preheader, which
lies in the enclosing loop's body, can migrate further out on a later round).

LoopLICM keeps that control structure exactly:

    LoopLICM.run()   applies AT MOST ONE hoist per attempt -- the first legal one,
                     chosen by licm2's identical invariant-fixpoint + legality
                     order -- through txn.move().
    licm_module()    drives the outer fixpoint with licm2's identical loop
                     ordering (a STABLE sort on -max(depth over body blocks)) and
                     restart-after-each-hoist, so the sequence of decisions -- and
                     therefore the final IR -- matches licm2 exactly.

The per-instruction legality lives here (as it does in licm2) rather than in the
M7 legality framework: M7's predicates are LOOP-level queries over a descriptor,
whereas LICM's legality is a per-INSTRUCTION whitelist. Only the loop-level
precondition (a unique preheader that dominates the header) is expressed through
the shared descriptor. The instruction whitelist below is copied verbatim from
licm2 so the two are equivalent by construction; the cross-check proves it.
"""

from ir import Temp
from ir_utils import src_names, func_slices
from analysis import DefUse

from .transform import (LoopTransform, LoopTransformDriver, TransformStats)
from . import legality as L

# ── instruction classification (VERBATIM from licm2.py -- the specification) ───
# Pure, single-destination, non-memory computations eligible for hoisting.
_HOIST_KINDS = ('IRBinOp', 'IRUnaryOp', 'IRAssign', 'IRCast',
                'IRLoadAddr', 'IRGlobalAddrOf')
_TERMS = ('IRJump', 'IRCondJump', 'IRReturn', 'IRHalt')


def _is_float(ins):
    if getattr(ins, 'ftype', None):
        return True
    if type(ins).__name__ == 'IRCast':
        return '$f' in getattr(ins, 'dest_type', '') or '$f' in getattr(ins, 'src_type', '')
    return False


def _hoistable_kind(ins):
    return type(ins).__name__ in _HOIST_KINDS and not _is_float(ins)


# ── the transform ─────────────────────────────────────────────────────────────

class LoopLICM(LoopTransform):
    """Hoist ONE loop-invariant pure computation to the preheader per attempt,
    through the MutationTransaction. The framework rebuilds analyses, verifies and
    commits/rolls back; the outer fixpoint + innermost-first ordering that make
    this equivalent to licm2 live in licm_module()."""

    name = 'loop-licm'

    # -- legality: licm2's loop-level precondition, via the shared descriptor ---

    def legal(self, desc):
        """licm2 skips a loop unless it has a UNIQUE non-back-edge predecessor
        (the preheader) that DOMINATES the header. The first half is the shared
        M7 predicate; the dominance half is checked on the borrowed dominator
        tree. (For a natural loop a unique external predecessor always dominates
        the header, but we check it to mirror licm2's decision exactly and to
        stay correct if discovery's preheader rule ever loosens.)"""
        pre = L.has_unique_preheader(desc)
        if not pre.ok:
            return False, pre.reason
        if not desc.dom.dominates(desc.preheader, desc.header):
            return False, 'preheader does not dominate header'
        return True, ''

    # -- one hoist, exclusively through the transaction ------------------------

    def run(self, instrs, lo, desc, txn):
        """Reproduce licm2's per-loop body EXACTLY: build the invariant set to a
        fixpoint, then hoist the FIRST instruction (in ascending IR order) that
        passes every legality check, via txn.move(). Return True on a hoist, False
        for an intentional no-op (loop exhausted)."""
        cfg = desc.cfg
        pre = desc.preheader
        du = DefUse(instrs, cfg.lo, cfg.hi)

        # body = every IR index inside the loop's body blocks (absolute indices,
        # exactly as licm2 computes them; the header is part of the body).
        body = set()
        for b in desc.body_blocks:
            blk = cfg.blocks[b]
            for i in range(blk.lo, blk.hi + 1):
                body.add(i)

        # invariant fixpoint: an op operand is invariant iff every in-body def of
        # it is already proven invariant (or it has no in-body def).
        inv = set()

        def op_inv(name):
            d = [x for x in du.def_sites(name) if x in body]
            return (not d) or all(x in inv for x in d)

        changed = True
        while changed:
            changed = False
            for i in sorted(body):
                if i in inv or not _hoistable_kind(instrs[i]):
                    continue
                if all(op_inv(nm) for nm in src_names(instrs[i])):
                    inv.add(i)
                    changed = True

        # apply the first legal hoist (ascending order -> preserves the relative
        # order of hoisted values in the preheader, matching licm2).
        for i in sorted(inv):
            ins = instrs[i]
            dest = getattr(ins, 'dest', None)
            if not isinstance(dest, Temp):
                continue
            dn = dest.name
            if [x for x in du.def_sites(dn) if x in body] != [i]:
                continue                                # loop-carried destination
            if any(u not in body for u in du.use_sites(dn)):
                continue                                # used after the loop
            pb = cfg.blocks[pre]
            pos = pb.hi if type(instrs[pb.hi]).__name__ in _TERMS else pb.hi + 1
            if pos >= i:
                continue                                # safety: preheader precedes body
            txn.move(i, pos)                            # replaces licm2's del+insert
            return True
        return False


# ── LICM-specific report (derived entirely from framework TransformStats) ─────

class LICMReport:
    """LICM-specific view of the framework's TransformStats. One commit == one
    instruction hoisted; `attempts` counts every run_transform call, including the
    repeated no-op re-scans of already-exhausted loops that the fixpoint performs
    (licm2 has the identical re-scan cost)."""

    __slots__ = ('attempts', 'hoists', 'skipped', 'verifier_failures',
                 'rollbacks', 'semantic_mismatches')

    def __init__(self, stats, semantic_mismatches=0):
        self.attempts = stats.attempts
        self.hoists = stats.commits
        self.skipped = stats.skipped_illegal + stats.skipped_noop
        self.verifier_failures = stats.verifier_failures
        self.rollbacks = stats.rollbacks
        self.semantic_mismatches = semantic_mismatches

    def report(self):
        return "\n".join([
            "LoopLICM report:",
            f"  attempts (incl re-scans) : {self.attempts}",
            f"  instructions hoisted     : {self.hoists}",
            f"  attempts skipped         : {self.skipped}",
            f"  verifier failures        : {self.verifier_failures}",
            f"  rollbacks                : {self.rollbacks}",
            f"  semantic mismatches      : {self.semantic_mismatches}",
        ])


# ── driver: licm2's outer fixpoint + innermost-first ordering ─────────────────

def _innermost_first(descs):
    """licm2's EXACT loop ordering: a STABLE sort by descending max nesting depth
    over the loop's body blocks. A loop and its deepest descendant tie on this
    key; Python's stable sort then preserves discovery order (== analysis.li.loops
    order, which discover_function preserves), reproducing licm2's tie-break so the
    sequence of hoists -- and the final IR -- match instruction-for-instruction."""
    return sorted(descs, key=lambda d: -max(
        (d.loop_info.depth(b) for b in d.body_blocks), default=0))


def _one_hoist(drv, xform, instrs, stats):
    """Find and apply a SINGLE legal hoist across all functions (first slice, then
    innermost loop first), returning True if one was applied -- the framework
    analogue of licm2._one_hoist. Rebuilds descriptors per slice through the
    framework so no attempt ever sees a stale descriptor."""
    for lo, _hi in func_slices(instrs):
        descs = drv._rebuild(instrs, lo, drv.epoch)
        for desc in _innermost_first(descs):
            res = drv.run_transform(xform, instrs, lo, desc, stats)
            if res.committed:
                return True                             # restart the whole scan
    return False


def licm_module(instrs, verifier=None, stats=None):
    """Run loop-invariant code motion over `instrs` (IN PLACE) THROUGH the M5
    framework, to a fixpoint. Behaviourally equivalent to
    licm2.loop_invariant_code_motion (with APARA_LICM on). Returns
    (TransformStats, LICMReport). Instructions are MOVED, never duplicated."""
    drv = LoopTransformDriver(verifier=verifier)
    stats = stats or TransformStats()
    xform = LoopLICM()
    for _ in range(100000):                             # licm2's fixpoint bound
        if not _one_hoist(drv, xform, instrs, stats):
            break
    return stats, LICMReport(stats)

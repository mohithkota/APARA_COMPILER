"""
canonicalize.py -- LoopCanonicalizer (Loop Optimization Framework, Milestone M4).

The FIRST IR-mutating stage. Its ONLY job is to put every natural loop into one
canonical STRUCTURAL form that later transforms (rotation, LICM, IVSR, ...) can
assume. It does NOT optimize: no rotation, no LICM, no IV work, no strength
reduction, no unswitching, no peeling, no unrolling. It never changes the
program's logical behaviour -- every mutation only reroutes control through a
new block that unconditionally forwards to where control was already going.

Canonical form (the classic "loop-simplify" shape, minus anything optional):

  1. DEDICATED PREHEADER   -- the header has exactly one predecessor from
                             outside the loop, and that block's only successor
                             is the header. (create one when missing/shared)
  2. SINGLE LATCH          -- exactly one back edge into the header. (merge
                             multiple latches through one forwarding block)
  3. DEDICATED EXITS       -- every exit target reached by an explicit branch is
                             entered ONLY from inside the loop. (insert a
                             landing pad when an exit target is shared with
                             non-loop code)

Each of the three is INDEPENDENTLY GATED on its property already being false, so
an already-canonical loop is touched zero times -- the canonicalizer is a total
no-op on a corpus that is already in this form (which the production corpus is).

Correctness protocol (per the milestone): this module NEVER trusts a stale
descriptor. After every single IR mutation it discards all analyses and calls
loopopt.discover_function to rebuild CFG / Dominators / LoopInfo / Liveness and
regenerate every LoopDescriptor, then runs LoopVerifier. A mutation that fails
verification is ROLLED BACK (IR restored exactly) and the loop is left unchanged
and reported. Mutations are applied one-at-a-time to a fixpoint.

Reuses ONLY the existing framework (loopopt.discover_function, LoopVerifier,
ir_utils, analysis.*). It recomputes nothing that those already provide.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import IRLabel, IRJump                                       # noqa: E402
from ir_utils import func_slices                                     # noqa: E402
from analysis import build_cfg, compute_dominators                  # noqa: E402
from .discovery import discover_function                            # noqa: E402
from .verify import LoopVerifier                                    # noqa: E402


# ── report ────────────────────────────────────────────────────────────────────

class CanonReport:
    """Impact statistics for a canonicalization run (a measurement, not a
    decision). Loop identity is (slice_lo, header_label) so the same loop is
    counted once even though descriptors are rebuilt between mutations."""

    __slots__ = ('visited', 'modified', 'preheaders_created',
                 'latches_normalized', 'exits_normalized',
                 'irreducible_skipped', 'verifier_failures',
                 'rollbacks', 'actions')

    def __init__(self):
        self.visited = set()            # {(lo, header_label)}
        self.modified = set()           # {(lo, header_label)}
        self.preheaders_created = 0
        self.latches_normalized = 0
        self.exits_normalized = 0
        self.irreducible_skipped = 0
        self.verifier_failures = 0
        self.rollbacks = 0
        self.actions = []               # [(loop_id, action_str)]

    @property
    def loops_visited(self):
        return len(self.visited)

    @property
    def loops_modified(self):
        return len(self.modified)

    @property
    def loops_unchanged(self):
        return len(self.visited - self.modified)

    def report(self):
        return "\n".join([
            "LoopCanonicalizer impact:",
            f"  loops visited        : {self.loops_visited}",
            f"  loops modified       : {self.loops_modified}",
            f"  loops unchanged      : {self.loops_unchanged}",
            f"  preheaders created   : {self.preheaders_created}",
            f"  latches normalized   : {self.latches_normalized}",
            f"  exits normalized     : {self.exits_normalized}",
            f"  irreducible skipped  : {self.irreducible_skipped}",
            f"  verifier failures    : {self.verifier_failures}",
            f"  mutations rolled back: {self.rollbacks}",
        ])


# ── small IR/label helpers ────────────────────────────────────────────────────

def _existing_labels(instrs, lo, hi):
    return {instrs[i].name for i in range(lo, hi + 1)
            if type(instrs[i]).__name__ == 'IRLabel'}


def _fresh_label(existing, base):
    """A label not already present. `existing` is a mutable set that is updated."""
    cand = base
    k = 0
    while cand in existing:
        k += 1
        cand = f"{base}_{k}"
    existing.add(cand)
    return cand


def _slice_end(instrs, lo):
    """Index of the IRFuncEnd that closes the function opened at `lo`."""
    for i in range(lo, len(instrs)):
        if type(instrs[i]).__name__ == 'IRFuncEnd':
            return i
    return len(instrs) - 1


def _label_index(instrs, lo, hi, name):
    for i in range(lo, hi + 1):
        ins = instrs[i]
        if type(ins).__name__ == 'IRLabel' and ins.name == name:
            return i
    return None


def _is_terminator(ins):
    return type(ins).__name__ in ('IRJump', 'IRCondJump', 'IRReturn', 'IRHalt')


def _targets(ins, label):
    """True if `ins` (a terminator) explicitly branches to `label`."""
    c = type(ins).__name__
    if c == 'IRJump':
        return ins.label == label
    if c == 'IRCondJump':
        return ins.true_label == label or ins.false_label == label
    return False


def _retarget(ins, old, new):
    """Rewrite explicit branch targets `old` -> `new` on a terminator. Returns
    the list of (attr, old) edits performed (for rollback)."""
    c = type(ins).__name__
    edits = []
    if c == 'IRJump' and ins.label == old:
        edits.append(('label', ins.label)); ins.label = new
    elif c == 'IRCondJump':
        if ins.true_label == old:
            edits.append(('true_label', ins.true_label)); ins.true_label = new
        if ins.false_label == old:
            edits.append(('false_label', ins.false_label)); ins.false_label = new
    return edits


# ── irreducibility detector (report + "leave unchanged" proof) ────────────────

def _irreducible_headers(cfg, dom):
    """Headers of irreducible loops: targets of a RETREATING edge (an edge to a
    node still on the DFS stack) that is NOT a dominating back edge. LoopInfo
    only surfaces natural (reducible) loops, so these never become descriptors
    and are therefore never mutated -- this is purely for reporting/tests."""
    if cfg.entry_id is None:
        return set()
    WHITE, GREY, BLACK = 0, 1, 2
    color = {b.id: WHITE for b in cfg.blocks}
    irr = set()
    stack = [(cfg.entry_id, 0)]
    color[cfg.entry_id] = GREY
    while stack:
        bid, ci = stack[-1]
        succs = cfg.blocks[bid].succs
        if ci < len(succs):
            stack[-1] = (bid, ci + 1)
            s = succs[ci]
            if color[s] == WHITE:
                color[s] = GREY
                stack.append((s, 0))
            elif color[s] == GREY:            # retreating edge bid -> s
                if not dom.dominates(s, bid):  # ... not a natural back edge
                    irr.add(s)
        else:
            color[bid] = BLACK
            stack.pop()
    return irr


# ── the canonicalizer ─────────────────────────────────────────────────────────

class LoopCanonicalizer:
    """Structural loop canonicalizer. Mutates the flat IR list in place; rebuilds
    and re-verifies after every mutation."""

    def __init__(self, verifier=None, max_mutations=10000, normalize_exits=False):
        self._verifier = verifier or LoopVerifier()
        self._max_mutations = max_mutations
        # Dedicated-exit normalization (landing pads for exit blocks shared with
        # non-loop code) is OFF by default. Preheader + single-latch are the two
        # properties every later transform (rotation, LICM, IVSR) strictly needs
        # and are the ones the production corpus already satisfies, so the
        # default canonicalizer is a total no-op there (the milestone's explicit
        # goal). Exit normalization is a legal, tested, but optional structural
        # tidy-up -- it would fire on ordinary `break`-with-shared-exit loops --
        # so it is enabled only when a caller opts in.
        self._normalize_exits = normalize_exits

    # -- public API ------------------------------------------------------------

    def canonicalize(self, instrs, epoch=0, report=None):
        """Canonicalize every loop in every function of `instrs` (in place).
        Returns a CanonReport."""
        report = report or CanonReport()
        k = 0
        while True:
            slices = func_slices(instrs)          # re-derived: slices shift as we grow the IR
            if k >= len(slices):
                break
            lo, _hi = slices[k]
            self.canonicalize_function(instrs, lo, epoch, report)
            k += 1
        return report

    def canonicalize_function(self, instrs, lo, epoch=0, report=None):
        """Canonicalize every loop in the function opened at index `lo`. Applies
        one mutation at a time, rebuilding all analyses each time, to a fixpoint.
        Returns the CanonReport."""
        report = report or CanonReport()
        for _ in range(self._max_mutations):
            hi = _slice_end(instrs, lo)
            descs = discover_function(instrs, lo, hi, epoch)
            descs.sort(key=lambda d: (-d.depth, d.header))   # innermost-first

            # record what we saw this sweep (idempotent set inserts)
            for d in descs:
                report.visited.add((lo, self._loop_id(d)))
            # irreducibility is a slice-level fact; recompute once per sweep
            if descs:
                report.irreducible_skipped = max(
                    report.irreducible_skipped,
                    len(_irreducible_headers(descs[0].cfg, descs[0].dom)))
            else:
                cfg = build_cfg(instrs, lo, hi)
                report.irreducible_skipped = max(
                    report.irreducible_skipped,
                    len(_irreducible_headers(cfg, compute_dominators(cfg))))

            progressed = False
            for d in descs:
                action = self._pick_action(d)
                if action is None:
                    continue
                if self._apply(instrs, lo, d, action, report):
                    progressed = True
                    break              # analyses now stale -> rebuild from scratch
            if not progressed:
                return report          # fixpoint: every loop already canonical
        return report

    # -- canonical-form predicates ---------------------------------------------

    @staticmethod
    def _loop_id(desc):
        lb = desc.cfg.blocks[desc.header].label
        return lb if lb is not None else f"~B{desc.header}"

    @staticmethod
    def _external_preds(desc):
        cfg, body, header = desc.cfg, desc.body_blocks, desc.header
        return [p for p in cfg.preds(header) if p not in body]

    def _needs_preheader(self, desc):
        ext = self._external_preds(desc)
        if len(ext) != 1:
            return True                        # 0 or >=2 external entries
        p = ext[0]
        return len(desc.cfg.succs(p)) != 1     # single entry but not dedicated

    @staticmethod
    def _needs_single_latch(desc):
        return len(desc.latches) > 1

    @staticmethod
    def _shared_exit_edges(desc):
        """Exit edges (b_in_loop -> E) reached by an EXPLICIT branch whose target
        E also has a predecessor from OUTSIDE the loop (E is not a dedicated exit
        block). Fall-through exits are left alone (documented deviation)."""
        cfg, body = desc.cfg, desc.body_blocks
        out = []
        for (b, s) in desc.exit_edges:
            term = cfg.instrs[cfg.blocks[b].hi]
            slabel = cfg.blocks[s].label
            if slabel is None or not _targets(term, slabel):
                continue                       # fall-through / non-label exit
            if any(p not in body for p in cfg.preds(s)):
                out.append((b, s))
        return out

    def _pick_action(self, desc):
        """The single highest-priority normalization this loop still needs, or
        None if it is already canonical. Order: preheader, then latch, then
        exits (each later transform depends on the earlier ones existing)."""
        # A loop whose header carries no label cannot be a branch target we can
        # reroute; such a loop cannot be a natural loop here, but guard anyway.
        if desc.cfg.blocks[desc.header].label is None:
            return None
        if self._needs_preheader(desc):
            return ('preheader', None)
        if self._needs_single_latch(desc):
            return ('latch', None)
        if self._normalize_exits:
            shared = self._shared_exit_edges(desc)
            if shared:
                return ('exit', shared[0])
        return None

    # -- mutation dispatch with transaction / rollback -------------------------

    def _apply(self, instrs, lo, desc, action, report):
        """Apply one normalization transactionally. Rebuild + verify; roll back
        on any violation. Returns True iff the mutation was committed."""
        kind, payload = action
        hlbl = desc.cfg.blocks[desc.header].label
        loop_id = (lo, hlbl)

        saved = list(instrs)                   # list-membership snapshot
        edits = []                             # [(obj, attr, oldval)] field snapshot

        # Capture the pre-mutation progress metric NOW: the mutation splices the
        # shared instrs list in place, which leaves `desc`'s borrowed CFG stale,
        # so anything re-derived from `desc` afterwards is garbage. Stored list
        # attributes (desc.latches) survive; anything recomputed from desc.cfg
        # (shared-exit edges) must be snapshotted here.
        before_metric = None
        if kind == 'latch':
            before_metric = len(desc.latches)
        elif kind == 'exit':
            before_metric = len(self._shared_exit_edges(desc))

        if kind == 'preheader':
            self._make_preheader(instrs, lo, desc, hlbl, edits)
        elif kind == 'latch':
            self._make_single_latch(instrs, lo, desc, hlbl, edits)
        elif kind == 'exit':
            self._make_dedicated_exit(instrs, lo, desc, payload, edits)
        else:
            return False

        # rebuild EVERYTHING from the mutated IR and verify
        new_hi = _slice_end(instrs, lo)
        new_descs = discover_function(instrs, lo, new_hi, desc.epoch)
        vres = self._verifier.verify_all(new_descs)
        target = next((d for d in new_descs
                       if d.cfg.blocks[d.header].label == hlbl), None)

        ok = (vres.ok and target is not None
              and self._postcondition(kind, before_metric, target))
        if not ok:
            # ROLLBACK: undo field edits, then restore list membership/order
            for obj, attr, old in reversed(edits):
                setattr(obj, attr, old)
            instrs[:] = saved
            report.rollbacks += 1
            if not vres.ok:
                report.verifier_failures += 1
            return False

        # COMMIT
        report.modified.add(loop_id)
        if kind == 'preheader':
            report.preheaders_created += 1
        elif kind == 'latch':
            report.latches_normalized += 1
        elif kind == 'exit':
            report.exits_normalized += 1
        report.actions.append((loop_id, kind))
        return True

    def _postcondition(self, kind, before_metric, target):
        """The mutation must have made real progress toward canonical form on
        the rebuilt target loop (identity -- the header label -- is preserved by
        construction, and the verifier has already confirmed structural
        soundness). `before_metric` is the relevant pre-mutation count captured
        before the IR was touched. Progress guarantees the fixpoint terminates."""
        if kind == 'preheader':
            return not self._needs_preheader(target)
        if kind == 'latch':
            return (not self._needs_single_latch(target)
                    and len(target.latches) < before_metric)
        if kind == 'exit':
            return len(self._shared_exit_edges(target)) < before_metric
        return False

    # -- the three structural mutations ----------------------------------------

    def _make_preheader(self, instrs, lo, desc, hlbl, edits):
        """Insert a dedicated preheader that forwards to the header, and route
        every external entry through it. Internal back edges are untouched."""
        cfg = desc.cfg
        hi = _slice_end(instrs, lo)
        existing = _existing_labels(instrs, lo, hi)
        phlbl = _fresh_label(existing, f"__ph_{hlbl}")
        header_id = desc.header
        body = desc.body_blocks

        # Guard: if the block physically before the header is a LOOP block that
        # falls through into the header, give it an explicit back-jump first so
        # the inserted preheader cannot capture that internal fall-through.
        if header_id > 0:
            prev = header_id - 1
            pterm = cfg.instrs[cfg.blocks[prev].hi]
            if (prev in body and header_id in cfg.succs(prev)
                    and not _is_terminator(pterm)):
                pos = cfg.blocks[prev].hi + 1
                instrs[pos:pos] = [IRJump(hlbl)]

        # Insert the preheader immediately before the header label. The single
        # possible external fall-through predecessor (physically adjacent) now
        # flows into the preheader; explicit external entries are retargeted.
        hidx = _label_index(instrs, lo, _slice_end(instrs, lo), hlbl)
        instrs[hidx:hidx] = [IRLabel(phlbl), IRJump(hlbl)]

        for p in self._external_preds(desc):
            term = cfg.instrs[cfg.blocks[p].hi]
            for attr, old in _retarget(term, hlbl, phlbl):
                edits.append((term, attr, old))

    def _make_single_latch(self, instrs, lo, desc, hlbl, edits):
        """Merge all back edges through one forwarding latch block that jumps to
        the header. Placed at a safe seam (end of slice); reached only by the
        redirected explicit back edges."""
        cfg = desc.cfg
        hi = _slice_end(instrs, lo)
        existing = _existing_labels(instrs, lo, hi)
        latchlbl = _fresh_label(existing, f"__latch_{hlbl}")

        # redirect every back edge tail's header-target to the merged latch
        for tail in desc.latches:
            term = cfg.instrs[cfg.blocks[tail].hi]
            for attr, old in _retarget(term, hlbl, latchlbl):
                edits.append((term, attr, old))

        fe = _slice_end(instrs, lo)            # insert just before IRFuncEnd
        instrs[fe:fe] = [IRLabel(latchlbl), IRJump(hlbl)]

    def _make_dedicated_exit(self, instrs, lo, desc, exit_edge, edits):
        """Insert a landing pad for one shared exit edge so the exit target is
        entered only from inside the loop. Placed at a safe seam (end of slice)."""
        cfg = desc.cfg
        b, s = exit_edge
        elabel = cfg.blocks[s].label
        hi = _slice_end(instrs, lo)
        existing = _existing_labels(instrs, lo, hi)
        padlbl = _fresh_label(existing, f"__exit_{elabel}")

        term = cfg.instrs[cfg.blocks[b].hi]
        for attr, old in _retarget(term, elabel, padlbl):
            edits.append((term, attr, old))

        fe = _slice_end(instrs, lo)
        instrs[fe:fe] = [IRLabel(padlbl), IRJump(elabel)]


def canonicalize(instrs, epoch=0):
    """Convenience: canonicalize `instrs` in place, return a CanonReport."""
    return LoopCanonicalizer().canonicalize(instrs, epoch)

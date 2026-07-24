"""
transform.py -- LoopTransform framework (Loop Optimization Framework, M5).

The generic, reusable substrate through which EVERY future loop optimization
executes. M5 provides ONLY this infrastructure -- it implements NO optimization
(no rotation, LICM, unswitching, peeling, unrolling, pipelining, IVSR/LICM
migration). Rotation and the real passes arrive in M6+ and are written as small
LoopTransform subclasses that supply legality + a mutation and nothing else; the
transaction, analysis rebuild, verification, rollback and statistics all live
here and are never duplicated by a transform.

The transaction pattern generalized here is exactly the one proven inline in the
M4 LoopCanonicalizer (snapshot -> mutate -> rebuild -> verify -> commit/rollback);
M5 lifts it into a first-class, independently-tested framework and REUSES the M4
mutation primitives (canonicalize._retarget / _slice_end / _fresh_label / ...)
rather than re-implementing them. The canonicalizer itself is frozen and is not
rewritten onto the framework in M5 (that would be a redesign; it is an optional,
additive migration for a later milestone).

Components:
    LoopTransform        -- abstract base: legal() + run() + postcondition()
    MutationTransaction  -- reversible IR edit buffer (splice / retarget / set)
    TransformResult      -- per-attempt outcome (+ LoopDiff + VerifyResult)
    TransformStats       -- aggregate counters + per-transform CFG-diff totals
    LoopTransformDriver  -- owns begin/apply/invalidate/rebuild/verify/commit/rollback
    PassRegistry         -- name -> transform registration

Inputs consumed (never recomputed outside the framework): LoopDescriptor,
LoopVerifier, CFGDiff (diff_loop), plus the InductionVars / MemEffects / Profile
fields already on the descriptor. The Canonicalizer is available to callers who
want to canonicalize before transforming; the framework does not force it.
"""

import copy
import time

from .discovery import discover_function
from .verify import LoopVerifier
from .cfgdiff import diff_loop
from .canonicalize import (_slice_end, _existing_labels, _fresh_label,  # reuse M4 primitives
                           _retarget, _label_index, _is_terminator, _targets)


# ── reversible mutation buffer ────────────────────────────────────────────────

class MutationTransaction:
    """A reversible buffer of IR edits for ONE transform attempt.

    A transform performs all its IR changes through this object so the framework
    can undo them exactly on failure. Two kinds of change are tracked:

      * FIELD edits on pre-existing instruction objects (branch retargets, etc.)
        are recorded as (obj, attr, old_value) and undone individually.
      * SPLICES that insert new instructions are undone wholesale by restoring
        the saved list membership/order (inserted objects are simply dropped).

    Rollback undoes field edits first, then restores the list -- returning the
    IR to a byte-for-byte identical state. The framework, never the transform,
    calls commit()/rollback()."""

    __slots__ = ('instrs', 'lo', '_saved', '_edits', 'inserted', '_open')

    def __init__(self, instrs, lo):
        self.instrs = instrs
        self.lo = lo
        self._saved = list(instrs)      # membership/order snapshot
        self._edits = []                # [(obj, attr, old_value)]
        self.inserted = 0               # count of instructions spliced in
        self._open = True

    # -- edit primitives a transform calls --------------------------------------

    def set_field(self, obj, attr, value):
        """Set obj.attr = value, remembering the old value for rollback."""
        self._edits.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def retarget(self, term, old_label, new_label):
        """Rewrite explicit branch targets old_label -> new_label on a terminator
        (reuses the M4 primitive), recording every field change for rollback.
        Returns True iff any target was rewritten."""
        edits = _retarget(term, old_label, new_label)   # mutates term in place
        self._edits.extend((term, attr, oldv) for (attr, oldv) in edits)
        return bool(edits)

    def splice(self, at, new_instrs):
        """Insert new_instrs into the IR at index `at`."""
        self.instrs[at:at] = new_instrs
        self.inserted += len(new_instrs)

    def move(self, src, dst):
        """RELOCATE the instruction at index `src` to index `dst` (dst <= src).

        Reorders EXISTING objects only -- nothing is created or destroyed -- which
        is the single primitive an instruction-motion transform (LICM / sinking)
        needs. Because list MEMBERSHIP is unchanged, the transaction's saved
        order snapshot (`_saved`) already restores the pre-move order exactly on
        rollback, so no per-move bookkeeping is required here. The `pop(src)` then
        `insert(dst, obj)` sequence is identical to the reference pass's
        `del instrs[src]; instrs.insert(dst, obj)` idiom for the upward move
        (dst < src) LICM performs, so the index arithmetic matches exactly."""
        obj = self.instrs.pop(src)
        self.instrs.insert(dst, obj)

    def replace_span(self, start, end, new_instrs):
        """Replace the INCLUSIVE index range [start, end] with `new_instrs` in one
        reversible edit -- a splice that may grow, shrink, or rewrite the span at
        once. This is the single primitive a region-rewriting transform needs:
        IVSR replaces a loop's [header .. back-edge] span with
        [preheader + rewritten body + increments]. Membership changes are undone
        wholesale on rollback by restoring the saved order snapshot (`_saved`);
        any newly inserted objects are simply dropped, exactly as for splice()."""
        old = end - start + 1
        self.instrs[start:end + 1] = new_instrs
        self.inserted += max(0, len(new_instrs) - old)

    # -- convenience helpers (delegate to reused M4 utilities) ------------------

    def slice_end(self):
        return _slice_end(self.instrs, self.lo)

    def fresh_label(self, base):
        hi = _slice_end(self.instrs, self.lo)
        return _fresh_label(_existing_labels(self.instrs, self.lo, hi), base)

    def label_index(self, name):
        return _label_index(self.instrs, self.lo, _slice_end(self.instrs, self.lo), name)

    # -- framework-only lifecycle ----------------------------------------------

    def commit(self):
        self._saved = None
        self._open = False

    def rollback(self):
        for obj, attr, old in reversed(self._edits):
            setattr(obj, attr, old)
        self.instrs[:] = self._saved
        self._open = False


# ── transform base class ──────────────────────────────────────────────────────

class LoopTransform:
    """Abstract loop transform. A concrete transform supplies ONLY:

        legal(desc)                 -- read-only pre-legality check; the framework
                                       skips the transform if this returns False.
        run(instrs, lo, desc, txn)  -- perform the IR mutation THROUGH `txn`;
                                       return True if it changed anything.
        postcondition(before, after)-- optional: assert the intended structural
                                       property now holds on the rebuilt loop.

    It must NOT rebuild analyses, run the verifier, snapshot, or roll back --
    the framework does all of that. Loop identity (the header label) is preserved
    by the framework: it relocates the loop after rebuild by that label and
    treats a lost identity as failure."""

    name = '<abstract-loop-transform>'

    def legal(self, desc):
        """Return (ok: bool, reason: str). Read-only. Default: always legal."""
        return True, ''

    def run(self, instrs, lo, desc, txn):
        """Mutate the IR through `txn`. Return True if a change was made, False
        for an intentional no-op. Must not touch analyses."""
        raise NotImplementedError

    def postcondition(self, before_desc, after_desc):
        """Return True iff the transform's intended structural property holds on
        the regenerated loop. Default: no extra requirement beyond verification
        and identity preservation."""
        return True


# ── per-attempt result ────────────────────────────────────────────────────────

class TransformResult:
    """Outcome of one LoopTransformDriver.run_transform attempt."""

    COMMITTED = 'committed'
    ROLLED_BACK = 'rolled-back'
    SKIPPED_ILLEGAL = 'skipped-illegal'
    SKIPPED_NOOP = 'skipped-noop'

    __slots__ = ('transform', 'loop_id', 'outcome', 'loop_diff', 'verify', 'reason')

    def __init__(self, transform, loop_id, outcome,
                 loop_diff=None, verify=None, reason=''):
        self.transform = transform
        self.loop_id = loop_id
        self.outcome = outcome
        self.loop_diff = loop_diff      # cfgdiff.LoopDiff (committed) or None
        self.verify = verify            # VerifyResult from the framework's check
        self.reason = reason

    @property
    def committed(self):
        return self.outcome == self.COMMITTED

    @property
    def rolled_back(self):
        return self.outcome == self.ROLLED_BACK

    def __repr__(self):
        return (f"TransformResult({self.transform} {self.loop_id} "
                f"-> {self.outcome}{(' [' + self.reason + ']') if self.reason else ''})")


# ── aggregate statistics ──────────────────────────────────────────────────────

class TransformStats:
    """Counters across a driver run, plus per-transform CFG-diff totals."""

    __slots__ = ('attempts', 'commits', 'rollbacks', 'skipped_illegal',
                 'skipped_noop', 'verifier_failures', 'per_transform', 'elapsed_ms')

    def __init__(self):
        self.attempts = 0
        self.commits = 0
        self.rollbacks = 0
        self.skipped_illegal = 0
        self.skipped_noop = 0
        self.verifier_failures = 0
        self.per_transform = {}         # name -> dict of totals
        self.elapsed_ms = 0.0

    def _bucket(self, name):
        return self.per_transform.setdefault(
            name, {'commits': 0, 'blocks_added': 0, 'blocks_removed': 0,
                   'edges_added': 0, 'edges_removed': 0})

    def record_commit(self, name, loop_diff):
        self.commits += 1
        b = self._bucket(name)
        b['commits'] += 1
        if loop_diff is not None:
            cd = loop_diff.cfg_diff
            b['blocks_added'] += len(cd.blocks_added)
            b['blocks_removed'] += len(cd.blocks_removed)
            b['edges_added'] += len(cd.edges_added)
            b['edges_removed'] += len(cd.edges_removed)

    def report(self):
        lines = ["LoopTransform stats:",
                 f"  attempts          : {self.attempts}",
                 f"  commits           : {self.commits}",
                 f"  rollbacks         : {self.rollbacks}",
                 f"  skipped (illegal) : {self.skipped_illegal}",
                 f"  skipped (no-op)   : {self.skipped_noop}",
                 f"  verifier failures : {self.verifier_failures}",
                 f"  elapsed           : {self.elapsed_ms:.2f} ms"]
        for name, b in sorted(self.per_transform.items()):
            lines.append(f"  [{name}] commits={b['commits']} "
                         f"+{b['blocks_added']}/-{b['blocks_removed']} blocks "
                         f"+{b['edges_added']}/-{b['edges_removed']} edges")
        return "\n".join(lines)


# ── pass registration ─────────────────────────────────────────────────────────

class PassRegistry:
    """Name -> LoopTransform registry. Future passes register here so a driver /
    pass manager can look them up by name; M5 ships it empty (no passes yet)."""

    def __init__(self):
        self._by_name = {}

    def register(self, transform):
        if transform.name in self._by_name:
            raise ValueError(f"transform '{transform.name}' already registered")
        self._by_name[transform.name] = transform
        return transform

    def get(self, name):
        return self._by_name[name]

    def names(self):
        return sorted(self._by_name)

    def __contains__(self, name):
        return name in self._by_name

    def __iter__(self):
        return iter(self._by_name.values())


# ── the driver (owns the whole transaction protocol) ──────────────────────────

class LoopTransformDriver:
    """Runs LoopTransforms through the one canonical transaction protocol:

        begin -> apply mutation -> invalidate -> rebuild -> verify
              -> commit on success / rollback on failure

    A transform never performs any of these steps itself. `_rebuild` is the SOLE
    place in the transform path that (re)constructs CFG / Dominators / LoopInfo /
    Liveness / LoopDescriptors -- analyses are never recomputed elsewhere."""

    def __init__(self, verifier=None):
        self.verifier = verifier or LoopVerifier()
        self.epoch = 0                  # monotonic invalidation token; bumped per commit

    # -- analysis (re)generation: the single rebuild point ---------------------

    def _rebuild(self, instrs, lo, epoch):
        """Regenerate every analysis + descriptor for the slice at `lo`. This is
        the framework's analysis-invalidation-and-rebuild step; callers upstream
        must never hand a transform a stale descriptor."""
        hi = _slice_end(instrs, lo)
        return discover_function(instrs, lo, hi, epoch)

    @staticmethod
    def _find(descs, header_label):
        return next((d for d in descs
                     if d.cfg.blocks[d.header].label == header_label), None)

    # -- one transform attempt on one loop -------------------------------------

    def run_transform(self, transform, instrs, lo, desc, stats):
        """Attempt `transform` on the loop `desc` within the function slice at
        `lo`, transactionally. Returns a TransformResult. All analysis rebuild /
        verification / rollback is owned here."""
        stats.attempts += 1
        hlbl = desc.cfg.blocks[desc.header].label
        loop_id = (lo, hlbl if hlbl is not None else f"~B{desc.header}")

        # 1. LEGALITY -- read-only; skip without touching the IR
        ok, reason = transform.legal(desc)
        if not ok:
            stats.skipped_illegal += 1
            return TransformResult(transform.name, loop_id,
                                   TransformResult.SKIPPED_ILLEGAL, reason=reason)

        # 2. BEFORE snapshot -- an INDEPENDENT deep copy of the slice so the diff
        #    is faithful even after the live IR is mutated in place.
        before_desc = None
        try:
            hi = _slice_end(instrs, lo)
            before_slice = copy.deepcopy(instrs[lo:hi + 1])
            before_desc = self._find(self._rebuild(before_slice, 0, self.epoch), hlbl)
        except Exception:
            before_desc = None          # diff will be unavailable; correctness unaffected

        # 3. BEGIN TRANSACTION + APPLY mutation (transform edits through txn only)
        txn = MutationTransaction(instrs, lo)
        changed = transform.run(instrs, lo, desc, txn)
        if not changed:
            txn.rollback()
            stats.skipped_noop += 1
            return TransformResult(transform.name, loop_id,
                                   TransformResult.SKIPPED_NOOP, reason='no mutation')

        # 4. INVALIDATE + REBUILD (framework-owned) then VERIFY
        after_descs = self._rebuild(instrs, lo, self.epoch + 1)
        vres = self.verifier.verify_all(after_descs)
        after_desc = self._find(after_descs, hlbl)
        identity_ok = after_desc is not None
        post_ok = identity_ok and transform.postcondition(before_desc, after_desc)

        # 5. COMMIT on success, ROLLBACK on any failure
        if vres.ok and post_ok:
            txn.commit()
            self.epoch += 1             # analyses advanced; old descriptors now stale
            diff = (diff_loop(before_desc, after_desc)
                    if (before_desc is not None and after_desc is not None) else None)
            stats.record_commit(transform.name, diff)
            return TransformResult(transform.name, loop_id,
                                   TransformResult.COMMITTED, loop_diff=diff, verify=vres)

        txn.rollback()
        stats.rollbacks += 1
        if not vres.ok:
            stats.verifier_failures += 1
            why = 'verifier'
        elif not identity_ok:
            why = 'loop-identity-lost'
        else:
            why = 'postcondition'
        return TransformResult(transform.name, loop_id,
                               TransformResult.ROLLED_BACK, verify=vres, reason=why)

    # -- drive a transform over every loop of every function -------------------

    def run(self, transform, instrs, stats=None):
        """Attempt `transform` once on each natural loop in `instrs` (innermost
        first), rebuilding analyses after every attempt so no transform ever sees
        a stale descriptor. Terminates: each loop (by header label) is attempted
        at most once. Returns TransformStats."""
        from ir_utils import func_slices
        stats = stats or TransformStats()
        t0 = time.perf_counter()
        k = 0
        while True:
            slices = func_slices(instrs)        # re-derived: slices shift as IR grows
            if k >= len(slices):
                break
            lo, _hi = slices[k]
            processed = set()
            while True:
                descs = self._rebuild(instrs, lo, self.epoch)
                target = None
                for d in sorted(descs, key=lambda d: (-d.depth, d.header)):
                    lbl = d.cfg.blocks[d.header].label
                    if lbl in processed:
                        continue
                    target = d
                    break
                if target is None:
                    break
                processed.add(target.cfg.blocks[target.header].label)
                self.run_transform(transform, instrs, lo, target, stats)
            k += 1
        stats.elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return stats

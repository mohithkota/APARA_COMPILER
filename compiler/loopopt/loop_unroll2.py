"""
loop_unroll2.py -- LoopUnrollFactor2 (Research milestone R1.2).

The FIRST production-quality loop-unroll TRANSFORM. R1.1 built the analysis
(eligibility / legality / profitability) and a no-op transform; R1.2 performs
REAL factor-2 unrolling of the loops R1.1 already accepts. It is intentionally
conservative: correctness dominates, it unrolls ONLY the simplest countable
shape, and it always emits a remainder loop so odd and even trip counts are
handled uniformly.

R1.1 is FROZEN. This milestone does NOT modify LoopUnroll: LoopUnrollFactor2
SUBCLASSES it, inheriting `legal()` (structural eligibility) and `analyze()`
(the profitability model) verbatim, and overrides only `run()`. Everything else
-- the transaction, analysis rebuild, verification, rollback, statistics -- is
the frozen M5 framework and is not touched.

Supported shape (a strict subset of R1.1-eligible; anything else is a no-op):
  * top-tested, exactly two blocks (header + single latch body block),
  * single preheader, single latch, single exit (R1.1 legality),
  * innermost, clean primary induction variable with positive step,
  * a header guard `iv < C` / `iv <= C` with a CONSTANT bound and standard
    while-polarity (the in-loop edge is the guard's true edge),
  * a side-effect-free header (so the guard's loads may be recomputed).

Transformation (factor 2), for header guard `iv < C`, step s, one body copy B:

    head:  <setup>                       head:  <setup>
           if iv<C goto body else exit           if iv < C-s goto body else REM_ph
    body:  <B>                    ==>     body:  <B copy1>
           goto head                             <setup'>            (reload IV = iv+s)
    exit:                                        <B copy2>           (IV -> reloaded)
                                                 goto head
                                          REM_ph: goto REM_head      (remainder preheader)
                                          REM_head: <setup''>
                                                    if iv<C goto REM_body else exit
                                          REM_body: <B copy3>
                                                    goto REM_head
                                          exit:

Correctness argument:
  * The IV is memory-backed: copy1 writes iv+s to the slot; copy2 RELOADS the
    slot (via a freshened copy of the header's side-effect-free setup) so it
    operates on iv+s and writes iv+2s. Every duplicated temp is renamed into a
    private namespace, so no definition collides.
  * The main guard `iv < C-s` runs the body only when BOTH iterations are in
    range (iv < C and iv+s < C), i.e. floor(remaining/2) times.
  * The remainder loop is an exact copy of the original loop (guard `iv < C`); it
    drains the 0-or-1 leftover iterations. Total iterations are unchanged.
  * All observable effects (stores) are replayed in the same order for the same
    IV values, so behaviour is identical -- verified by differential execution.

R1.2 does NOT wire this into the production pipeline and does NOT unroll beyond
factor 2 (larger factors / symbolic bounds are R1.3).
"""

import copy as _copy

from ir import IRLabel, IRJump, IRCondJump, Const, Temp
from ir_utils import dest_names
from .descriptor import TOP_TESTED
from .transform import LoopTransform, LoopTransformDriver, TransformStats
from .loop_unroll import LoopUnroll
from . import legality as L

_FACTOR = 2


def _walk_temps(ins):
    """Yield every Temp object referenced by `ins` (dest or src position)."""
    for val in vars(ins).values():
        if isinstance(val, Temp):
            yield val
        elif isinstance(val, list):
            for e in val:
                if isinstance(e, Temp):
                    yield e


class LoopUnrollFactor2(LoopUnroll):
    """Factor-2 loop unrolling with a remainder loop, through the M5 framework."""

    name = 'loop-unroll-f2'

    def __init__(self):
        # private fresh-name counters and the set of loop labels this pass itself
        # created (its remainder loops), which it must never re-unroll.
        self._tn = 0
        self._ln = 0
        self._synthetic = set()

    # -- fresh names in a private namespace (never collides with existing names) -
    def _fresh_temp(self):
        self._tn += 1
        return f'__u2t{self._tn}'

    def _fresh_label(self):
        self._ln += 1
        return f'__u2L{self._ln}'

    def _clone_region(self, span):
        """Deep-copy the loop-region `span`, renaming into a private namespace
        every TEMP it defines and every LABEL it defines (remapping in-span uses
        and in-span branch targets to match). Names used but not DEFINED in the
        span -- temps that dominate the region, and branch targets OUTSIDE it (the
        header back-edge, the loop exit) -- are left untouched. Returns
        (clone, temp_map, label_map)."""
        clone = _copy.deepcopy(list(span))
        tmap, lmap = {}, {}
        for ins in clone:
            for n in dest_names(ins):
                tmap.setdefault(n, self._fresh_temp())
            if type(ins).__name__ == 'IRLabel':
                lmap.setdefault(ins.name, self._fresh_label())
        for ins in clone:
            for t in _walk_temps(ins):
                if t.name in tmap:
                    t.name = tmap[t.name]
            c = type(ins).__name__
            if c == 'IRLabel' and ins.name in lmap:
                ins.name = lmap[ins.name]
            elif c == 'IRJump' and ins.label in lmap:
                ins.label = lmap[ins.label]
            elif c == 'IRCondJump':
                if ins.true_label in lmap:
                    ins.true_label = lmap[ins.true_label]
                if ins.false_label in lmap:
                    ins.false_label = lmap[ins.false_label]
        return clone, tmap, lmap

    @staticmethod
    def _retarget_backedge(instrs, old, new):
        """Rewrite an in-list clone's back-edge target `old` -> `new` (used to
        point a remainder copy's latch at the remainder header)."""
        for ins in instrs:
            c = type(ins).__name__
            if c == 'IRJump' and ins.label == old:
                ins.label = new
            elif c == 'IRCondJump':
                if ins.true_label == old:
                    ins.true_label = new
                if ins.false_label == old:
                    ins.false_label = new

    # -- the real transform (overrides LoopUnroll's no-op run) ------------------
    def run(self, instrs, lo, desc, txn):
        cfg = desc.cfg
        header = desc.header
        hblk = cfg.blocks[header]
        hlbl = hblk.label

        # never re-unroll a remainder loop this pass produced
        if hlbl in self._synthetic:
            return False

        # ---- structural preconditions (a strict, conservative subset) --------
        if desc.shape != TOP_TESTED or len(desc.latches) != 1:
            return False
        latch = desc.latches[0]
        if latch == header or latch not in desc.body_blocks or header not in desc.body_blocks:
            return False

        hlo, hhi = hblk.lo, hblk.hi
        header_cond = instrs[hhi]
        if type(header_cond).__name__ != 'IRCondJump' or header_cond.ftype is not None:
            return False
        if header_cond.op not in ('<', '<='):
            return False
        if not isinstance(header_cond.right, Const) or not isinstance(header_cond.left, Temp):
            return False

        # the loop payload = every body block except the header; it must be one
        # contiguous instruction run placed immediately after the header, and its
        # single exit is the header's (so its only external target is the header).
        pay_blocks = [b for b in desc.body_blocks if b != header]
        ranges = sorted((cfg.blocks[b].lo, cfg.blocks[b].hi) for b in pay_blocks)
        p_lo, p_hi = ranges[0][0], ranges[-1][1]
        if p_lo != hhi + 1:
            return False
        if sum(hi - lo + 1 for lo, hi in ranges) != (p_hi - p_lo + 1):
            return False                          # payload not contiguous

        # standard while-polarity: the guard's TRUE edge enters the payload's
        # first block; the FALSE edge is the loop exit.
        body_label = header_cond.true_label
        first_pay = cfg.blocks[min(pay_blocks, key=lambda b: cfg.blocks[b].lo)]
        if body_label is None or first_pay.label != body_label:
            return False
        exit_label = header_cond.false_label
        if exit_label is None or exit_label == body_label:
            return False

        # single back-edge: the one latch jumps (unconditionally) to the header.
        latch_term = instrs[cfg.blocks[latch].hi]
        if type(latch_term).__name__ != 'IRJump' or latch_term.label != hlbl:
            return False

        # clean primary IV with a positive step.
        iv_slot = desc.primary_iv
        if iv_slot is None or iv_slot not in desc.basic_ivs:
            return False
        step = desc.basic_ivs[iv_slot].step
        if step <= 0:
            return False

        # header must be a pure guard (its setup is safe to recompute in copy2).
        if not L.has_side_effect_free_header(desc).ok:
            return False

        # ---- profitability: reuse R1.1 model EXACTLY (no thresholds changed) --
        rep = self.analyze(desc)
        if not (rep.eligible and rep.profit.should_unroll):
            return False

        # ---- build the transform --------------------------------------------
        bound = header_cond.right.value
        main_bound = bound - step
        iv_name = header_cond.left.name           # the guard's IV temp

        # region = header setup (reload) ++ payload; captured before any splice.
        setup_src = list(instrs[hlo + 1:hhi])     # header setup (excl. label & guard)
        payload_src = list(instrs[p_lo:p_hi + 1])
        region_src = setup_src + payload_src
        n_setup = len(setup_src)

        # (1) SECOND in-line iteration: reload the IV, then replay the payload.
        #     Its payload back-edge still targets the (main) header -- correct.
        c2, _t2, l2 = self._clone_region(region_src)
        entry2 = self._fresh_label()
        copy2_blocks = [IRLabel(entry2)] + c2

        # (2) REMAINDER loop: a fresh full copy, its back-edge re-pointed at the
        #     remainder header, guarded by the ORIGINAL bound.
        cr, tr, lr = self._clone_region(region_src)
        rem_setup = cr[:n_setup]
        rem_payload = cr[n_setup:]
        rem_ph = self._fresh_label()
        rem_head = self._fresh_label()
        rem_body_first = lr[body_label]
        # the remainder payload's back-edge must return to the remainder header,
        # not the main header (its only external target is the back-edge).
        self._retarget_backedge(rem_payload, hlbl, rem_head)
        rem_iv = tr[iv_name]
        rem_cond = IRCondJump(Temp(rem_iv), header_cond.op, Const(bound),
                              rem_body_first, exit_label)
        rem_blocks = ([IRLabel(rem_ph), IRJump(rem_head), IRLabel(rem_head)]
                      + rem_setup + [rem_cond] + rem_payload)

        # (a) append copy2 and the remainder loop at the end of the slice.
        txn.splice(txn.slice_end(), copy2_blocks + rem_blocks)
        # (b) route the original latch's back-edge into copy2 (the second copy).
        txn.retarget(latch_term, hlbl, entry2)
        # (c) tighten the main guard and route its exit into the remainder loop.
        txn.set_field(header_cond, 'right', Const(main_bound))
        txn.retarget(header_cond, exit_label, rem_ph)

        # never touch the loops / entry we just synthesized
        self._synthetic.update({rem_ph, rem_head, rem_body_first, entry2, hlbl})
        return True

    def postcondition(self, before_desc, after_desc):
        # framework already guarantees loop identity + a clean verifier; require
        # the main loop to remain a single-latch top-tested loop (sanity guard).
        return (after_desc.shape == TOP_TESTED and len(after_desc.latches) == 1
                and after_desc.is_innermost)


# ── module driver + report ────────────────────────────────────────────────────

class UnrollF2Report:
    """Factor-2 unroll metrics, derived from the framework's TransformStats."""

    __slots__ = ('loops_visited', 'loops_unrolled', 'loops_skipped',
                 'verifier_failures', 'rollbacks', 'remainder_loops')

    def __init__(self, stats):
        self.loops_visited = stats.attempts
        self.loops_unrolled = stats.commits
        self.loops_skipped = stats.skipped_illegal + stats.skipped_noop
        self.verifier_failures = stats.verifier_failures
        self.rollbacks = stats.rollbacks
        self.remainder_loops = stats.commits        # one remainder loop per unroll

    def report(self):
        return "\n".join([
            "LoopUnrollFactor2 report:",
            f"  loops visited      : {self.loops_visited}",
            f"  loops unrolled     : {self.loops_unrolled}",
            f"  remainder loops    : {self.remainder_loops}",
            f"  loops skipped      : {self.loops_skipped}",
            f"  verifier failures  : {self.verifier_failures}",
            f"  rollbacks          : {self.rollbacks}",
        ])


def unroll_module(instrs, verifier=None, stats=None):
    """Factor-2 unroll every profitable loop in `instrs` (in place) THROUGH the
    M5 framework. Returns (TransformStats, UnrollF2Report)."""
    drv = LoopTransformDriver(verifier=verifier)
    stats = drv.run(LoopUnrollFactor2(), instrs, stats)
    return stats, UnrollF2Report(stats)

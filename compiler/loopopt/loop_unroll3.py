"""
loop_unroll3.py -- LoopUnrollFactor2R13 (Research milestone R1.3).

R1.3 IMPROVES the code quality of the R1.2 factor-2 unroller WITHOUT changing the
unroll factor, legality, or profitability. Everything through R1.2 is frozen:
this is a new transform that SUBCLASSES `LoopUnrollFactor2` and inherits its
helpers (clone/rename, back-edge retarget, the synthetic-loop guard) verbatim,
overriding only `run()`. R1.2 remains available and comparable.

Four quality improvements, all behaviour-preserving:

  1. INDUCTION-VARIABLE SUBSTITUTION.  R1.2's second copy RELOADS the IV from its
     stack slot, which the first copy just wrote -- a store->load serial chain the
     bundler cannot overlap. R1.3 reuses the value the first copy ALREADY computed
     (the source temp of the first copy's IV-update store, == iv+step) directly as
     a register, rewriting the second copy's IV-slot loads to that temp. The slot
     is clean (no alias), so the value is identical; the cross-copy memory
     dependency disappears.

  2. DEAD REMAINDER ELIMINATION.  When the trip count is a compile-time constant
     that is evenly divisible by the factor (T % 2 == 0), the remainder loop can
     never iterate, so it is omitted entirely (the main guard's exit edge stays
     the original loop exit).

  3. SYMBOLIC BOUNDS.  R1.2 required a constant guard bound. R1.3 also unrolls a
     loop whose bound is a loop-invariant temp (legality already permits it -- the
     M7 `guard_inputs_loop_independent` fact holds): it computes `bound - step`
     ONCE in the preheader and guards the main loop against it, keeping the
     remainder loop on the original bound.

  4. CLEANUP.  Substitution leaves the second copy's IV address-computations
     (loadaddr) dead; those, and the now-redundant reloads, are dropped from the
     generated copy (only from code THIS pass creates -- never the original).

Correctness is preserved exactly (differential execution proves it); the frozen
M5 framework still owns rebuild / verify / rollback. Not wired into the pipeline;
no unroll-and-jam, no software pipelining, factor stays 2.
"""

from collections import Counter

from ir import IRLabel, IRJump, IRCondJump, IRBinOp, IRAssign, Const, Temp
from ir_utils import src_names
from .descriptor import TOP_TESTED
from .transform import LoopTransformDriver
from .loop_unroll2 import LoopUnrollFactor2, _walk_temps, _FACTOR
from . import legality as L


class LoopUnrollFactor2R13(LoopUnrollFactor2):
    """Factor-2 unrolling with IV substitution, dead-remainder elimination, and
    symbolic-bound support. Same framework, same legality, same profitability."""

    name = 'loop-unroll-f2-r13'

    # -- IV substitution + cleanup on a generated copy -------------------------
    def _substitute_iv(self, blocks, iv_slot, iv_next_name):
        """In a CLONED region, replace loads of the IV slot that read the value at
        region entry (== `iv_next_name`, the value the previous copy stored) with
        that register, and drop the address computations that become dead. Only
        loads BEFORE the region's single IV-slot STORE are substituted: a load
        AFTER that store observes THIS copy's own updated IV (e.g. `s += i` after
        `i++`) and must keep reloading. Operates only on freshly-cloned code."""
        addr_temps = {ins.dest.name for ins in blocks
                      if type(ins).__name__ == 'IRLoadAddr' and ins.fp_offset == iv_slot}
        # position of the (single) IV-slot store; loads after it see the new value
        store_pos = len(blocks)
        for i, ins in enumerate(blocks):
            if (type(ins).__name__ == 'IRStore' and isinstance(ins.base, Temp)
                    and ins.base.name in addr_temps
                    and isinstance(ins.offset, Const) and ins.offset.value == 0):
                store_pos = i
                break
        # the IV-slot loads BEFORE that store (base is an IV address, zero offset)
        remove = set()
        remap = {}
        for i, ins in enumerate(blocks):
            if i >= store_pos:
                break
            if (type(ins).__name__ == 'IRLoad' and isinstance(ins.base, Temp)
                    and ins.base.name in addr_temps
                    and isinstance(ins.offset, Const) and ins.offset.value == 0):
                remove.add(id(ins))
                remap[ins.dest.name] = iv_next_name
        # which IV-address temps are still used by something we keep?
        still_used = Counter()
        for ins in blocks:
            if id(ins) in remove:
                continue
            for s in src_names(ins):
                if s in addr_temps:
                    still_used[s] += 1
        kept = []
        for ins in blocks:
            if id(ins) in remove:
                continue
            if (type(ins).__name__ == 'IRLoadAddr' and ins.fp_offset == iv_slot
                    and still_used[ins.dest.name] == 0):
                continue                              # dead IV address computation
            kept.append(ins)
        # rewrite uses of the removed loads' results to the reused register value
        for ins in kept:
            for t in _walk_temps(ins):
                if t.name in remap:
                    t.name = remap[t.name]
        return kept

    # -- the improved transform ------------------------------------------------
    def run(self, instrs, lo, desc, txn):
        cfg = desc.cfg
        header = desc.header
        hblk = cfg.blocks[header]
        hlbl = hblk.label
        if hlbl in self._synthetic:
            return False

        # ---- structural preconditions (identical shape to R1.2) --------------
        if desc.shape != TOP_TESTED or len(desc.latches) != 1:
            return False
        latch = desc.latches[0]
        if latch == header or latch not in desc.body_blocks or header not in desc.body_blocks:
            return False
        hlo, hhi = hblk.lo, hblk.hi
        header_cond = instrs[hhi]
        if type(header_cond).__name__ != 'IRCondJump' or header_cond.ftype is not None:
            return False
        if header_cond.op not in ('<', '<=') or not isinstance(header_cond.left, Temp):
            return False

        pay_blocks = [b for b in desc.body_blocks if b != header]
        ranges = sorted((cfg.blocks[b].lo, cfg.blocks[b].hi) for b in pay_blocks)
        p_lo, p_hi = ranges[0][0], ranges[-1][1]
        if p_lo != hhi + 1:
            return False
        if sum(hi - lo + 1 for lo, hi in ranges) != (p_hi - p_lo + 1):
            return False

        body_label = header_cond.true_label
        first_pay = cfg.blocks[min(pay_blocks, key=lambda b: cfg.blocks[b].lo)]
        if body_label is None or first_pay.label != body_label:
            return False
        exit_label = header_cond.false_label
        if exit_label is None or exit_label == body_label:
            return False
        latch_term = instrs[cfg.blocks[latch].hi]
        if type(latch_term).__name__ != 'IRJump' or latch_term.label != hlbl:
            return False

        iv_slot = desc.primary_iv
        if iv_slot is None or iv_slot not in desc.basic_ivs:
            return False
        biv = desc.basic_ivs[iv_slot]
        step = biv.step
        if step <= 0:
            return False
        if not L.has_side_effect_free_header(desc).ok:
            return False

        # the value the FIRST copy already computes for the IV (== iv + step): the
        # source of the IV-update store. Reused instead of a reload (improvement 1).
        iv_store = instrs[biv.update_site]
        if type(iv_store).__name__ != 'IRStore' or not isinstance(iv_store.src, Temp):
            return False
        iv_next_name = iv_store.src.name

        # ---- bound: constant OR loop-invariant symbolic (improvement 3) ------
        right = header_cond.right
        bound_is_const = isinstance(right, Const)
        boundsub_instr = None
        if bound_is_const:
            main_bound_operand = Const(right.value - step)
            rem_bound_operand = Const(right.value)
        elif isinstance(right, Temp):
            # symbolic: the bound must be loop-invariant (M7 fact) and available in
            # the preheader; compute (bound - step) there, once.
            if desc.preheader is None:
                return False
            if not L.guard_inputs_loop_independent(desc).ok:
                return False
            if not self._bound_available_in_preheader(instrs, desc, right.name):
                return False
            boundsub_name = self._fresh_temp()
            boundsub_instr = IRBinOp(Temp(boundsub_name), '-', Temp(right.name), Const(step))
            main_bound_operand = Temp(boundsub_name)
            rem_bound_operand = Temp(right.name)
        else:
            return False

        # ---- profitability: reuse R1.1 model EXACTLY -------------------------
        rep = self.analyze(desc)
        if not (rep.eligible and rep.profit.should_unroll):
            return False

        # dead-remainder elimination for a known, evenly-divisible trip (impr. 2)
        emit_remainder = True
        if (bound_is_const and rep.profit.trip_kind == 'known'
                and rep.profit.trip_value is not None
                and rep.profit.trip_value % _FACTOR == 0):
            emit_remainder = False

        # ---- build ----------------------------------------------------------
        setup_src = list(instrs[hlo + 1:hhi])
        payload_src = list(instrs[p_lo:p_hi + 1])
        region_src = setup_src + payload_src
        n_setup = len(setup_src)

        # SECOND copy: clone, then substitute the IV (drops reload dependency).
        c2, _t2, l2 = self._clone_region(region_src)
        c2 = self._substitute_iv(c2, iv_slot, iv_next_name)   # its back-edge -> header
        entry2 = self._fresh_label()
        copy2_blocks = [IRLabel(entry2)] + c2

        appended = list(copy2_blocks)
        synth = {entry2, hlbl}

        if emit_remainder:
            cr, tr, lr = self._clone_region(region_src)
            rem_setup = cr[:n_setup]
            rem_payload = cr[n_setup:]
            rem_ph = self._fresh_label()
            rem_head = self._fresh_label()
            rem_body_first = lr[body_label]
            self._retarget_backedge(rem_payload, hlbl, rem_head)
            rem_iv = tr[header_cond.left.name] if header_cond.left.name in tr else None
            # the remainder guard reloads the IV normally (no substitution needed);
            # its IV temp is the cloned header-setup load result.
            if rem_iv is None:
                return False
            rem_cond = IRCondJump(Temp(rem_iv), header_cond.op, rem_bound_operand,
                                  rem_body_first, exit_label)
            rem_blocks = ([IRLabel(rem_ph), IRJump(rem_head), IRLabel(rem_head)]
                          + rem_setup + [rem_cond] + rem_payload)
            appended += rem_blocks
            synth |= {rem_ph, rem_head, rem_body_first}

        # (a) preheader arithmetic for a symbolic bound (once).
        if boundsub_instr is not None:
            at = self._preheader_insert_index(instrs, desc)
            txn.splice(at, [boundsub_instr])

        # (b) append the second copy (+ remainder) at the slice end.
        txn.splice(txn.slice_end(), appended)
        # (c) reroute the first copy's back-edge into the second copy.
        txn.retarget(latch_term, hlbl, entry2)
        # (d) tighten the main guard; route its exit to the remainder (if any).
        txn.set_field(header_cond, 'right', main_bound_operand)
        if emit_remainder:
            txn.retarget(header_cond, exit_label, rem_ph)

        self._synthetic.update(synth)
        return True

    # -- helpers for symbolic bounds -------------------------------------------
    @staticmethod
    def _bound_available_in_preheader(instrs, desc, bound_name):
        """True iff the bound temp is defined at or before the preheader's last
        instruction (so it is available when we compute bound-step there)."""
        ph_hi = desc.cfg.blocks[desc.preheader].hi
        lo, _hi = desc.func_slice
        for k in range(lo, ph_hi + 1):
            ins = instrs[k]
            if type(ins).__name__ == 'IRLoadWide':
                if any(d.name == bound_name for d in ins.dests):
                    return True
            d = getattr(ins, 'dest', None)
            if isinstance(d, Temp) and d.name == bound_name:
                return True
        return False

    @staticmethod
    def _preheader_insert_index(instrs, desc):
        """Index at which to insert preheader arithmetic: before the preheader's
        terminator if it ends in a branch, else just before the header label."""
        phblk = desc.cfg.blocks[desc.preheader]
        if type(instrs[phblk.hi]).__name__ in ('IRJump', 'IRCondJump'):
            return phblk.hi
        return desc.cfg.blocks[desc.header].lo


# ── module driver + report ────────────────────────────────────────────────────

class UnrollR13Report:
    __slots__ = ('loops_visited', 'loops_unrolled', 'loops_skipped',
                 'verifier_failures', 'rollbacks')

    def __init__(self, stats):
        self.loops_visited = stats.attempts
        self.loops_unrolled = stats.commits
        self.loops_skipped = stats.skipped_illegal + stats.skipped_noop
        self.verifier_failures = stats.verifier_failures
        self.rollbacks = stats.rollbacks

    def report(self):
        return "\n".join([
            "LoopUnrollFactor2R13 report:",
            f"  loops visited      : {self.loops_visited}",
            f"  loops unrolled     : {self.loops_unrolled}",
            f"  loops skipped      : {self.loops_skipped}",
            f"  verifier failures  : {self.verifier_failures}",
            f"  rollbacks          : {self.rollbacks}",
        ])


def unroll_module(instrs, verifier=None, stats=None):
    """Factor-2 unroll (R1.3 quality) every profitable loop in `instrs` in place,
    through the M5 framework. Returns (TransformStats, UnrollR13Report)."""
    drv = LoopTransformDriver(verifier=verifier)
    stats = drv.run(LoopUnrollFactor2R13(), instrs, stats)
    return stats, UnrollR13Report(stats)

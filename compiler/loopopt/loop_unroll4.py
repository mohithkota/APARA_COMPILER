"""
loop_unroll4.py -- LoopUnrollFactorN (Research milestone R1.4).

R1.4 GENERALISES the R1.3 factor-2 unroller to factors 2 / 4 / 8, driven by the
existing profitability model's `recommended_factor`, and WIDENS symbolic-bound
coverage to loops whose bound is defined in the header. All R1.3 quality wins
(IV substitution, dead-remainder elimination, cleanup) stay active at every
factor. Nothing in the optimisation model changes.

Everything through R1.3 is frozen. LoopUnrollFactorN SUBCLASSES
`LoopUnrollFactor2R13`, inherits its helpers, and overrides only `run()`. R1.1,
R1.2 and R1.3 remain available and comparable.

New capabilities
----------------
* Higher factors. For factor F the loop runs F copies per main iteration; the
  main guard becomes `iv < C - (F-1)*step` (all F iterations in range) and a
  single remainder loop drains the `T mod F` leftovers. The factor is the model's
  `recommended_factor`, clamped to the supported set {2,4,8} and never above a
  known trip count (a support/sanity bound, not a new heuristic).

* Chained IV substitution. Copy k reuses the value copy k-1 already computed
  (a register), not a slot reload. IV loads are FULLY substituted -- loads before
  a copy's IV store take the entry value, loads after it (e.g. `s += i` post
  `i++`) take that copy's own increment -- so copies 2..F-1 touch the IV slot not
  at all and their (dead) IV stores are dropped, leaving no store-store chain.

* Wider symbolic bounds. A loop-invariant bound defined in the HEADER (not just
  one available in the preheader) is supported: `bound - (F-1)*step` is computed
  where the bound is available (preheader when possible, else the header) and the
  main loop is guarded against it; the remainder keeps the original bound.

* Dead-remainder elimination generalised: a known trip with `T % F == 0` omits
  the remainder loop.

Correctness is preserved exactly (differential execution proves it); the frozen
M5 framework owns rebuild / verify / rollback. Not wired into the pipeline; no
unroll-and-jam, no software pipelining, no modulo scheduling.
"""

from collections import Counter

from ir import IRLabel, IRJump, IRCondJump, IRBinOp, Const, Temp
from ir_utils import src_names
from .descriptor import TOP_TESTED
from .transform import LoopTransformDriver
from .loop_unroll3 import LoopUnrollFactor2R13
from .loop_unroll2 import _walk_temps
from . import legality as L

_SUPPORTED_FACTORS = (8, 4, 2)


class LoopUnrollFactorN(LoopUnrollFactor2R13):
    """Factor 2/4/8 unrolling with chained IV substitution, dead-remainder
    elimination and header/preheader symbolic bounds. Same framework, same
    legality, same profitability model."""

    name = 'loop-unroll-fN'

    def __init__(self):
        super().__init__()
        # test/override knob: force a specific factor (2/4/8) instead of the
        # model's recommendation. Default None -> use the profitability model.
        self.force_factor = None
        # factors actually applied (by run() returning True). Rollbacks (≈0 here)
        # would over-count; the driver reports 0 rollbacks so this is exact.
        self.factors_applied = Counter()

    # -- factor selection: clamp the model's recommendation to {2,4,8} ---------
    def _choose_factor(self, rep):
        if self.force_factor in (2, 4, 8):
            f = self.force_factor
        else:
            rec = rep.profit.recommended_factor
            f = next((c for c in _SUPPORTED_FACTORS if c <= rec), 2)
        if rep.profit.trip_kind == 'known' and rep.profit.trip_value is not None:
            t = rep.profit.trip_value
            if t < 2:
                return None
            while f > 2 and f > t:                 # never unroll past the trip
                f //= 2
        return f

    # -- full IV substitution + store/address cleanup on one cloned copy --------
    def _subst_copy(self, blocks, iv_slot, entry_val_name, biv_src_name, tmap, keep_store):
        """Rewrite a cloned copy so it makes NO IV-slot loads: loads before the
        copy's IV store take `entry_val_name`, loads after it take this copy's own
        increment (== tmap[biv_src_name]). If keep_store is False the (dead) IV
        store and its address are dropped. Returns (kept_blocks, this_copy_nv)."""
        addr_temps = {ins.dest.name for ins in blocks
                      if type(ins).__name__ == 'IRLoadAddr' and ins.fp_offset == iv_slot}
        store_idx, store_obj = None, None
        for i, ins in enumerate(blocks):
            if (type(ins).__name__ == 'IRStore' and isinstance(ins.base, Temp)
                    and ins.base.name in addr_temps
                    and isinstance(ins.offset, Const) and ins.offset.value == 0):
                store_idx, store_obj = i, ins
                break
        if store_idx is None:
            return None, None
        nv_name = tmap.get(biv_src_name)
        if nv_name is None:
            return None, None
        remove, remap = set(), {}
        for i, ins in enumerate(blocks):
            if (type(ins).__name__ == 'IRLoad' and isinstance(ins.base, Temp)
                    and ins.base.name in addr_temps
                    and isinstance(ins.offset, Const) and ins.offset.value == 0):
                remove.add(id(ins))
                remap[ins.dest.name] = entry_val_name if i < store_idx else nv_name
        if not keep_store:
            remove.add(id(store_obj))
        still = Counter()
        for ins in blocks:
            if id(ins) in remove:
                continue
            for s in src_names(ins):
                if s in addr_temps:
                    still[s] += 1
        kept = []
        for ins in blocks:
            if id(ins) in remove:
                continue
            if (type(ins).__name__ == 'IRLoadAddr' and ins.fp_offset == iv_slot
                    and still[ins.dest.name] == 0):
                continue
            kept.append(ins)
        for ins in kept:
            for t in _walk_temps(ins):
                if t.name in remap:
                    t.name = remap[t.name]
        return kept, nv_name

    @staticmethod
    def _bound_defined_in_header(instrs, desc, bname):
        hblk = desc.cfg.blocks[desc.header]
        for k in range(hblk.lo, hblk.hi + 1):
            d = getattr(instrs[k], 'dest', None)
            if isinstance(d, Temp) and d.name == bname:
                return True
        return False

    @staticmethod
    def _bound_invariant_slot(instrs, desc, bname):
        """Prove the symbolic bound's VALUE is loop-invariant: it must be a load of
        a CLEAN stack slot (its address never escapes) that the loop never writes.
        Returns the slot offset, or None if invariance cannot be proven.

        This is essential -- `guard_inputs_loop_independent` only proves the guard's
        temp is not body-defined (the header reloads it each iteration), NOT that
        its value is stable. A loop like `while(lo<hi){...;hi--;}` reloads `hi`
        every iteration but its value changes; treating it as an invariant bound
        would be unsound. Requiring a clean, unwritten source slot rules that out."""
        lo, hi = desc.func_slice
        defidx = None
        for k in range(lo, hi + 1):
            d = getattr(instrs[k], 'dest', None)
            if isinstance(d, Temp) and d.name == bname:
                defidx = k
        if defidx is None:
            return None
        ld = instrs[defidx]
        if (type(ld).__name__ != 'IRLoad' or not isinstance(ld.base, Temp)
                or not (isinstance(ld.offset, Const) and ld.offset.value == 0)):
            return None
        base = ld.base.name
        slot = None
        addr_names = set()
        for k in range(lo, hi + 1):
            j = instrs[k]
            if type(j).__name__ == 'IRLoadAddr':
                if j.dest.name == base:
                    slot = j.fp_offset
        if slot is None:
            return None
        for k in range(lo, hi + 1):
            j = instrs[k]
            if type(j).__name__ == 'IRLoadAddr' and j.fp_offset == slot:
                addr_names.add(j.dest.name)
        # cleanness: every use of a slot-address temp is a zero-offset load/store base
        for k in range(lo, hi + 1):
            j = instrs[k]
            c = type(j).__name__
            allowed = (j.base.name if c in ('IRLoad', 'IRStore')
                       and isinstance(getattr(j, 'base', None), Temp)
                       and isinstance(j.offset, Const) and j.offset.value == 0 else None)
            for s in src_names(j):
                if s in addr_names and s != allowed:
                    return None
        # the loop must not write the slot
        for b in desc.body_blocks:
            blk = desc.cfg.blocks[b]
            for k in range(blk.lo, blk.hi + 1):
                j = instrs[k]
                if (type(j).__name__ == 'IRStore' and isinstance(j.base, Temp)
                        and j.base.name in addr_names
                        and isinstance(j.offset, Const) and j.offset.value == 0):
                    return None
        return slot

    # -- the generalised transform ---------------------------------------------
    def run(self, instrs, lo, desc, txn):
        cfg = desc.cfg
        header = desc.header
        hblk = cfg.blocks[header]
        hlbl = hblk.label
        if hlbl in self._synthetic:
            return False

        # ---- structural preconditions (identical shape to R1.2/R1.3) ---------
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
        if p_lo != hhi + 1 or sum(hi - lo + 1 for lo, hi in ranges) != (p_hi - p_lo + 1):
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
        iv_store = instrs[biv.update_site]
        if type(iv_store).__name__ != 'IRStore' or not isinstance(iv_store.src, Temp):
            return False
        biv_src_name = iv_store.src.name

        # ---- profitability + factor ------------------------------------------
        rep = self.analyze(desc)
        if not (rep.eligible and rep.profit.should_unroll):
            return False
        F = self._choose_factor(rep)
        if F is None or F < 2:
            return False

        # ---- bound handling: const OR loop-invariant symbolic ----------------
        right = header_cond.right
        delta = (F - 1) * step
        boundsub_instr = None
        boundsub_at_header = False
        if isinstance(right, Const):
            symbolic = False
            main_bound_operand = Const(right.value - delta)
        elif isinstance(right, Temp):
            symbolic = True
            if not L.guard_inputs_loop_independent(desc).ok:
                return False
            # the bound's VALUE must be provably loop-invariant, not just its temp
            if self._bound_invariant_slot(instrs, desc, right.name) is None:
                return False
            boundsub_name = self._fresh_temp()
            boundsub_instr = IRBinOp(Temp(boundsub_name), '-', Temp(right.name), Const(delta))
            main_bound_operand = Temp(boundsub_name)
            if desc.preheader is not None and self._bound_available_in_preheader(
                    instrs, desc, right.name):
                boundsub_at_header = False          # preheader (reuse R1.3 mechanism)
            elif self._bound_defined_in_header(instrs, desc, right.name):
                boundsub_at_header = True            # header (bound only available here)
            else:
                return False
        else:
            return False

        # dead-remainder for a known, evenly divisible trip
        emit_remainder = not (not symbolic and rep.profit.trip_kind == 'known'
                              and rep.profit.trip_value is not None
                              and rep.profit.trip_value % F == 0)

        # ---- build the copies ------------------------------------------------
        setup_src = list(instrs[hlo + 1:hhi])
        payload_src = list(instrs[p_lo:p_hi + 1])
        region_src = setup_src + payload_src
        n_setup = len(setup_src)

        entries = {k: self._fresh_label() for k in range(2, F + 1)}
        appended = []
        synth = {hlbl}
        iv_src_prev = biv_src_name                  # copy 1's increment result
        for k in range(2, F + 1):
            ck, tmap_k, lmap_k = self._clone_region(region_src)
            ck, nv_k = self._subst_copy(ck, iv_slot, iv_src_prev, biv_src_name,
                                        tmap_k, keep_store=(k == F))
            if ck is None:
                return False
            if k < F:
                self._retarget_backedge(ck, hlbl, entries[k + 1])   # chain forward
            appended += [IRLabel(entries[k])] + ck
            synth.add(entries[k])
            iv_src_prev = nv_k

        # ---- remainder loop (unless proven dead) -----------------------------
        if emit_remainder:
            cr, tr, lr = self._clone_region(region_src)
            rem_setup = cr[:n_setup]
            rem_payload = cr[n_setup:]
            rem_ph = self._fresh_label()
            rem_head = self._fresh_label()
            rem_body_first = lr[body_label]
            self._retarget_backedge(rem_payload, hlbl, rem_head)
            rem_iv = tr.get(header_cond.left.name)
            if rem_iv is None:
                return False
            rem_bound = (Const(right.value) if not symbolic else Temp(tr[right.name])
                         if right.name in tr else None)
            if rem_bound is None:
                return False
            rem_cond = IRCondJump(Temp(rem_iv), header_cond.op, rem_bound,
                                  rem_body_first, exit_label)
            appended += ([IRLabel(rem_ph), IRJump(rem_head), IRLabel(rem_head)]
                         + rem_setup + [rem_cond] + rem_payload)
            synth |= {rem_ph, rem_head, rem_body_first}

        # ---- splice + rewire -------------------------------------------------
        if boundsub_instr is not None:
            if boundsub_at_header:
                txn.splice(hhi, [boundsub_instr])   # in the header, before the guard
            else:
                txn.splice(self._preheader_insert_index(instrs, desc), [boundsub_instr])
        txn.splice(txn.slice_end(), appended)
        txn.retarget(latch_term, hlbl, entries[2])
        txn.set_field(header_cond, 'right', main_bound_operand)
        if emit_remainder:
            txn.retarget(header_cond, exit_label, rem_ph)

        self._synthetic.update(synth)
        self.factors_applied[F] += 1
        return True


# ── module driver + report ────────────────────────────────────────────────────

class UnrollNReport:
    __slots__ = ('loops_visited', 'loops_unrolled', 'loops_skipped',
                 'verifier_failures', 'rollbacks', 'factors')

    def __init__(self, stats, factors=None):
        self.loops_visited = stats.attempts
        self.loops_unrolled = stats.commits
        self.loops_skipped = stats.skipped_illegal + stats.skipped_noop
        self.verifier_failures = stats.verifier_failures
        self.rollbacks = stats.rollbacks
        self.factors = dict(factors or {})       # {factor: count}

    def report(self):
        return "\n".join([
            "LoopUnrollFactorN report:",
            f"  loops visited      : {self.loops_visited}",
            f"  loops unrolled     : {self.loops_unrolled}",
            f"  loops skipped      : {self.loops_skipped}",
            f"  verifier failures  : {self.verifier_failures}",
            f"  rollbacks          : {self.rollbacks}",
        ])


def unroll_module(instrs, verifier=None, stats=None, force_factor=None):
    """Factor 2/4/8 unroll (R1.4) every profitable loop in `instrs` in place,
    through the M5 framework. `force_factor` (2/4/8) overrides the model's choice
    (testing only). Returns (TransformStats, UnrollNReport)."""
    drv = LoopTransformDriver(verifier=verifier)
    xf = LoopUnrollFactorN()
    xf.force_factor = force_factor
    stats = drv.run(xf, instrs, stats)
    return stats, UnrollNReport(stats, xf.factors_applied)

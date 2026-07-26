"""
pipeline_mve.py -- Modulo Variable Expansion + Rotating Kernel Realisation
(Milestone R2.8).

The final engineering step of the software-pipelining framework. R2.5 (modulo
scheduling) and R2.7 (register-aware recognition) already produce a *correct
modulo schedule*; R2.5/R2.7 REALISE that schedule by fully UNROLLING it over all
T iterations (prologue -> iteration0 -> ... -> iterationN -> epilogue). That is
correct but grows the static code with T. R2.8 replaces ONLY the realisation
strategy: it emits a COMPACT

    prologue  ->  kernel LOOP  ->  epilogue

whose static size is O(stages), independent of the trip count. The scheduler, the
reservation table, the dependence graph, the recurrence abstraction and every
gate are consumed UNCHANGED -- nothing about scheduling / promotion / the
dependence graph / the bundler / register allocation / LoopInfo is redesigned.

--------------------------------------------------------------------------------
MODULO VARIABLE EXPANSION (the idea)

Take the same linearised schedule R2.5 emits: instance (iteration `it`, kernel op
`o`) runs at absolute time  it*II + cycle(o).  Its *window* (which II-slice it
lands in) is  W(it,o) = it + stage(o),  where stage(o) = cycle(o)//II in [0,S-1].

In steady state, window `w` issues, for every stage s, the stage-s op of iteration
(w - s) -- i.e. S different iterations are in flight at once. If we roll those
windows into a single loop, a value that a op defines in stage s_A of iteration it
and another op consumes in stage s_B > s_A of the SAME iteration lives across
(s_B - s_A) windows, i.e. across kernel-loop back-edges; a single physical
register holding it would be overwritten by the NEXT iteration entering the same
stage before the consumer reads it.

The classic fix is a small bank of ROTATING registers. We implement it purely in
compiler IR (no hardware rotating registers): every per-iteration temp is renamed
into bank  b = it mod U,  with U = S (the stage count). Because the maximum live
span of any value is <= S-1 < S = U, two iterations that share a bank (U apart)
never have overlapping live ranges -- so the rename is conflict-free. Within any
one window the S in-flight iterations occupy S *distinct* banks (w, w-1, ..,
w-S+1 mod S are all different), which is exactly a rotating-register file with a
fixed footprint. Loop-carried recurrence registers (the accumulator / IV promoted
by R2.6) are the ONE thing kept SHARED across all banks -- identical to how R2.5
keeps a memory slot shared -- so the recurrence still threads correctly.

--------------------------------------------------------------------------------
KERNEL GENERATION (known trip count T, stages S, II)

  * U = S  rotating banks; bank(instance) = iteration mod U  (uniform everywhere).
  * prologue   = windows [0 .. S-2]            (ramp-up; seeds every rotating reg)
  * kernel loop = windows [S-1 .. S-2+U] emitted ONCE, run K = (T-S+1)//U times
                 (each pass advances the shared recurrences by U iterations)
  * remainder  = windows [S-1+K*U .. T-1]      (steady windows the loop didn't cover)
  * epilogue   = windows [T .. T+S-2]           (drain)

The loop is a do-while over a fresh counter initialised to K in the (new) loop
preheader -- a register recurrence codegen already keeps live across a back-edge
via its documented loop-live-in live-range extension. Every rotating register the
loop body reads before it writes is SEEDED in the prologue (checked structurally),
so codegen's extension keeps it live across the back-edge -- this is the codegen-
correctness invariant of the construction.

Requires T >= 2S-1 so at least one full loop period exists; smaller trips (and any
loop where the codegen invariant does not hold) fall back to R2.7's full-unroll
realiser, so R2.8 NEVER pipelines fewer loops than R2.7.

--------------------------------------------------------------------------------
CORRECTNESS is mandatory and unchanged in spirit: a compact kernel is committed
only when it passes the SAME structural check + the clean-slot-respecting multi-
seed differential (over the whole prologue/kernel/epilogue) + the compile gate,
AND the rotating-register-seeding invariant. Anything else rolls back to the full-
unroll form (still correct) or leaves the loop untouched. Standalone -- not wired
into the production compiler.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import Temp, Const                                          # noqa: E402
from ir_utils import func_slices, dest_names, src_names             # noqa: E402
from .discovery import discover_function                            # noqa: E402
from .analysis_iv import annotate_induction_vars, TripCount          # noqa: E402
from .analysis_mem import annotate_memory_effects                    # noqa: E402
from .depgraph import DependenceGraph                                # noqa: E402
from .depgraph_disambig import MemoryDisambiguator                   # noqa: E402
from .modulo import (build_kernel, min_ii, modulo_schedule,          # noqa: E402
                     generate_pipeline, _clone_op, _compiles,
                     _MAX_PIPELINE_OPS)
from .loop_promote import (promote_function, _promote_diff, _rp_n)    # noqa: E402
from .pipeline_regaware import (_carried_register_values,            # noqa: E402
                                _normalize_register_loop,
                                _find_loop_by_header_label,
                                realize_register_pipeline)


# deterministic fresh-name counter for kernel counters / labels
_mve_n = [0]


def _fresh(prefix):
    _mve_n[0] += 1
    return f"{prefix}{_mve_n[0]}"


# ── window emission (the modulo-variable-expansion rename) ─────────────────────

def _window_ops(kernel, sched, T, w):
    """The (iteration, op) instances that land in window `w`, in issue order
    (modulo-slot, then iteration, then op) -- exactly R2.5's intra-window order."""
    ii = sched.ii
    cyc = sched.cycle
    out = []
    for op in kernel.ops:
        st = cyc[op] // ii
        it = w - st
        if 0 <= it <= T - 1:
            out.append((cyc[op] % ii, it, op))
    out.sort()                                   # (modslot, it, op)
    return [(it, op) for (_m, it, op) in out]


def _emit_window(graph, kernel, sched, T, w, U, cache):
    """Clone every instance of window `w`, renaming each per-iteration temp into
    bank (iteration mod U). Shared recurrence registers / memory slots are left
    shared via the pre-seeded `cache`."""
    body = []
    for (it, op) in _window_ops(kernel, sched, T, w):
        body.append(_clone_op(graph.instrs[op], it % U, cache))
    return body


def _seed_cache(recur_names, U):
    """A rename cache pre-seeded so every loop-carried recurrence register maps to
    itself in ALL U banks (shared), while all other temps expand per bank."""
    cache = {}
    for name in recur_names:
        shared = Temp(name)
        for b in range(U):
            cache[(name, b)] = shared
    return cache


# ── codegen-correctness invariant: rotating regs seeded before the loop ────────

def _read_before_write(slice_instrs, head_local, backedge_local):
    """Temps the kernel body [head_local..backedge_local] READS BEFORE it WRITES --
    the loop-carried / rotating registers whose value crosses the back-edge."""
    seen_def = set()
    carried = set()
    for i in range(head_local, backedge_local + 1):
        ins = slice_instrs[i]
        for s in src_names(ins):
            if s not in seen_def:
                carried.add(s)
        seen_def.update(dest_names(ins))
    return carried


def _codegen_keeps_alive(slice_instrs, head_local, backedge_local):
    """Certify -- using CODEGEN'S OWN liveness computation -- that every loop-
    carried rotating register of the kernel body is kept live across the back-edge.
    Codegen's `_compute_last_uses` extends a loop-live-in value's last use to the
    back-edge index iff it is defined before the header; if it does so for every
    read-before-write body temp, the physical register survives the loop and the
    machine code is correct. This grounds the codegen-correctness claim in the real
    allocator's code, not a re-derivation. Returns (ok, offending_name_or_None)."""
    from codegen import CodeGen
    carried = _read_before_write(slice_instrs, head_local, backedge_local)
    if not carried:
        return True, None
    last_use = CodeGen()._compute_last_uses(slice_instrs)
    for name in carried:
        # kept alive iff its last use reaches (at least) the back-edge instruction
        if last_use.get(name, -1) < backedge_local:
            return False, name
    return True, None


# ── report / stats ─────────────────────────────────────────────────────────────

class MVEReport:
    __slots__ = ('func', 'header', 'form', 'committed', 'compacted', 'reason',
                 'ii', 'stages', 'trip', 'bank_size', 'n_rotating',
                 'prologue', 'kernel_body', 'epilogue', 'loop_trips',
                 'static_full', 'static_compact')

    def __init__(self, func, header):
        self.func = func
        self.header = header
        self.form = None                # 'memory' | 'register'
        self.committed = False
        self.compacted = False          # True iff realised as a compact kernel loop
        self.reason = 'not-eligible'
        self.ii = self.stages = self.trip = 0
        self.bank_size = self.n_rotating = 0
        self.prologue = self.kernel_body = self.epilogue = self.loop_trips = 0
        self.static_full = self.static_compact = 0

    def __repr__(self):
        if not self.committed:
            return f"MVE({self.func}@B{self.header} {self.reason})"
        kind = 'compact' if self.compacted else 'full-unroll'
        return (f"MVE({self.func}@B{self.header} {self.form}/{kind} II={self.ii} "
                f"S={self.stages} U={self.bank_size} rot={self.n_rotating} "
                f"static {self.static_full}->{self.static_compact})")


class MVEStats:
    def __init__(self):
        self.functions = self.loops = 0
        self.kernel_loops = 0           # compact kernels generated
        self.full_unroll = 0            # loops pipelined but not compacted (fallback)
        self.declined = self.rolled_back = 0
        self.mve_mappings = 0           # total rotating-register mappings created
        self.sum_bank = self.sum_rot = 0
        self.sum_ii = self.sum_stages = 0
        self.static_full = self.static_compact = 0
        self.reasons = {}

    def _bump(self, k):
        self.reasons[k] = self.reasons.get(k, 0) + 1


# ── the compact realiser ───────────────────────────────────────────────────────

def realize_mve_kernel(instrs, lo, hi, desc, graph, kernel, sched, recur_names):
    """Realise the modulo schedule as a COMPACT prologue/kernel-loop/epilogue with
    modulo variable expansion. Returns (new_instrs, MVEReport). On any decline the
    report is not committed and the caller falls back to the full-unroll realiser
    (so coverage never regresses)."""
    fname = getattr(instrs[lo], 'name', '?')
    rep = MVEReport(fname, desc.header)
    rep.ii, rep.stages = sched.ii, sched.stages

    if desc.trip_count.kind != TripCount.KNOWN:
        rep.reason = 'trip-not-known'
        return None, rep
    T = desc.trip_count.value
    S = sched.stages
    II = sched.ii
    U = S                                         # rotating-bank count
    rep.trip = T
    rep.bank_size = U
    rep.static_full = T * len(kernel.ops)

    if S < 2:
        rep.reason = 'single-stage-not-profitable'
        return None, rep
    if T < 2 * S - 1:                             # need >=1 full loop period
        rep.reason = 'trip-too-small-for-kernel'
        return None, rep
    K = (T - S + 1) // U                          # full loop periods
    if K < 1:
        rep.reason = 'no-full-period'
        return None, rep
    if rep.static_full > _MAX_PIPELINE_OPS:
        rep.reason = 'code-size-guard'
        return None, rep

    # region to replace: [header block start .. latch block end] (as R2.5)
    hblk = graph.cfg.blocks[desc.header]
    lblk = graph.cfg.blocks[desc.latches[0]]
    region_lo, region_hi = hblk.lo, lblk.hi

    cache = _seed_cache(recur_names, U)

    # prologue: windows [0 .. S-2]
    prologue = []
    for w in range(0, S - 1):
        prologue.extend(_emit_window(graph, kernel, sched, T, w, U, cache))

    # kernel loop body: windows [S-1 .. S-2+U]  (U consecutive full windows)
    kbody = []
    for w in range(S - 1, S - 1 + U):
        kbody.extend(_emit_window(graph, kernel, sched, T, w, U, cache))

    # remainder: full steady windows the K periods did not cover
    remainder = []
    for w in range(S - 1 + K * U, T):
        remainder.extend(_emit_window(graph, kernel, sched, T, w, U, cache))

    # epilogue: windows [T .. T+S-2]  (drain)
    epilogue = []
    for w in range(T, T + S - 1):
        epilogue.extend(_emit_window(graph, kernel, sched, T, w, U, cache))

    # kernel loop scaffold: do-while over a fresh register counter (loop-live-in)
    cnt = Temp(_fresh('_mvk'))
    head = _fresh('mve_kernel_')
    from ir import IRAssign, IRBinOp, IRCondJump, IRLabel
    preheader = [IRAssign(cnt, Const(K))]
    loop_open = [IRLabel(head)]
    loop_close = [IRBinOp(cnt, '-', cnt, Const(1)),
                  IRCondJump(cnt, '>', Const(0), head, None)]

    new_region = (prologue + preheader + loop_open + kbody + loop_close
                  + remainder + epilogue)
    rep.static_compact = len(new_region)
    # only worthwhile if the compact form is actually SMALLER than the full unroll;
    # otherwise decline so the caller falls back to the (proven) full-unroll realiser
    if rep.static_compact >= rep.static_full:
        rep.reason = 'no-size-reduction'
        return None, rep
    new = instrs[:region_lo] + new_region + instrs[region_hi + 1:]

    # locate the rebuilt slice + the kernel loop within it (for the invariant)
    base = region_lo - lo                          # offset of region within slice
    head_idx = None
    for i in range(region_lo, region_lo + len(new_region)):
        ni = new[i]
        if type(ni).__name__ == 'IRLabel' and ni.name == head:
            head_idx = i
            break
    backedge_idx = None
    for i in range(head_idx, region_lo + len(new_region)):
        ni = new[i]
        if type(ni).__name__ == 'IRCondJump' and ni.true_label == head:
            backedge_idx = i
            break

    # structural: module still parses into the same functions
    if len(func_slices(new)) != len(func_slices(instrs)):
        rep.reason = 'structural-slice-mismatch'
        return None, rep

    # codegen invariant: rotating regs read-before-write in the body must be kept
    # alive across the back-edge by codegen's OWN live-range extension
    slo = next(a for a, b in func_slices(new) if new[a].name == fname)
    new_slice = _slice_of(new, fname)
    ok, bad = _codegen_keeps_alive(new_slice, head_idx - slo, backedge_idx - slo)
    if not ok:
        rep.reason = f'unseeded-rotating-reg:{bad}'
        return None, rep

    # clean-slot-respecting multi-seed differential (over the whole realisation)
    if _promote_diff(instrs, new, lo, hi) == 'mismatch':
        rep.reason = 'differential-rollback'
        return None, rep
    if not _compiles(new):
        rep.reason = 'compile-rollback'
        return None, rep

    # rotating registers = the loop-carried (read-before-write) temps of the body;
    # these occupy the rotating bank and are seeded by the prologue
    rep.n_rotating = len(_read_before_write(new_slice, head_idx - slo,
                                            backedge_idx - slo))
    rep.prologue = len(prologue)
    rep.kernel_body = len(kbody)
    rep.epilogue = len(epilogue) + len(remainder)
    rep.loop_trips = K
    rep.static_compact = len(new_region)
    rep.committed = True
    rep.compacted = True
    rep.reason = 'ok'
    return new, rep


# ── recognition / driver (mirrors R2.7; compact realiser preferred) ────────────

def _schedule_register_form(orig_sub, T, header_label, rep):
    """Promote (R2.6) then schedule the register recurrence via R2.5. Returns
    (promoted_slice, desc, graph, kernel, sched, recur_names, rec) or None."""
    promoted, preps = promote_function(orig_sub, 0, len(orig_sub) - 1)
    if not any(r.committed for r in preps):
        return None
    _descs, graph2, d2 = _find_loop_by_header_label(promoted, 0, len(promoted) - 1,
                                                    header_label)
    if d2 is None:
        return None
    _normalize_register_loop(d2, T)
    kernel2, why = build_kernel(d2, graph2)
    if kernel2 is None:
        rep.reason = f'kernel:{why}'
        return None
    mii, rec, res = min_ii(kernel2)
    sched, _sw = modulo_schedule(kernel2, mii)
    if sched is None:
        rep.reason = 'no-schedule'
        return None
    recur = _carried_register_values(kernel2, graph2)
    return promoted, d2, graph2, kernel2, sched, recur, rec


def _schedule_memory_form(orig_sub, desc0, graph0, rep):
    """Schedule the memory recurrence directly (Case A) via R2.5. Returns
    (desc, graph, kernel, sched, recur_names, rec) or None."""
    kernel, why = build_kernel(desc0, graph0)
    if kernel is None:
        rep.reason = f'kernel:{why}'
        return None
    mii, rec, res = min_ii(kernel)
    sched, _sw = modulo_schedule(kernel, mii)
    if sched is None:
        rep.reason = 'no-schedule'
        return None
    return desc0, graph0, kernel, sched, set(), rec


def _realize_form(sub, plan, form, rep):
    """Realise one scheduled loop FORM (register or memory): try the COMPACT kernel
    first, fall back to R2.7's proven FULL-UNROLL realiser. Both are re-validated
    end-to-end against the ORIGINAL `sub` with the clean-slot differential. Returns
    (new_slice, compacted_bool, mve_report_or_None) or None if the form declines
    entirely. `plan` = (sched_slice, desc, graph, kernel, sched, recur, rec)."""
    sched_sub, dd, gg, kernel, sched, recur, rec = plan

    # 1) COMPACT kernel realisation
    new_c, mrep = realize_mve_kernel(sched_sub, 0, len(sched_sub) - 1,
                                     dd, gg, kernel, sched, recur)
    if mrep.committed and _promote_diff(sub, new_c, 0, len(sub) - 1) != 'mismatch':
        mrep.form = form
        return _slice_of(new_c, sub[0].name), True, mrep
    decline = mrep.reason if not mrep.committed else 'e2e-differential-rollback'

    # 2) FULL-UNROLL fallback (R2.7 / R2.5, unchanged) so coverage never regresses
    if form == 'register':
        new_f, pr = realize_register_pipeline(sched_sub, 0, len(sched_sub) - 1,
                                              dd, gg, kernel, sched, recur)
    else:
        new_f, pr = generate_pipeline(sched_sub, 0, len(sched_sub) - 1,
                                      dd, gg, kernel, sched)
    if pr.committed and new_f is not None \
            and _promote_diff(sub, new_f, 0, len(sub) - 1) != 'mismatch':
        rep.reason = 'full-unroll-fallback:' + decline
        return _slice_of(new_f, sub[0].name), False, None
    return None


def pipeline_mve_function(instrs, lo, hi, stats, reports):
    """Pipeline the first eligible loop of one function as a COMPACT kernel,
    preferring the register form (promote first for a lower II) and falling back to
    R2.7's full-unroll realiser (then to the memory form) so coverage never
    regresses below R2.7. Returns the (possibly transformed) function slice."""
    fname = getattr(instrs[lo], 'name', '?')
    sub = instrs[lo:hi + 1]
    descs = discover_function(sub, 0, len(sub) - 1)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    disamb = MemoryDisambiguator(sub, 0, len(sub) - 1, descs)
    graph = DependenceGraph(sub, 0, len(sub) - 1, disambiguator=disamb)

    for d in descs:
        stats.loops += 1
        rep = MVEReport(fname, d.header)
        reports.append(rep)
        kern0, why = build_kernel(d, graph)
        if kern0 is None or d.trip_count.kind != TripCount.KNOWN:
            rep.reason = why if kern0 is None else 'trip-not-known'
            continue
        T = d.trip_count.value
        hblk = graph.cfg.blocks[d.header]
        header_label = graph.instrs[hblk.lo].name \
            if type(graph.instrs[hblk.lo]).__name__ == 'IRLabel' else None
        if header_label is None:
            rep.reason = 'no-header-label'
            continue

        # prefer the REGISTER form (lower II), then the MEMORY form
        forms = []
        rplan = _schedule_register_form(sub, T, header_label, rep)
        if rplan is not None:
            forms.append(('register', rplan))
        mplan = _schedule_memory_form(sub, d, graph, rep)
        if mplan is not None:
            forms.append(('memory', (sub, *mplan)))

        for form, plan in forms:
            got = _realize_form(sub, plan, form, rep)
            if got is None:
                continue
            new_slice, compacted, mrep = got
            _record_commit(stats, rep, form, plan[4], compacted, mrep)
            return new_slice

        # nothing committed -> untouched
        if 'differential' in rep.reason:
            stats.rolled_back += 1
        else:
            stats.declined += 1
        stats._bump(rep.reason)
        return sub
    return sub


def _record_commit(stats, rep, form, sched, compacted, mrep):
    rep.form = form
    rep.committed = True
    rep.compacted = compacted
    rep.ii, rep.stages = sched.ii, sched.stages
    stats.sum_ii += sched.ii
    stats.sum_stages += sched.stages
    if compacted:
        rep.reason = 'ok'
        rep.trip = mrep.trip
        rep.bank_size, rep.n_rotating = mrep.bank_size, mrep.n_rotating
        rep.prologue, rep.kernel_body = mrep.prologue, mrep.kernel_body
        rep.epilogue, rep.loop_trips = mrep.epilogue, mrep.loop_trips
        rep.static_full, rep.static_compact = mrep.static_full, mrep.static_compact
        stats.kernel_loops += 1
        stats.mve_mappings += mrep.n_rotating
        stats.sum_bank += mrep.bank_size
        stats.sum_rot += mrep.n_rotating
        stats.static_full += mrep.static_full
        stats.static_compact += mrep.static_compact
        stats._bump(form + '-compact')
    else:
        stats.full_unroll += 1
        stats._bump(form + '-full-unroll')


def _slice_of(instrs, fname):
    for a, b in func_slices(instrs):
        if instrs[a].name == fname:
            return instrs[a:b + 1]
    return instrs


def pipeline_mve_module(instrs):
    """Compact-kernel software pipelining across a module. Returns (new_instrs,
    MVEStats, [MVEReport]). Rebuilds by concatenating each function's (possibly
    pipelined) slice so globals / inter-function code are preserved."""
    _rp_n[0] = 0
    _mve_n[0] = 0
    stats = MVEStats()
    reports = []
    out = []
    prev_end = 0
    for (lo, hi) in func_slices(instrs):
        out.extend(instrs[prev_end:lo])
        prev_end = hi + 1
        stats.functions += 1
        out.extend(pipeline_mve_function(instrs, lo, hi, stats, reports))
    out.extend(instrs[prev_end:])
    return out, stats, reports

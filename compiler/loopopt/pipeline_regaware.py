"""
pipeline_regaware.py -- Register-Aware Software Pipelining (Milestone R2.7).

Integrates R2.6 (loop register promotion) with R2.5 (modulo scheduling). R2.5's
modulo scheduler is already correct and RECURRENCE-AGNOSTIC -- it consumes a
KernelModel whose edges come from the R2.1 DependenceGraph, which already
captures REGISTER recurrences (carried edges on temps) exactly as it captures
memory recurrences. The ONLY thing that made R2.5 reject a register-promoted loop
is its build_kernel ELIGIBILITY test (line: `primary_iv is None or trip_count ==
UNKNOWN`), because M1's induction-variable analysis is memory-slot based and,
after R2.6 promotes the IV to a register, finds no memory IV.

R2.7 therefore changes ONLY the recognition / normalization layer:

  1. Analyse the ORIGINAL (memory-backed) loop with M1 -> its KNOWN trip count T.
  2. Apply R2.6 promotion -> the IV / accumulator become register recurrences and
     RecMII drops (memory load-modify-store ~5 -> register cycle ~3).
  3. NORMALISE the promoted loop's descriptor: register promotion is value- and
     trip-preserving, so carry T forward -- set trip_count = KNOWN(T) and a
     sentinel primary_iv, which is exactly the information R2.5's eligibility
     needs. Nothing else in build_kernel / generate_pipeline is memory-specific.
  4. Feed the promoted loop to R2.5's build_kernel + modulo_schedule +
     generate_pipeline UNCHANGED. The scheduler now exploits the shorter register
     recurrence instead of rejecting the loop.

Memory-recurrence loops that R2.6 does not promote are pipelined by the SAME code
path directly (Case A). There is ONE pipeline generator; both loop forms flow
through the canonical LoopRecurrence view and R2.5's realiser.

Standalone -- not wired into the production compiler. Correctness is mandatory:
the end-to-end transform (original -> promoted -> pipelined) is validated with a
clean-slot-respecting multi-seed differential + compile, and rolled back on any
failure. R2.5 and R2.6 are consumed, never modified.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir_utils import func_slices                                     # noqa: E402
from .discovery import discover_function                            # noqa: E402
from .analysis_iv import (annotate_induction_vars, TripCount)         # noqa: E402
from .analysis_mem import annotate_memory_effects                     # noqa: E402
from .depgraph import (DependenceGraph, MEM_RAW, MEM_WAR, MEM_WAW,     # noqa: E402
                       RAW, WAR, WAW)
from .depgraph_disambig import MemoryDisambiguator                    # noqa: E402
from ir import Temp                                                   # noqa: E402
from .modulo import (build_kernel, min_ii, modulo_schedule,           # noqa: E402
                     generate_pipeline, _edge_latency, _clone_op,
                     _compiles, _MAX_PIPELINE_OPS, PipelineResult)
from .loop_promote import (promote_function, _promote_diff,           # noqa: E402
                           _loop_recmii, _rp_n)


# ── canonical recurrence abstraction ──────────────────────────────────────────

class LoopRecurrence:
    """A single loop-carried recurrence, normalised so the scheduler need not
    care whether it came from memory or a promoted register. Derived from the
    DependenceGraph's carried edges (the same edges R2.5 already schedules on)."""
    __slots__ = ('kind', 'producer', 'consumer', 'distance', 'latency', 'var')

    def __init__(self, kind, producer, consumer, distance, latency, var):
        self.kind = kind                # 'memory' | 'register'
        self.producer = producer        # producer IR index
        self.consumer = consumer        # consumer IR index
        self.distance = distance        # iteration distance
        self.latency = latency          # recurrence latency
        self.var = var                  # temp name (register) or alias key (memory)

    def __repr__(self):
        return (f"LoopRecurrence({self.kind} {self.producer}->{self.consumer} "
                f"d={self.distance} lat={self.latency} {self.var})")


def _carried_register_values(kernel, graph):
    """The set of temp names that carry a VALUE across iterations (a register
    recurrence): the resource of a carried RAW register edge. These must stay
    SHARED through the pipeline (like a memory slot), while all other body temps
    are per-iteration expanded -- exactly how R2.5 already treats memory slots vs
    ordinary temps. (This is NOT modulo variable expansion: no rotating registers,
    no compact kernel; it is the shared loop-carried storage of a full unroll.)"""
    opset = set(kernel.ops)
    names = set()
    for e in graph.carried_edges():
        if e.kind == RAW and isinstance(e.resource, str) \
                and e.src in opset and e.dst in opset:
            names.add(e.resource)
    return names


def realize_register_pipeline(instrs, lo, hi, desc, graph, kernel, sched,
                              recur_names):
    """Realise the modulo schedule of a REGISTER-recurrence loop. Identical to
    R2.5's generate_pipeline (same guards, same linearisation, same R2.5
    `_clone_op`, same structural check) EXCEPT the loop-carried registers in
    `recur_names` are kept SHARED across iterations -- achieved by PRE-SEEDING
    _clone_op's rename cache so those names map to themselves, instead of being
    per-iteration expanded (which would break the recurrence). One realiser, one
    clone routine; only the cache seed differs between the memory and register
    forms."""
    fname = getattr(instrs[lo], 'name', '?')
    pr = PipelineResult(fname, desc.header)
    pr.ii, pr.stages = sched.ii, sched.stages
    if desc.trip_count.kind != TripCount.KNOWN:
        pr.reason = 'trip-not-known'
        return None, pr
    T = desc.trip_count.value
    S = sched.stages
    pr.trip = T
    if S < 2:
        pr.reason = 'single-stage-not-profitable'
        return None, pr
    if T < S + 1:
        pr.reason = 'trip-too-small'
        return None, pr
    if T * len(kernel.ops) > _MAX_PIPELINE_OPS:
        pr.reason = 'code-size-guard'
        return None, pr

    hblk = graph.cfg.blocks[desc.header]
    lblk = graph.cfg.blocks[desc.latches[0]]
    region_lo, region_hi = hblk.lo, lblk.hi

    cyc = sched.cycle
    instances = []
    for it in range(T):
        for op in kernel.ops:
            instances.append((it * sched.ii + cyc[op], it, op))
    instances.sort(key=lambda x: (x[0], x[1], x[2]))

    # PRE-SEED: recurrence registers stay shared (identity across every bank), so
    # iteration k's update feeds iteration k+1 through the one register -- exactly
    # like a memory slot. Every other temp is expanded per iteration as usual.
    cache = {}
    shared = {name: Temp(name) for name in recur_names}
    for name, t in shared.items():
        for it in range(T):
            cache[(name, it)] = t

    body = [_clone_op(graph.instrs[op], it, cache) for (_t, it, op) in instances]
    new = instrs[:region_lo] + body + instrs[region_hi + 1:]

    if len(func_slices(new)) != len(func_slices(instrs)):
        pr.reason = 'structural-slice-mismatch'
        return None, pr
    # gate: promoted vs pipelined (clean-slot-respecting differential) + compile
    if _promote_diff(instrs, new, lo, hi) == 'mismatch':
        pr.reason = 'differential-rollback'
        return None, pr
    if not _compiles(new):
        pr.reason = 'compile-rollback'
        return None, pr

    S = sched.stages
    pr.prologue = sum(1 for (t, _i, _o) in instances if t < (S - 1) * sched.ii)
    pr.epilogue = sum(1 for (t, _i, _o) in instances if t >= T * sched.ii)
    pr.kernel = len(instances) - pr.prologue - pr.epilogue
    pr.overlapped = S
    pr.committed = True
    pr.reason = 'ok'
    return new, pr


def canonical_recurrences(kernel, graph):
    """The kernel's loop-carried recurrences as a memory/register-agnostic list.
    This is the abstraction the scheduler consumes (via the KernelModel edges);
    the view is exposed for reporting and equivalence checks."""
    opset = set(kernel.ops)
    out = []
    for e in graph.edges:
        if not e.carried:
            continue
        if e.src not in opset or e.dst not in opset:
            continue
        if e.kind in (MEM_RAW, MEM_WAR, MEM_WAW):
            kind = 'memory'
        elif e.kind in (RAW, WAR, WAW):
            kind = 'register'
        else:
            continue
        out.append(LoopRecurrence(kind, e.src, e.dst, 1,
                                  _edge_latency(graph, e), e.resource))
    return out


# ── recognition / normalization ───────────────────────────────────────────────

def _find_loop_by_header_label(instrs, lo, hi, header_label):
    """Return the (descs, graph, desc) on instrs[lo..hi] whose header block has
    `header_label`, or (descs, graph, None)."""
    descs = discover_function(instrs, lo, hi)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    disamb = MemoryDisambiguator(instrs, lo, hi, descs)
    graph = DependenceGraph(instrs, lo, hi, disambiguator=disamb)
    tgt = None
    for d in descs:
        blk = graph.cfg.blocks[d.header]
        lbl = graph.instrs[blk.lo]
        if type(lbl).__name__ == 'IRLabel' and lbl.name == header_label:
            tgt = d
            break
    return descs, graph, tgt


def _normalize_register_loop(desc, trip):
    """Carry the (value-preserving) trip count across R2.6 promotion so R2.5's
    memory-slot eligibility is satisfied for a REGISTER recurrence loop. This is
    the whole recognition change -- nothing about scheduling is touched."""
    desc.trip_count = TripCount.known(trip)
    if desc.primary_iv is None:
        desc.primary_iv = -1            # sentinel: a register IV (non-memory)
    desc.is_counted = True


class RegAwareStats:
    def __init__(self):
        self.functions = self.loops = 0
        self.pipelined_memory = self.pipelined_register = 0
        self.declined = self.rolled_back = 0
        self.sum_rec_mem = self.sum_rec_reg = 0
        self.sum_ii = self.sum_stages = self.sum_kernel = 0
        self.reasons = {}

    def _bump(self, k):
        self.reasons[k] = self.reasons.get(k, 0) + 1


class RegAwareReport:
    __slots__ = ('func', 'header', 'form', 'committed', 'reason',
                 'rec_mii', 'ii', 'stages', 'trip')

    def __init__(self, func, header):
        self.func = func
        self.header = header
        self.form = None                # 'memory' | 'register'
        self.committed = False
        self.reason = 'not-eligible'
        self.rec_mii = self.ii = self.stages = self.trip = 0

    def __repr__(self):
        if not self.committed:
            return f"RegAware({self.func}@B{self.header} {self.reason})"
        return (f"RegAware({self.func}@B{self.header} form={self.form} "
                f"RecMII={self.rec_mii} II={self.ii} stages={self.stages})")


# ── the driver ────────────────────────────────────────────────────────────────

def _try_register_form(orig_sub, desc0, T, header_label, rep):
    """Promote (R2.6) then pipeline the register recurrence via R2.5. Returns the
    pipelined function slice (list) or None. Validated end-to-end vs the ORIGINAL
    with a clean-slot-respecting differential."""
    promoted, preps = promote_function(orig_sub, 0, len(orig_sub) - 1)
    if not any(r.committed for r in preps):
        return None                                     # nothing promoted -> Case A
    # locate the (now register) loop on the promoted slice
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
    sched, sw = modulo_schedule(kernel2, mii)
    if sched is None:
        rep.reason = 'no-schedule'
        return None
    # realise, keeping the loop-carried registers shared (the register recurrence)
    recur = _carried_register_values(kernel2, graph2)
    new_full, pr = realize_register_pipeline(promoted, 0, len(promoted) - 1, d2,
                                             graph2, kernel2, sched, recur)
    if not pr.committed or new_full is None:
        rep.reason = 'gen:' + pr.reason
        return None
    # end-to-end gate: ORIGINAL vs pipelined-promoted (clean-slot-respecting)
    if _promote_diff(orig_sub, new_full, 0, len(orig_sub) - 1) == 'mismatch':
        rep.reason = 'e2e-differential-rollback'
        return None
    rep.form = 'register'
    rep.committed = True
    rep.reason = 'ok'
    rep.rec_mii = rec
    rep.ii = sched.ii
    rep.stages = sched.stages
    rep.trip = T
    return new_full


def _try_memory_form(orig_sub, desc0, graph0, rep):
    """Pipeline the memory recurrence directly (Case A) via R2.5 -- same code
    path, no promotion. Returns the pipelined slice or None."""
    kernel, why = build_kernel(desc0, graph0)
    if kernel is None:
        rep.reason = f'kernel:{why}'
        return None
    mii, rec, res = min_ii(kernel)
    sched, sw = modulo_schedule(kernel, mii)
    if sched is None:
        rep.reason = 'no-schedule'
        return None
    new_full, pr = generate_pipeline(orig_sub, 0, len(orig_sub) - 1, desc0,
                                     graph0, kernel, sched)
    if not pr.committed or new_full is None:
        rep.reason = 'gen:' + pr.reason
        return None
    rep.form = 'memory'
    rep.committed = True
    rep.reason = 'ok'
    rep.rec_mii = rec
    rep.ii = sched.ii
    rep.stages = sched.stages
    rep.trip = desc0.trip_count.value
    return new_full


def pipeline_regaware_function(instrs, lo, hi, stats, reports):
    """Pipeline the first eligible loop of one function, preferring the register
    form (promote first for a lower II). Returns the (possibly transformed)
    function slice as a list."""
    fname = getattr(instrs[lo], 'name', '?')
    sub = instrs[lo:hi + 1]
    descs = discover_function(sub, 0, len(sub) - 1)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    disamb = MemoryDisambiguator(sub, 0, len(sub) - 1, descs)
    graph = DependenceGraph(sub, 0, len(sub) - 1, disambiguator=disamb)

    for d in descs:
        stats.loops += 1
        rep = RegAwareReport(fname, d.header)
        reports.append(rep)
        # eligibility on the ORIGINAL (memory) loop: R2.5's build_kernel gate
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

        # prefer the REGISTER form (lower II); fall back to the MEMORY form
        result = _try_register_form(sub, d, T, header_label, rep)
        if result is not None:
            stats.pipelined_register += 1
            stats.sum_rec_reg += rep.rec_mii
            stats.sum_ii += rep.ii
            stats.sum_stages += rep.stages
            stats._bump('register')
            return result
        # register form declined -> try memory form on the original
        rep2 = RegAwareReport(fname, d.header)
        result = _try_memory_form(sub, d, graph, rep2)
        if result is not None:
            reports.append(rep2)
            stats.pipelined_memory += 1
            stats.sum_rec_mem += rep2.rec_mii
            stats.sum_ii += rep2.ii
            stats.sum_stages += rep2.stages
            stats._bump('memory')
            return result
        if rep.reason == 'e2e-differential-rollback' or 'differential' in rep.reason:
            stats.rolled_back += 1
        else:
            stats.declined += 1
        stats._bump(rep.reason)
        return sub                                       # one loop attempt per fn
    return sub


def pipeline_regaware_module(instrs):
    """Register-aware software pipelining across a module. Rebuilds by
    concatenating each function's (possibly pipelined) slice so globals /
    inter-function code are preserved. Returns (new_instrs, RegAwareStats,
    [RegAwareReport])."""
    _rp_n[0] = 0                                    # deterministic promotion temps
    stats = RegAwareStats()
    reports = []
    out = []
    prev_end = 0
    for (lo, hi) in func_slices(instrs):
        out.extend(instrs[prev_end:lo])
        prev_end = hi + 1
        stats.functions += 1
        out.extend(pipeline_regaware_function(instrs, lo, hi, stats, reports))
    out.extend(instrs[prev_end:])
    return out, stats, reports

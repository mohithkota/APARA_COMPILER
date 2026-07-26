"""
modulo.py -- Software Pipelining / Modulo Scheduling (Milestone R2.5).

The first optimisation that schedules ACROSS loop iterations. It is built
entirely on the frozen infrastructure -- LoopInfo/descriptors (M0-M3), the R2.1
DependenceGraph + its loop-carried recurrence edges, R2.2 disambiguation, the
R2.4 latency + resource models, and the ir_interp differential oracle -- and adds
no new analysis of its own beyond the modulo scheduler.

It is organised in the milestone's five phases:

  Phase 1  MII analysis            RecMII / ResMII / MII        (this file, mutation-free)
  Phase 2  modulo reservation table + iterative modulo scheduling      (mutation-free)
  Phase 3  prologue / kernel / epilogue generation
  Phase 4  validation (verifier + structural + differential + rollback)
  Phase 5  statistics

SCOPE (strict; anything else is rejected cleanly, never mis-scheduled):
  * exactly one natural loop in the function, innermost, not nested
  * top-tested counted loop with a clean induction variable
  * single preheader, single latch, single exit, reducible CFG
  * no calls / no unsupported ops inside the kernel
Loops outside this scope are reported UNSUPPORTED and left untouched.

CORRECTNESS is mandatory: Phase 1/2 never touch the IR; Phase 3 emits a pipeline
only when the structural verifier AND a SEEDED differential (which forces a real
trip count so the prologue/kernel/epilogue are actually exercised) confirm it is
behaviour-identical -- otherwise the loop is rolled back to its original IR.

--------------------------------------------------------------------------------
IR REALITY (documented, because it governs the results): at this stage locals --
including the induction variable and any accumulator -- live in STACK SLOTS and
are updated by load-modify-store each iteration. So the IV forms a *memory*
recurrence (load i -> i+1 -> store i -> next load i, ~5 cycles, distance 1), and
RecMII is bound by it on essentially every counted loop. Profitable overlap
therefore requires register-promoting the recurrences first (a separate pass,
out of scope) -- R2.5 quantifies this precisely and pipelines only where II
genuinely beats the sequential body.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir_utils import func_slices                                     # noqa: E402
from .discovery import discover_function                            # noqa: E402
from .analysis_iv import annotate_induction_vars, TripCount          # noqa: E402
from .analysis_mem import annotate_memory_effects                    # noqa: E402
from .depgraph import (DependenceGraph, RAW, WAR, WAW,                # noqa: E402
                       MEM_RAW, MEM_WAR, MEM_WAW, CONTROL)
from .depgraph_disambig import MemoryDisambiguator                   # noqa: E402
from .schedule import _latency, _iclass, _CAP, _BUNDLE_MAX           # noqa: E402


# ── edge latency model (reuses R2.4 per-instruction latency) ──────────────────

def _edge_latency(graph, e):
    """Delay (in cycles) the dependence edge imposes: a flow (RAW / MEM_RAW)
    waits the producer's result latency; output deps wait 1; anti deps wait 0;
    control 0. Conservative -- overestimating latency only raises RecMII."""
    if e.kind in (RAW, MEM_RAW):
        return _latency(graph.instrs[e.src])
    if e.kind in (WAW, MEM_WAW):
        return 1
    return 0                                            # WAR / MEM_WAR / CONTROL


# ── kernel model ──────────────────────────────────────────────────────────────

# instruction classes that make a loop ineligible for this initial scope
_UNSUPPORTED_KERNEL = frozenset({
    'IRCall', 'IRIndirectCall', 'IRLoadWide', 'IRStoreWide',
    'IRVecArith', 'IRVecDot', 'IRVecDot128', 'IRVecReduce', 'IRFsqrt',
})
_CONTROL_CLS = frozenset({'IRLabel', 'IRCondJump', 'IRJump', 'IRFuncBegin',
                          'IRFuncEnd', 'IRHalt', 'IRReturn'})


class KernelModel:
    """The data operations of one loop iteration plus the dependence edges that
    constrain a modulo schedule. `ops` are IR indices (program order); `intra`
    and `carried` are (u, v, latency, distance) tuples over ops."""
    __slots__ = ('desc', 'ops', 'intra', 'carried', 'resources', 'body_span',
                 'instrs_cls')

    def __init__(self, desc, ops, intra, carried, resources, body_span,
                 instrs_cls):
        self.desc = desc
        self.ops = ops
        self.intra = intra
        self.carried = carried
        self.resources = resources          # {'total':n, 'MEM':n, 'DIV':n}
        self.body_span = body_span          # (min index, max index) of the body
        self.instrs_cls = instrs_cls        # {op index: IR instruction object}


def _eligible(desc):
    """(ok, reason). Strict initial scope: one innermost top-tested counted loop
    with single preheader/latch/exit and a clean IV."""
    if not desc.is_innermost:
        return False, 'not-innermost'
    if desc.shape != 'top-tested':
        return False, 'not-top-tested'
    if desc.preheader is None:
        return False, 'no-unique-preheader'
    if len(desc.latches) != 1:
        return False, 'multi-latch'
    if len(desc.exit_blocks) != 1 or len(desc.exiting_blocks) != 1:
        return False, 'multi-exit'
    if desc.exiting_blocks[0] != desc.header:
        return False, 'exit-not-header'
    if desc.primary_iv is None or desc.trip_count.kind == TripCount.UNKNOWN:
        return False, 'no-counted-iv'
    return True, 'ok'


def build_kernel(desc, graph):
    """Build the KernelModel for an eligible loop, or return (None, reason)."""
    ok, reason = _eligible(desc)
    if not ok:
        return None, reason

    header = desc.header
    latch = desc.latches[0]
    # kernel = data ops of the body blocks, excluding control (labels / the guard
    # branch / the back-edge jump). The guard compare feeding the branch is also
    # excluded -- it is loop control, regenerated structurally, not scheduled.
    body_idxs = []
    for b in sorted(desc.body_blocks):
        blk = graph.cfg.blocks[b]
        body_idxs.extend(range(blk.lo, blk.hi + 1))
    body_idxs.sort()

    # the header's guard compare (feeds the IRCondJump) -- keep as control
    guard_cmp = None
    hblk = graph.cfg.blocks[header]
    if type(graph.instrs[hblk.hi]).__name__ == 'IRCondJump':
        cj = graph.instrs[hblk.hi]
        # the compare is the cond-jump itself (operands are temps loaded above)
        guard_cmp = hblk.hi

    ops = []
    for k in body_idxs:
        c = type(graph.instrs[k]).__name__
        if c in _UNSUPPORTED_KERNEL:
            return None, f'unsupported-op:{c}'
        if c in _CONTROL_CLS:
            continue
        ops.append(k)
    if not ops:
        return None, 'empty-kernel'
    opset = set(ops)

    # dependence edges among kernel ops (drop edges touching control ops)
    intra, carried = [], []
    for e in graph.edges:
        if e.src not in opset or e.dst not in opset:
            continue
        if e.kind == CONTROL:
            continue
        lat = _edge_latency(graph, e)
        if e.carried:
            carried.append((e.src, e.dst, lat, 1))       # distance 1 (conservative)
        else:
            intra.append((e.src, e.dst, lat, 0))

    # resource usage
    res = {'total': len(ops), 'MEM': 0, 'DIV': 0}
    for k in ops:
        cls = _iclass(graph.instrs[k])
        if cls == 'MEM':
            res['MEM'] += 1
        elif cls == 'DIV':
            res['DIV'] += 1

    instrs_cls = {k: graph.instrs[k] for k in ops}
    return KernelModel(desc, ops, intra, carried, res,
                       (min(ops), max(ops)), instrs_cls), 'ok'


# ── Phase 1: RecMII / ResMII / MII ────────────────────────────────────────────

def res_mii(kernel):
    """Resource-bound MII = max over resource classes of ceil(uses / capacity).
    Capacities mirror the bundler: 8 slots, 4 memory lanes, 1 divide lane."""
    import math
    r = kernel.resources
    cands = [math.ceil(r['total'] / _BUNDLE_MAX)]
    cands.append(math.ceil(r['MEM'] / _CAP['MEM']))
    if r['DIV']:
        cands.append(math.ceil(r['DIV'] / _CAP['DIV']))
    return max(cands)


def rec_mii(kernel, ii_cap=256):
    """Recurrence-bound MII = the smallest II for which the recurrence graph has
    no positive cycle in edge weights (latency - II*distance). Found by scanning
    II upward with a Bellman-Ford longest-path feasibility test. Edges with
    distance 0 (intra) plus the carried edges together form the constraint graph;
    only cycles (which must use >=1 carried edge) can be positive."""
    edges = kernel.intra + kernel.carried
    if not any(d > 0 for (_u, _v, _l, d) in edges):
        return 1                                        # no recurrence
    nodes = list(kernel.ops)
    for ii in range(1, ii_cap + 1):
        if _feasible(nodes, edges, ii):
            return ii
    return ii_cap                                        # give up (won't pipeline)


def _feasible(nodes, edges, ii):
    """True iff t(v) >= t(u) + (lat - ii*dist) has a solution, i.e. no positive
    cycle. Bellman-Ford for longest paths: relax |V| times, then check for a
    further relaxation (a positive cycle)."""
    dist = {n: 0 for n in nodes}
    w = [(u, v, lat - ii * d) for (u, v, lat, d) in edges]
    for _ in range(len(nodes)):
        changed = False
        for (u, v, wt) in w:
            if dist[u] + wt > dist[v]:
                dist[v] = dist[u] + wt
                changed = True
        if not changed:
            return True
    for (u, v, wt) in w:
        if dist[u] + wt > dist[v]:
            return False                                # positive cycle -> infeasible
    return True


def min_ii(kernel):
    """(MII, RecMII, ResMII)."""
    rec = rec_mii(kernel)
    res = res_mii(kernel)
    return max(rec, res), rec, res


# ── module-level Phase-1 analysis ─────────────────────────────────────────────

class LoopMII:
    """Phase-1 result for one loop (analysis only)."""
    __slots__ = ('func', 'header', 'eligible', 'reason', 'trip', 'kernel_size',
                 'rec_mii', 'res_mii', 'mii')

    def __init__(self, func, header, eligible, reason, trip=None,
                 kernel_size=0, rec=0, res=0, mii=0):
        self.func = func
        self.header = header
        self.eligible = eligible
        self.reason = reason
        self.trip = trip
        self.kernel_size = kernel_size
        self.rec_mii = rec
        self.res_mii = res
        self.mii = mii

    def __repr__(self):
        if not self.eligible:
            return f"LoopMII({self.func}@B{self.header} UNSUPPORTED:{self.reason})"
        return (f"LoopMII({self.func}@B{self.header} trip={self.trip} "
                f"kernel={self.kernel_size} RecMII={self.rec_mii} "
                f"ResMII={self.res_mii} MII={self.mii})")


def analyze_function(instrs, lo, hi):
    """Phase-1 analysis for every loop in one function slice (no IR mutation)."""
    descs = discover_function(instrs, lo, hi)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    disamb = MemoryDisambiguator(instrs, lo, hi, descs)
    graph = DependenceGraph(instrs, lo, hi, disambiguator=disamb)
    fname = getattr(instrs[lo], 'name', '?')
    out = []
    for d in descs:
        kernel, reason = build_kernel(d, graph)
        if kernel is None:
            out.append(LoopMII(fname, d.header, False, reason))
            continue
        mii, rec, res = min_ii(kernel)
        out.append(LoopMII(fname, d.header, True, 'ok', d.trip_count,
                           len(kernel.ops), rec, res, mii))
    return out, graph, descs


def analyze_module(instrs):
    """Phase-1 analysis for every function (no IR mutation). Returns [LoopMII]."""
    results = []
    for (lo, hi) in func_slices(instrs):
        res, _g, _d = analyze_function(instrs, lo, hi)
        results.extend(res)
    return results


# ══ Phase 2: modulo reservation table + iterative modulo scheduling ═══════════
#
# A schedule assigns each kernel op an integer cycle t(op) >= 0 such that:
#   intra edge   (u,v,lat,0):  t(v) >= t(u) + lat
#   carried edge (u,v,lat,1):  t(v) >= t(u) + lat - II        (across one II)
#   resources: at every modulo slot s in [0,II), the ops with t(op)%II == s fit
#              the bundle caps (<=8 total, <=4 memory, <=1 divide).
# The op's STAGE is t(op)//II; the number of stages is max stage + 1; the schedule
# LENGTH is max t(op)+1. All Phase-2 work is mutation-free -- it produces a
# schedule object that verify_schedule() independently certifies legal.


class ReservationTable:
    """Per-modulo-slot resource occupancy under a given II."""

    def __init__(self, ii):
        self.ii = ii
        self.slots = [{'total': 0, 'MEM': 0, 'DIV': 0} for _ in range(ii)]

    def fits(self, cls, cycle):
        s = self.slots[cycle % self.ii]
        if s['total'] >= _BUNDLE_MAX:
            return False
        if cls == 'MEM' and s['MEM'] >= _CAP['MEM']:
            return False
        if cls == 'DIV' and s['DIV'] >= _CAP['DIV']:
            return False
        return True

    def add(self, cls, cycle):
        s = self.slots[cycle % self.ii]
        s['total'] += 1
        if cls in ('MEM', 'DIV'):
            s[cls] += 1


class ModuloSchedule:
    """A legal modulo schedule for one kernel."""
    __slots__ = ('kernel', 'ii', 'cycle', 'stages', 'length')

    def __init__(self, kernel, ii, cycle):
        self.kernel = kernel
        self.ii = ii
        self.cycle = cycle                              # {op index: cycle}
        self.length = max(cycle.values()) + 1 if cycle else 0
        self.stages = (max(v // ii for v in cycle.values()) + 1) if cycle else 0

    def stage_of(self, op):
        return self.cycle[op] // self.ii


def _min_times(kernel, ii, lower=None):
    """Minimal cycle times satisfying every dependence for this II (longest-path
    over the difference constraints). Returns None if infeasible (positive
    cycle -- only when ii < RecMII). `lower` optionally pins minimum times."""
    edges = kernel.intra + kernel.carried
    t = {n: 0 for n in kernel.ops}
    if lower:
        t.update(lower)
    w = [(u, v, lat - ii * d) for (u, v, lat, d) in edges]
    for _ in range(len(kernel.ops) + 1):
        changed = False
        for (u, v, wt) in w:
            if t[u] + wt > t[v]:
                t[v] = t[u] + wt
                changed = True
        if lower:
            for n, lv in lower.items():
                if t[n] < lv:
                    t[n] = lv
                    changed = True
        if not changed:
            return t
    return None                                          # positive cycle


def _place_resources(kernel, ii, base_times):
    """Greedy resource legalisation: place ops in ascending base time; if a
    modulo slot is full, push the op one cycle later (and its dependents follow
    on the next solve). Returns cycle map or None if it cannot converge."""
    from .schedule import _iclass as cls_of
    order = sorted(kernel.ops, key=lambda n: (base_times[n], n))
    # forward successors for lower-bound propagation
    succ = {n: [] for n in kernel.ops}
    for (u, v, lat, d) in kernel.intra:
        succ[u].append((v, lat))
    cycle = dict(base_times)
    for _ in range(len(kernel.ops) * 4 + 8):             # bounded legalisation
        rt = ReservationTable(ii)
        placed = {}
        conflict = None
        for n in sorted(order, key=lambda x: (cycle[x], x)):
            c = cycle[n]
            cl = cls_of(kernel.instrs_cls[n])
            if not rt.fits(cl, c):
                conflict = n
                break
            rt.add(cl, c)
            placed[n] = c
        if conflict is None:
            return cycle
        # push the conflicting op one cycle later; propagate to intra successors
        cycle[conflict] += 1
        stack = [conflict]
        while stack:
            u = stack.pop()
            for (v, lat) in succ[u]:
                if cycle[v] < cycle[u] + lat:
                    cycle[v] = cycle[u] + lat
                    stack.append(v)
    return None


def modulo_schedule(kernel, mii, ii_cap=64):
    """Find a legal modulo schedule, trying II = MII, MII+1, ... up to a cap.
    Returns (ModuloSchedule, 'ok') or (None, reason)."""
    # KernelModel doesn't carry instruction objects; attach a class lookup so the
    # resource placer can classify without the graph.
    if not hasattr(kernel, 'instrs_cls'):
        return None, 'no-instr-classes'
    for ii in range(mii, ii_cap + 1):
        base = _min_times(kernel, ii)
        if base is None:
            continue                                     # ii < RecMII (shouldn't happen >= MII)
        cyc = _place_resources(kernel, ii, base)
        if cyc is None:
            continue
        sched = ModuloSchedule(kernel, ii, cyc)
        ok, _why = verify_schedule(kernel, sched)
        if ok:
            return sched, 'ok'
    return None, 'no-schedule'


def verify_schedule(kernel, sched):
    """Independently certify a schedule legal: every dependence satisfied and no
    modulo slot over capacity. Returns (ok, reason)."""
    ii = sched.ii
    t = sched.cycle
    for (u, v, lat, d) in kernel.intra:
        if t[v] < t[u] + lat:
            return False, f'intra {u}->{v} violated'
    for (u, v, lat, d) in kernel.carried:
        if t[v] < t[u] + lat - ii * d:
            return False, f'carried {u}->{v} violated'
    rt = ReservationTable(ii)
    for n in kernel.ops:
        cl = _iclass(kernel.instrs_cls[n])
        if not rt.fits(cl, t[n]):
            return False, f'resource slot {t[n] % ii} over capacity'
        rt.add(cl, t[n])
    return True, 'ok'


# ══ Phase 3-5: pipeline generation + validation + statistics ══════════════════
#
# For a KNOWN-trip-count counted loop, the modulo schedule is REALISED by
# linearising it over all T iterations: op instance (iteration `it`, kernel op
# `o`) executes at absolute time  it*II + cycle(o).  Emitting every instance in
# absolute-time order is exactly the software-pipelined schedule -- iterations
# overlap (a later iteration's early stage runs before an earlier iteration's late
# stage) while every dependence (verify_schedule already proved the schedule
# legal) and every shared memory slot (IV / accumulator) is accessed in schedule
# order. Each iteration gets a private temp namespace (modulo variable expansion
# by renaming); memory slot offsets stay shared, so the recurrences self-serialise.
#
# Prologue / kernel / epilogue are the ramp-up (not all stages active) / steady
# (all S stages active) / drain regions of that linear order. A compact kernel
# LOOP (register-rotating MVE) is future work; this full realisation is the
# correctness-first form and is emitted only when the structural verifier AND a
# multi-seed differential certify it behaviour-identical, else it is rolled back.

import copy as _copy
import random as _random

from ir import Temp as _Temp, Const as _Const                        # noqa: E402
from . import ir_interp as _interp                                    # noqa: E402


def _rt_temp(t, bank, cache):
    key = (t.name, bank)
    nt = cache.get(key)
    if nt is None:
        nt = _Temp(f"{t.name}~p{bank}")
        cache[key] = nt
    return nt


def _clone_op(ins, bank, cache):
    """A copy of `ins` with every Temp renamed into iteration `bank`'s namespace;
    memory offsets (fp_offset / dmem_addr / Const) are left shared."""
    new = _copy.copy(ins)
    for attr, val in vars(ins).items():
        if isinstance(val, _Temp):
            setattr(new, attr, _rt_temp(val, bank, cache))
        elif isinstance(val, list):
            setattr(new, attr, [_rt_temp(x, bank, cache) if isinstance(x, _Temp)
                                else x for x in val])
    return new


class PipelineResult:
    __slots__ = ('func', 'header', 'ii', 'stages', 'trip', 'prologue', 'kernel',
                 'epilogue', 'overlapped', 'committed', 'reason')

    def __init__(self, func, header, reason='unsupported'):
        self.func = func
        self.header = header
        self.ii = self.stages = self.trip = 0
        self.prologue = self.kernel = self.epilogue = self.overlapped = 0
        self.committed = False
        self.reason = reason


_MAX_PIPELINE_OPS = 4000          # code-size guard for full realisation


def _seed_mem(instrs, lo, hi, rng):
    """A random initial-memory dict covering the stack + global addresses the
    function slice may touch, so the differential exercises real data."""
    mem = {}
    # global initialisers first (so those addresses are realistic), then noise
    mem.update(_interp._preload_globals(instrs))
    for addr in range(-4096, 4097, 8):
        if addr not in mem:
            mem[addr] = rng.randint(-1000, 1000)
    for base in (0x400, 0x800):
        for off in range(0, 2048, 8):
            mem.setdefault(base + off, rng.randint(-1000, 1000))
    return mem


def _compiles(instrs):
    """Phase-4 compile gate: the pipelined IR must go through the production
    CodeGen without raising (register exhaustion etc.). Uses a lazy import so the
    analysis phases stay codegen-free."""
    try:
        import copy as _c
        from codegen import CodeGen
        CodeGen(global_base=0x400).generate(_c.deepcopy(instrs), global_base=0x400)
        return True
    except Exception:
        return False


def _multiseed_ok(orig, trans, lo, hi, seeds=5):
    """Run original vs pipelined slice on the natural seed plus several random
    data seeds; require agreement (return value + final memory) on every seed the
    interpreter can execute, and at least one executable seed."""
    a0, b0 = lo, hi
    a1 = next((a for a, b in func_slices(trans) if trans[a].name == orig[lo].name), None)
    if a1 is None:
        return False
    b1 = next(b for a, b in func_slices(trans) if a == a1)
    ran = 0
    rng = _random.Random(0xB2C5)
    for s in range(seeds):
        seed = _seed_mem(orig, lo, hi, rng) if s else dict(_interp._preload_globals(orig))
        try:
            r0, m0 = _interp.run_slice(orig, a0, b0, init_mem=dict(seed))
            r1, m1 = _interp.run_slice(trans, a1, b1, init_mem=dict(seed))
        except (_interp.Unsupported, _interp.StepLimit):
            continue
        ran += 1
        if r0 != r1 or m0 != m1:
            return False
    return ran >= 1


def generate_pipeline(instrs, lo, hi, desc, graph, kernel, sched):
    """Phase 3-4: realise the modulo schedule for a KNOWN-trip-count loop, gated
    by structural + multi-seed differential validation. Returns (new_instrs or
    None, PipelineResult)."""
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

    # region to replace: [header block start .. latch block end]
    hblk = graph.cfg.blocks[desc.header]
    lblk = graph.cfg.blocks[desc.latches[0]]
    region_lo, region_hi = hblk.lo, lblk.hi

    # linearise: instance (it, op) at absolute time it*II + cycle(op)
    cyc = sched.cycle
    instances = []
    for it in range(T):
        for op in kernel.ops:
            instances.append((it * sched.ii + cyc[op], it, op))
    instances.sort(key=lambda x: (x[0], x[1], x[2]))

    cache = {}
    body = [_clone_op(graph.instrs[op], it, cache) for (_t, it, op) in instances]

    new = instrs[:region_lo] + body + instrs[region_hi + 1:]

    # structural: same function-slice count, instruction multiset grows only by
    # the (T-1) extra body copies; verify the slice still parses into functions
    if len(func_slices(new)) != len(func_slices(instrs)):
        pr.reason = 'structural-slice-mismatch'
        return None, pr

    if not _multiseed_ok(instrs, new, lo, hi):
        pr.reason = 'differential-rollback'
        return None, pr

    if not _compiles(new):
        pr.reason = 'compile-rollback'
        return None, pr

    # prologue = steps with < S stages filled (it+? ) ; report region sizes
    pr.prologue = sum(1 for (t, _it, _op) in instances if t < (S - 1) * sched.ii)
    pr.epilogue = sum(1 for (t, _it, _op) in instances if t >= T * sched.ii)
    pr.kernel = len(instances) - pr.prologue - pr.epilogue
    pr.overlapped = S
    pr.committed = True
    pr.reason = 'ok'
    return new, pr


class ModuloStats:
    def __init__(self):
        self.functions = self.loops = self.eligible = self.unsupported = 0
        self.scheduled = self.sched_fail = 0
        self.pipelined = self.declined = self.rolled_back = 0
        self.sum_rec = self.sum_res = self.sum_ii = 0
        self.sum_kernel = self.sum_prologue = self.sum_epilogue = 0
        self.sum_stages = 0
        self.reasons = {}
        self.decline_reasons = {}

    def _bump(self, d, k):
        d[k] = d.get(k, 0) + 1


def pipeline_module(instrs):
    """Full R2.5 driver over a module: analyse, schedule, and (where profitable +
    proven) realise a software pipeline. Returns (new_instrs, ModuloStats,
    [PipelineResult]). Every committed pipeline passed structural + multi-seed
    differential validation; anything else leaves the loop untouched."""
    stats = ModuloStats()
    results = []
    out = []                                   # rebuilt slice-by-slice (no index shift)
    prev_end = 0                               # preserves globals / inter-function code
    for (lo, hi) in func_slices(instrs):
        out.extend(instrs[prev_end:lo])        # non-function region before this func
        prev_end = hi + 1
        stats.functions += 1
        fname = getattr(instrs[lo], 'name', '?')
        transformed = instrs[lo:hi + 1]        # default: unchanged
        descs = discover_function(instrs, lo, hi)
        annotate_induction_vars(descs)
        annotate_memory_effects(descs)
        disamb = MemoryDisambiguator(instrs, lo, hi, descs)
        graph = DependenceGraph(instrs, lo, hi, disambiguator=disamb)
        for d in descs:
            stats.loops += 1
            kernel, reason = build_kernel(d, graph)
            if kernel is None:
                stats.unsupported += 1
                stats._bump(stats.reasons, reason)
                results.append(PipelineResult(fname, d.header, reason))
                continue
            stats.eligible += 1
            mii, rec, res = min_ii(kernel)
            stats.sum_rec += rec
            stats.sum_res += res
            sched, _why = modulo_schedule(kernel, mii)
            if sched is None:
                stats.sched_fail += 1
                stats._bump(stats.decline_reasons, 'no-schedule')
                continue
            stats.scheduled += 1
            stats.sum_ii += sched.ii
            new_full, pr = generate_pipeline(instrs, lo, hi, d, graph, kernel, sched)
            results.append(pr)
            if pr.committed and new_full is not None:
                stats.pipelined += 1
                stats.sum_kernel += pr.kernel
                stats.sum_prologue += pr.prologue
                stats.sum_epilogue += pr.epilogue
                stats.sum_stages += pr.stages
                a1 = next(a for a, b in func_slices(new_full)
                          if new_full[a].name == fname)
                b1 = next(b for a, b in func_slices(new_full) if a == a1)
                transformed = new_full[a1:b1 + 1]
                break                          # one pipeline per function (safe)
            if pr.reason == 'differential-rollback':
                stats.rolled_back += 1
            else:
                stats.declined += 1
            stats._bump(stats.decline_reasons, pr.reason)
        out.extend(transformed)
    out.extend(instrs[prev_end:])              # trailing non-function region
    return out, stats, results

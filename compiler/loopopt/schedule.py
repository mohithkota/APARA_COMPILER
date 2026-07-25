"""
schedule.py -- Dependence-Aware IR Scheduler (Milestone R2.3).

The FIRST optimisation that consumes the DependenceGraph. It reorders IR *within
basic blocks* to present the existing assembly bundler with a denser, ILP-exposed
order, while preserving semantics exactly. It never schedules across basic
blocks, never redesigns the graph or the bundler, and implements no software
pipelining / modulo / trace / superblock scheduling.

================================================================================
WHAT IT CONSUMES  (reused, nothing re-derived)
================================================================================
    loopopt.DependenceGraph              -- R2.1 graph: nodes, edges, CFG blocks
    loopopt.MemoryDisambiguator          -- R2.2 memory precision (fewer false
                                            memory edges => more scheduling freedom)
    loopopt.ir_interp.differential        -- the R1.x differential oracle: the
                                            per-function correctness gate
    ir_utils.func_slices                 -- per-function scoping

================================================================================
LEGALITY  (why an intra-block reorder is semantics-preserving)
================================================================================
A basic block runs as a straight-line sequence every time control enters it.
Reordering its instructions is legal iff the new order is a topological order of
the block's dependence constraints for a SINGLE execution -- i.e. the R2.x edges
whose BOTH endpoints lie in the block and that are NOT loop-carried
(RAW/WAR/WAW/MEM_*/CONTROL). Every such edge runs low->high program index, so the
constraint graph is acyclic and the original order is one valid topo order; any
other topo order is an equally-legal schedule.

  * Cross-block edges impose no intra-block constraint: block boundaries and
    inter-block order are fixed, so every instruction of an earlier block still
    precedes every instruction of a later one.
  * LOOP-CARRIED edges are respected BY CONSTRUCTION: an intra-block reorder can
    never move an instruction across the back-edge, so whole-iteration-before-
    whole-iteration ordering is preserved automatically. They must NOT be added
    as intra-block constraints -- a carried edge j->i (j>i) would cycle with its
    intra-iteration partner i->j and wrongly forbid all schedules. (Reasoning
    about carried edges to move work across iterations is software pipelining --
    explicitly out of scope.)

Correctness is doubly guarded: the schedule is a topo order of the conservative
dependence DAG (a proof), AND `ir_interp.differential` re-executes the function
before/after and compares observable behaviour, rolling the function back on any
mismatch.

================================================================================
PRIORITY  (deterministic critical-path / dependency-height list scheduling)
================================================================================
Among the ready instructions (all predecessors already placed) pick the one with
the greatest DEPENDENCY HEIGHT -- the length of the longest chain of dependent
instructions below it in the block's DAG (unit latency). Ties break by smallest
original program index. Rationale: scheduling the tallest dependence chains first
starts long-latency producers early and INTERLEAVES independent chains, so
mutually-independent instructions land adjacently -- exactly what the greedy VLIW
bundler needs to pack a dense bundle. The tie-break makes the output a pure
function of the input (fully deterministic / reproducible).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir_utils import func_slices, dest_names, src_names             # noqa: E402
from analysis import compute_liveness                               # noqa: E402
from .depgraph import DependenceGraph                                # noqa: E402
from .depgraph_disambig import MemoryDisambiguator, _function_descs  # noqa: E402
from . import ir_interp                                              # noqa: E402

# markers pinned to the front of their block (entry points / frame setup)
_LEADER_PIN = frozenset({'IRLabel', 'IRFuncBegin'})
# markers pinned to the end of their block (control leaves here)
_TRAILER_PIN = frozenset({'IRJump', 'IRCondJump', 'IRReturn', 'IRHalt', 'IRFuncEnd'})


# ══ R2.4 scheduler-quality models (additive; R2.3 core is unchanged) ═══════════
#
# Conservative per-instruction LATENCY, estimated from the existing ISA (the same
# op families the bundler's `_forces_full_bundle` treats as expensive: loads,
# divide, fsqrt, calls). Relative values only -- they weight the critical path.
_LAT_DEFAULT = 1


def _latency(ins):
    c = type(ins).__name__
    if c in ('IRLoad', 'IRGlobalLoad', 'IRLoadWide'):
        return 3                                    # memory load result latency
    if c in ('IRFsqrt',):
        return 8
    if c == 'IRBinOp':
        if ins.op in ('/', '%'):
            return 8                                # divide / modulo
        if ins.op == '*':
            return 3                                # multiply
        return 1
    if c in ('IRCall', 'IRIndirectCall'):
        return 5
    if c in ('IRVecDot', 'IRVecDot128'):
        return 4
    if c == 'IRVecReduce':
        return 3
    if c == 'IRVecArith':
        return 2
    return _LAT_DEFAULT                             # ALU / address / store / move


# Bundler resource CLASS + per-bundle caps (mirrors the bundler's known lane
# limits: <=4 loads/stores, <=1 divide/sqrt, control ends a bundle; 8 total).
# Used ONLY to inform tie-breaks / the utilisation estimate -- it never builds a
# real bundle (that stays the bundler's job).
_MEM = frozenset({'IRLoad', 'IRStore', 'IRGlobalLoad', 'IRGlobalStore',
                  'IRLoadWide', 'IRStoreWide'})
_CTL = frozenset({'IRCondJump', 'IRJump', 'IRReturn', 'IRHalt',
                  'IRCall', 'IRIndirectCall'})
_CAP = {'MEM': 4, 'DIV': 1, 'CTL': 1, 'ALU': 8}
_BUNDLE_MAX = 8


def _iclass(ins):
    c = type(ins).__name__
    if c in _MEM:
        return 'MEM'
    if c == 'IRFsqrt' or (c == 'IRBinOp' and ins.op in ('/', '%')):
        return 'DIV'
    if c in _CTL:
        return 'CTL'
    return 'ALU'


class SchedPolicy:
    """Which R2.4 quality features are active. R23 reproduces the R2.3 scheduler
    exactly (unit-latency height, index-only tie-break); R24 enables all three."""
    __slots__ = ('latency', 'pressure', 'bundle', 'name')

    def __init__(self, latency, pressure, bundle, name):
        self.latency = latency
        self.pressure = pressure
        self.bundle = bundle
        self.name = name


SchedPolicy.R23 = SchedPolicy(False, False, False, 'R2.3')
SchedPolicy.R24 = SchedPolicy(True, True, True, 'R2.4')


class ScheduleStats:
    def __init__(self):
        self.functions = 0
        self.functions_changed = 0
        self.blocks = 0
        self.blocks_reordered = 0
        self.instrs_reordered = 0
        self.verified = 0            # differential 'match'
        self.unverified = 0          # differential 'unsupported' (kept: legal by construction)
        self.rollbacks = 0           # differential 'mismatch' -> reverted
        self.structural_failures = 0  # internal verifier caught a bad order (should be 0)
        # ── R2.4 reusable scheduling statistics (aggregate over scheduled blocks)
        self.crit_path_total = 0     # sum of per-block latency-weighted critical paths
        self.ready_size_sum = 0      # sum of ready-list sizes across all pick steps
        self.ready_steps = 0         # number of pick steps (for average ready size)
        self.pressure_peak_sum = 0   # sum of per-block peak live-temp estimates
        self.movement_sum = 0        # sum of |new_pos - orig_pos| over moved instrs
        self.movement_max = 0        # largest single-instruction movement distance
        self.est_instrs = 0          # instructions covered by the bundle estimate
        self.est_bundles = 0         # estimated bundles (light model; not the real bundler)
        self.metriced_blocks = 0     # blocks that contributed metrics (for averages)

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items()}

    def _absorb(self, m):
        """Fold one block's BlockMetrics into the aggregate."""
        self.crit_path_total += m['crit_path']
        self.ready_size_sum += m['ready_sum']
        self.ready_steps += m['ready_steps']
        self.pressure_peak_sum += m['peak_live']
        self.movement_sum += m['move_sum']
        self.movement_max = max(self.movement_max, m['move_max'])
        self.est_instrs += m['est_instrs']
        self.est_bundles += m['est_bundles']
        self.metriced_blocks += 1

    _R24_FIELDS = ('crit_path_total', 'ready_size_sum', 'ready_steps',
                   'pressure_peak_sum', 'movement_sum', 'est_instrs',
                   'est_bundles', 'metriced_blocks')

    def merge_r24(self, other):
        """Fold another ScheduleStats' R2.4 aggregate (used to count only the
        functions actually committed)."""
        for f in self._R24_FIELDS:
            setattr(self, f, getattr(self, f) + getattr(other, f))
        self.movement_max = max(self.movement_max, other.movement_max)


# ── per-block list scheduling ─────────────────────────────────────────────────

def _block_constraints(graph, sched_set):
    """succ / indeg over the block's schedulable nodes, from the NON-carried
    intra-block dependence edges (both endpoints schedulable)."""
    succ = {n: set() for n in sched_set}
    indeg = {n: 0 for n in sched_set}
    for e in graph.edges:
        if e.carried:
            continue
        if e.src in sched_set and e.dst in sched_set and e.dst not in succ[e.src]:
            succ[e.src].add(e.dst)
            indeg[e.dst] += 1
    return succ, indeg


def _heights(sched_idxs, succ, instrs=None, latency=False):
    """Dependency height = longest weighted chain of dependents below n. Nodes
    processed in DECREASING index order (every successor has a higher index --
    edges run low->high -- so its height is known). With `latency` each node
    contributes its estimated latency (R2.4 critical path); otherwise unit weight
    reproduces the R2.3 height exactly (leaf 0, else 1+max)."""
    height = {}
    for n in sorted(sched_idxs, reverse=True):
        if latency:
            w = _latency(instrs[n])
            height[n] = w + max((height[s] for s in succ[n]), default=0)
        else:
            height[n] = max((height[s] + 1 for s in succ[n]), default=0)
    return height


def _list_schedule(sched_idxs, succ, indeg, height, *, policy, instrs,
                   live_out):
    """Dependence-legal list schedule with the R2.4 priority policy. Returns
    (order, block_metrics). The R2.3 policy uses key (height, -index); R2.4 adds
    a register-pressure and a bundle-fill tie-break BENEATH the critical-path
    height (so the critical path is never sacrificed). The final -index term
    keeps every choice deterministic under all policies."""
    indeg = dict(indeg)
    region = set(sched_idxs)

    # ── register-pressure model (estimate; reuses block liveness) ──────────────
    total_uses = {}
    defined = set()
    for k in sched_idxs:
        for u in src_names(instrs[k]):
            total_uses[u] = total_uses.get(u, 0) + 1
        for d in dest_names(instrs[k]):
            defined.add(d)
    remaining = dict(total_uses)
    live = {t for t in total_uses if t not in defined}   # live-in temps
    peak_live = len(live)

    def births(n):
        return sum(1 for d in dest_names(instrs[n])
                   if total_uses.get(d, 0) > 0 or d in live_out)

    def deaths_now(n):
        return sum(1 for u in src_names(instrs[n])
                   if remaining.get(u, 0) == 1 and u not in live_out)

    # ── bundle-fill model (estimate; reuses the bundler's lane caps) ───────────
    win = {'MEM': 0, 'DIV': 0, 'CTL': 0, 'ALU': 0, 'size': 0}
    est_bundles = [0]

    def fits(cls):
        return win['size'] < _BUNDLE_MAX and win[cls] < _CAP[cls]

    def place_in_window(cls):
        if win['size'] == 0:
            est_bundles[0] += 1
        elif not fits(cls):
            for k in ('MEM', 'DIV', 'CTL', 'ALU'):
                win[k] = 0
            win['size'] = 0
            est_bundles[0] += 1
        win[cls] += 1
        win['size'] += 1

    def key(n):
        if not (policy.pressure or policy.bundle):
            return (height[n], -n)                        # R2.3 behaviour
        press = (deaths_now(n) - births(n)) if policy.pressure else 0
        bpref = (1 if (policy.bundle and fits(_iclass(instrs[n]))) else 0)
        return (height[n], press, bpref, -n)

    ready = [n for n in sched_idxs if indeg[n] == 0]
    order = []
    ready_sum = 0
    steps = 0
    while ready:
        ready_sum += len(ready)
        steps += 1
        pick = max(ready, key=key)
        ready.remove(pick)
        order.append(pick)
        # update pressure state
        for u in src_names(instrs[pick]):
            if u in remaining:
                remaining[u] -= 1
                if remaining[u] == 0 and u not in live_out:
                    live.discard(u)
        for d in dest_names(instrs[pick]):
            if total_uses.get(d, 0) > 0 or d in live_out:
                live.add(d)
        if len(live) > peak_live:
            peak_live = len(live)
        place_in_window(_iclass(instrs[pick]))
        for s in succ[pick]:
            indeg[s] -= 1
            if indeg[s] == 0:
                ready.append(s)

    crit = max(height.values(), default=0)
    metrics = {'crit_path': crit, 'ready_sum': ready_sum, 'ready_steps': steps,
               'peak_live': peak_live, 'est_instrs': len(sched_idxs),
               'est_bundles': est_bundles[0], 'move_sum': 0, 'move_max': 0}
    return order, metrics


def _schedule_block(graph, b, policy, live_out):
    """(new_index_order, block_metrics) for one CFG block; (None, None) if nothing
    is schedulable."""
    idxs = list(range(b.lo, b.hi + 1))
    if len(idxs) <= 1:
        return None, None
    # pin leading markers (labels / func-begin) and trailing control/markers
    front, tail = 0, len(idxs)
    while front < tail and type(graph.instrs[idxs[front]]).__name__ in _LEADER_PIN:
        front += 1
    while tail > front and type(graph.instrs[idxs[tail - 1]]).__name__ in _TRAILER_PIN:
        tail -= 1
    sched_idxs = idxs[front:tail]
    if len(sched_idxs) <= 1:
        return None, None
    sched_set = set(sched_idxs)
    succ, indeg = _block_constraints(graph, sched_set)
    height = _heights(sched_idxs, succ, graph.instrs, latency=policy.latency)
    new_mid, metrics = _list_schedule(sched_idxs, succ, indeg, height,
                                      policy=policy, instrs=graph.instrs,
                                      live_out=live_out)
    # movement distance vs original order (within the schedulable window)
    orig_pos = {k: i for i, k in enumerate(sched_idxs)}
    mv_sum, mv_max = 0, 0
    for new_i, k in enumerate(new_mid):
        d = abs(new_i - orig_pos[k])
        mv_sum += d
        mv_max = max(mv_max, d)
    metrics['move_sum'] = mv_sum
    metrics['move_max'] = mv_max
    return idxs[:front] + new_mid + idxs[tail:], metrics


def _verify_order(graph, positions, sched_set):
    """Every non-carried intra-block edge must keep src before dst in the new
    positions. Returns True if the order is a legal topological order."""
    for e in graph.edges:
        if e.carried:
            continue
        if e.src in sched_set and e.dst in sched_set:
            if positions[e.src] >= positions[e.dst]:
                return False
    return True


# ── per-function scheduling ───────────────────────────────────────────────────

def schedule_function_order(graph, policy=SchedPolicy.R24, metrics=None):
    """Compute the scheduled index permutation for one function slice (does not
    build IR). Returns (new_order, blocks_reordered, structural_ok). If `metrics`
    (a ScheduleStats) is given, per-block scheduling statistics are folded in."""
    live = compute_liveness(graph.cfg)
    new_order = []
    blocks_reordered = 0
    all_sched_set = set()
    positions = {}
    for b in graph.cfg.blocks:
        blk, bm = _schedule_block(graph, b, policy, live.live_out(b.id))
        original = list(range(b.lo, b.hi + 1))
        if blk is None:
            new_order.extend(original)
        else:
            if blk != original:
                blocks_reordered += 1
            new_order.extend(blk)
            all_sched_set.update(k for k in original)
            if metrics is not None:
                metrics._absorb(bm)
    for pos, k in enumerate(new_order):
        positions[k] = pos
    structural_ok = _verify_order(graph, positions, all_sched_set)
    return new_order, blocks_reordered, structural_ok


# ── module driver ─────────────────────────────────────────────────────────────

def schedule_module(instrs, disambiguate=True, policy=SchedPolicy.R24):
    """Reorder instructions within basic blocks across the whole module.

    Returns (new_instrs, stats). Each function is scheduled independently and
    kept only if the internal topological verifier passes AND the differential
    oracle does not report a behaviour mismatch (else that function is rolled
    back to its original order). `disambiguate` selects the R2.2 memory-precise
    graph (default) vs the R2.1 conservative graph; `policy` selects the R2.4
    quality features (default) or SchedPolicy.R23 for the R2.3 scheduler."""
    work = list(instrs)
    stats = ScheduleStats()

    by_slice = _function_descs(instrs) if disambiguate else {}

    for (lo, hi) in func_slices(instrs):
        stats.functions += 1
        if disambiguate:
            disamb = MemoryDisambiguator(instrs, lo, hi, by_slice.get((lo, hi), []))
            graph = DependenceGraph(instrs, lo, hi, disambiguator=disamb)
        else:
            graph = DependenceGraph(instrs, lo, hi)
        stats.blocks += len(graph.cfg.blocks)

        fmetrics = ScheduleStats()                      # per-function (kept only on commit)
        new_order, blocks_reordered, structural_ok = schedule_function_order(
            graph, policy, metrics=fmetrics)

        original = list(range(lo, hi + 1))
        if new_order == original:
            continue                                    # nothing moved
        if not structural_ok:
            stats.structural_failures += 1
            continue                                    # keep original (safety)

        candidate = list(work)
        candidate[lo:hi + 1] = [instrs[k] for k in new_order]

        verdict, _detail = ir_interp.differential(instrs, candidate, lo, hi)
        if verdict == 'mismatch':
            stats.rollbacks += 1
            continue                                    # revert this function
        # 'match' or 'unsupported' -> accept (schedule is legal by construction)
        if verdict == 'match':
            stats.verified += 1
        else:
            stats.unverified += 1
        work[lo:hi + 1] = [instrs[k] for k in new_order]
        stats.functions_changed += 1
        stats.blocks_reordered += blocks_reordered
        stats.instrs_reordered += sum(1 for a, k in zip(original, new_order) if a != k)
        stats.merge_r24(fmetrics)                       # count only committed functions

    return work, stats


def schedule_function(instrs, lo, hi, disambiguate=True, policy=SchedPolicy.R24):
    """Schedule a single function slice in place on a copy; returns (new_instrs,
    changed, verdict). Convenience for tests."""
    if disambiguate:
        descs = _function_descs(instrs).get((lo, hi), [])
        disamb = MemoryDisambiguator(instrs, lo, hi, descs)
        graph = DependenceGraph(instrs, lo, hi, disambiguator=disamb)
    else:
        graph = DependenceGraph(instrs, lo, hi)
    new_order, _br, structural_ok = schedule_function_order(graph, policy)
    original = list(range(lo, hi + 1))
    if new_order == original or not structural_ok:
        return list(instrs), False, 'unchanged' if new_order == original else 'structural-fail'
    candidate = list(instrs)
    candidate[lo:hi + 1] = [instrs[k] for k in new_order]
    verdict, _d = ir_interp.differential(instrs, candidate, lo, hi)
    if verdict == 'mismatch':
        return list(instrs), False, 'mismatch'
    return candidate, True, verdict

"""
oracle_ilp.py -- Oracle ILP Bound Analyzer (Milestone R3.0).

ANALYSIS ONLY. Computes, for every innermost loop, the theoretical
instruction-level parallelism the architecture could reach and quantifies
exactly what prevents the current compiler from reaching it. It mutates NO IR,
changes no scheduling / bundling / register allocation / codegen decision, and
affects correctness in no way -- it only reports statistics.

It is built entirely on frozen infrastructure and DUPLICATES no analysis:

  * DependenceGraph (R2.1) + MemoryDisambiguator (R2.2)  -- the exact typed
    dependence DAG (RAW/WAR/WAW/mem/recurrence edges), reused for the ready-set
    simulation (Phase 1, 3).
  * analysis_profile (M3 / the M11 statistics framework), which itself reuses
    parallelism_profile's `_critical_path`, `_rec_mii`, `_res_mii_detail`,
    `_reg_pressure` -- the critical path / RecMII / ResMII / MII / resource-term
    breakdown / recurrence-membership / register pressure (Phase 2).
  * schedule.py's latency + resource model (`_latency`, `_iclass`, `_CAP`,
    `_BUNDLE_MAX`) and modulo.py's `_edge_latency` -- the bundle model (Phase 3-4).

The only genuinely new code is the ready-set list-scheduling *simulation* (a
measurement, not a transform) and the limiter/opportunity classification.

--------------------------------------------------------------------------------
THREE IPB NUMBERS (the analytical core)

  theoretical_ipb = min(N / MII, 8)
      The maximum ACHIEVABLE IPB on this architecture: perfect software
      pipelining, infinite registers, perfect allocation, but the REAL hardware
      caps (8 issue / 4 mem / 1 div / 1 ctl) and the REAL recurrence latency.
      MII = max(RecMII, ResMII) is the M11-framework value. This is the ceiling
      no scheduler can beat -- the answer to "what is the max IPB?".

  local_ideal_ipb = N / bundles(ideal single-iteration list schedule)
      An IDEAL LOCAL scheduler with INFINITE registers (true-dependence DAG only,
      anti/output deps renamed away), real resource caps, reordering freely --
      but WITHOUT overlapping iterations. Diagnostic: high local_ideal with low
      theoretical means a single iteration is parallel but the loop-carried
      recurrence blocks overlap (recurrence-bound); low local_ideal means even one
      iteration is a serial dependence chain (dependency-bound).

  achieved_ipb = N / bundles(in-order greedy pack, ALL deps incl. anti/output)
      Models the CURRENT compiler: a local, in-order, greedy bundler over the
      memory-backed form (so WAR/WAW hazards from slot reuse are REAL). The gap
      to local_ideal is the local-scheduler + register-renaming opportunity; the
      gap to theoretical is the pipelining + recurrence-shortening opportunity.

utilization = achieved_ipb / theoretical_ipb.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir_utils import func_slices                                     # noqa: E402
from .discovery import discover_function                            # noqa: E402
from .analysis_iv import annotate_induction_vars, TripCount          # noqa: E402
from .analysis_mem import annotate_memory_effects                    # noqa: E402
from .analysis_profile import analyze_profile                        # noqa: E402
from .depgraph import (DependenceGraph, RAW, MEM_RAW, CONTROL)        # noqa: E402
from .depgraph_disambig import MemoryDisambiguator                   # noqa: E402
from .modulo import _edge_latency, _CONTROL_CLS                       # noqa: E402
from .schedule import _latency, _iclass, _CAP, _BUNDLE_MAX           # noqa: E402

_TRUE_KINDS = frozenset({RAW, MEM_RAW})           # flow deps (survive renaming)


# ── per-loop result ────────────────────────────────────────────────────────────

class LoopILP:
    """The oracle's full ILP report for one innermost loop. Pure data."""
    __slots__ = (
        'func', 'header', 'label', 'trip',
        # Phase 1-2
        'n_instr', 'n_edges', 'n_recur_edges', 'crit_path', 'crit_path_true',
        'avg_depth', 'max_depth', 'longest_recurrence',
        'rec_mii', 'res_mii', 'mii', 'mem_recurrence', 'rec_nodes',
        'mem_term', 'width_term', 'div_term', 'n_mem_ops', 'n_div_ops',
        'reg_pressure',
        # Phase 3
        'avg_ready', 'max_ready', 'ready_hist',
        # Phase 4
        'theoretical_ipb', 'local_ideal_ipb', 'achieved_ipb', 'utilization',
        'pipelining_gap', 'scheduler_gap', 'total_gap',
        # Phase 5
        'limiter', 'limiter_rank',
        # Phase 7
        'opportunities', 'top_opportunity',
        # internal heuristic flags (analysis only)
        '_control_heavy', '_assoc_recur', '_vectorizable', '_alias_slack',
    )

    def __init__(self, func, header):
        self.func = func
        self.header = header
        self.label = None
        self.trip = None
        for a in self.__slots__:
            if not hasattr(self, a):
                try:
                    setattr(self, a, 0)
                except AttributeError:
                    pass
        self.ready_hist = {}
        self.limiter_rank = []
        self.opportunities = []
        self.top_opportunity = None
        self.limiter = 'unknown'
        self.mem_recurrence = False

    def __repr__(self):
        return (f"LoopILP({self.func}@B{self.header} '{self.label}' N={self.n_instr} "
                f"theo={self.theoretical_ipb:.2f} ach={self.achieved_ipb:.2f} "
                f"util={self.utilization:.0%} limiter={self.limiter} "
                f"opp={self.top_opportunity})")


# ── body ops + typed edges (reuse R2.1) ────────────────────────────────────────

def _body_ops(desc, graph):
    """The data operations of the loop body (control excluded), in program order."""
    idxs = []
    for b in sorted(desc.body_blocks):
        blk = graph.cfg.blocks[b]
        idxs.extend(range(blk.lo, blk.hi + 1))
    idxs.sort()
    return [k for k in idxs
            if type(graph.instrs[k]).__name__ not in _CONTROL_CLS]


def _edges(graph, opset):
    """Intra (loop-independent) and carried (recurrence) edges among body ops,
    annotated (src, dst, latency, kind). Straight from the R2.1 graph."""
    intra, carried = [], []
    for e in graph.edges:
        if e.src not in opset or e.dst not in opset or e.kind == CONTROL:
            continue
        rec = (e.src, e.dst, _edge_latency(graph, e), e.kind)
        (carried if e.carried else intra).append(rec)
    return intra, carried


# ── critical-path depth over a DAG ──────────────────────────────────────────────

def _depths(ops, intra):
    """Longest-path depth (unit hops) of every node from a DAG root, and heights
    (latency-weighted, to a sink) used as the list-scheduler priority."""
    succ = {o: [] for o in ops}
    indeg = {o: 0 for o in ops}
    for (u, v, _l, _k) in intra:
        succ[u].append(v)
        indeg[v] += 1
    # topological order (Kahn)
    order, q = [], [o for o in ops if indeg[o] == 0]
    ind = dict(indeg)
    while q:
        u = q.pop()
        order.append(u)
        for v in succ[u]:
            ind[v] -= 1
            if ind[v] == 0:
                q.append(v)
    depth = {o: 0 for o in ops}
    for u in order:
        for v in succ[u]:
            if depth[u] + 1 > depth[v]:
                depth[v] = depth[u] + 1
    return depth


def _heights(ops, edges, lat_of):
    """Latency-weighted longest path from each node to a sink (list priority)."""
    succ = {o: [] for o in ops}
    indeg = {o: 0 for o in ops}
    for (u, v, l, _k) in edges:
        succ[u].append((v, l))
        indeg[v] += 1
    order, ind, q = [], dict(indeg), [o for o in ops if indeg[o] == 0]
    while q:
        u = q.pop()
        order.append(u)
        for (v, _l) in succ[u]:
            ind[v] -= 1
            if ind[v] == 0:
                q.append(v)
    h = {o: lat_of(o) for o in ops}
    for u in reversed(order):
        for (v, _l) in succ[u]:
            if h[v] + lat_of(u) > h[u]:
                h[u] = h[v] + lat_of(u)
    return h


# ── Phase 3: ideal ready-set list scheduler (measurement) ──────────────────────

def _fits(caps, cls):
    if caps['total'] >= _BUNDLE_MAX:
        return False
    if cls in ('MEM', 'DIV', 'CTL') and caps[cls] >= _CAP[cls]:
        return False
    return True


def _add(caps, cls):
    caps['total'] += 1
    if cls in ('MEM', 'DIV', 'CTL'):
        caps[cls] += 1


def _list_schedule(ops, edges, cls_of, height):
    """Bundle-count an ideal list schedule: dependent ops land in a later bundle
    (unit distance), the real bundle caps apply, ties break by height. Returns
    (n_bundles, ready_histogram, avg_ready, max_ready). Infinite registers are
    modelled by the caller passing only TRUE-dependence `edges`."""
    succ = {o: [] for o in ops}
    indeg = {o: 0 for o in ops}
    for (u, v, _l, _k) in edges:
        succ[u].append(v)
        indeg[v] += 1
    remaining = set(ops)
    done = set()
    n_bundles = 0
    hist = {}
    ready_counts = []
    ordered = sorted(ops, key=lambda o: -height.get(o, 0))
    while remaining:
        ready = [o for o in ordered if o in remaining and indeg[o] == 0]
        k = len(ready)
        ready_counts.append(k)
        bucket = min(k, 8)
        hist[bucket] = hist.get(bucket, 0) + 1
        caps = {'total': 0, 'MEM': 0, 'DIV': 0, 'CTL': 0}
        issued = []
        for o in ready:
            c = cls_of(o)
            if _fits(caps, c):
                _add(caps, c)
                issued.append(o)
        for o in issued:
            remaining.discard(o)
            done.add(o)
        # decrement successors' indegree once their pred is in an EARLIER bundle
        for o in issued:
            for v in succ[o]:
                indeg[v] -= 1
        n_bundles += 1
        if not issued:                     # safety: never stall forever
            break
    avg = sum(ready_counts) / len(ready_counts) if ready_counts else 0.0
    return n_bundles, hist, avg, (max(ready_counts) if ready_counts else 0)


def _inorder_pack(ops, all_intra, cls_of):
    """Model the CURRENT local bundler: walk ops in PROGRAM ORDER, keep filling
    one bundle until an op depends on (has an intra edge from) something already
    in the bundle or a resource cap is hit, then start a new bundle. `all_intra`
    includes anti/output deps (the memory-backed form's real hazards). Returns the
    bundle count."""
    dep_src = {o: set() for o in ops}
    for (u, v, _l, _k) in all_intra:
        dep_src[v].add(u)
    bundles = 0
    cur = set()
    caps = {'total': 0, 'MEM': 0, 'DIV': 0, 'CTL': 0}
    for o in ops:                          # program order
        c = cls_of(o)
        if cur and (dep_src[o] & cur or not _fits(caps, c)):
            bundles += 1
            cur = set()
            caps = {'total': 0, 'MEM': 0, 'DIV': 0, 'CTL': 0}
        cur.add(o)
        _add(caps, c)
    if cur:
        bundles += 1
    return bundles


# ── longest recurrence latency (from carried cycles) ───────────────────────────

def _longest_recurrence(ops, intra, carried):
    """Max latency-sum of any simple recurrence cycle (a carried edge closed by an
    intra path). Approximated by, for each carried edge (u->v), the intra longest
    path v..u plus the carried latency -- the cycle that RecMII bounds."""
    if not carried:
        return 0
    succ = {o: [] for o in ops}
    for (u, v, l, _k) in intra:
        succ[u].append((v, l))
    best = 0
    lat_node = {}
    for (u, v, l, _k) in intra + carried:
        lat_node.setdefault(u, 0)
        lat_node.setdefault(v, 0)

    def longest(src, dst):
        # latency-weighted longest path src->dst over intra edges (DAG)
        memo = {}

        def dfs(n):
            if n == dst:
                return 0
            if n in memo:
                return memo[n]
            best_p = None
            for (w, l) in succ.get(n, []):
                sub = dfs(w)
                if sub is not None:
                    best_p = l + sub if best_p is None else max(best_p, l + sub)
            memo[n] = best_p
            return best_p
        return dfs(src)

    for (u, v, l, _k) in carried:
        p = longest(v, u)                  # intra path back to the producer
        if p is not None:
            best = max(best, l + p)
        elif v == u:
            best = max(best, l)
    return best


# ── Phase 5: limiter classification ────────────────────────────────────────────

def _classify(r):
    """Rank the limiters of the THEORETICAL ceiling and pick the dominant one."""
    ranked = []
    # recurrence vs resource on MII
    if r.rec_mii > r.res_mii:
        kind = 'recurrence-bound-memory' if r.mem_recurrence else 'recurrence-bound-register'
        ranked.append((kind, r.rec_mii))
    if r.res_mii >= r.rec_mii and r.res_mii > 1:
        # which resource term binds ResMII
        if r.mem_term >= max(r.width_term, r.div_term) and r.mem_term == r.res_mii:
            ranked.append(('memory-bound', r.res_mii))
        elif r.div_term == r.res_mii and r.div_term > 0:
            ranked.append(('resource-bound-divide', r.res_mii))
        else:
            ranked.append(('resource-bound-width', r.res_mii))
    # single-iteration serial chain (dependency-bound) -- long true critical path
    if r.local_ideal_ipb and r.local_ideal_ipb < 2.0 and r.n_instr >= 3:
        ranked.append(('dependency-bound', r.crit_path_true))
    # control-bound: many-block body / control fraction handled by caller flag
    if getattr(r, '_control_heavy', False):
        ranked.append(('control-bound', r.n_instr))
    if not ranked:
        ranked = [('balanced', r.mii)]
    ranked.sort(key=lambda x: -x[1])
    # 'mixed' when the top two are close and different families
    if len(ranked) >= 2 and abs(ranked[0][1] - ranked[1][1]) <= 0.001 * max(1, ranked[0][1]) \
            and ranked[0][0].split('-')[0] != ranked[1][0].split('-')[0]:
        r.limiter = 'mixed'
    else:
        r.limiter = ranked[0][0]
    r.limiter_rank = ranked


# ── Phase 7: opportunity ranking ───────────────────────────────────────────────

_ASSOC = None  # detected per loop by caller (reduction op family)


def _opportunities(r):
    """Estimate which optimization would raise THIS loop's IPB the most. Gains are
    principled estimates (documented), ranked; ties keep a stable order."""
    opps = []
    theo = r.theoretical_ipb
    N = max(1, r.n_instr)

    # register promotion: a MEMORY recurrence becomes a register recurrence --
    # this shortens the recurrence (load-modify-store ~5 -> register ~3) AND
    # removes the accumulator's load+store from the memory-lane / width resource
    # terms. Both RecMII and ResMII can drop, so it fires on ANY memory recurrence.
    if r.mem_recurrence and r.rec_mii > 1:
        new_rec = max(1, round(r.rec_mii * 3.0 / 5.0))
        new_mem_term = -(-max(0, r.n_mem_ops - 2) // _CAP['MEM'])      # ceil
        new_width = -(-max(1, r.n_instr - 2) // _BUNDLE_MAX)
        new_res = max(new_mem_term, new_width, r.div_term)
        new_theo = min(max(1, r.n_instr - 2) / max(new_rec, new_res, 1), 8.0)
        opps.append(('register-promotion', new_theo - theo))

    # reassociation: an ASSOCIATIVE recurrence (sum/prod/min/max) split into k
    # parallel accumulators divides RecMII until ResMII binds.
    if getattr(r, '_assoc_recur', False) and r.rec_mii > r.res_mii:
        k = 4
        new_rec = max(1, -(-r.rec_mii // k))          # ceil(rec/k)
        new_theo = min(N / max(new_rec, r.res_mii), 8.0)
        opps.append(('reassociation', new_theo - theo))

    # software pipelining: overlap iterations to reach the sustainable ceiling.
    # worthwhile only when the ceiling is above what a single local pass gets.
    if theo - r.achieved_ipb > 0.25 and r.rec_mii <= r.res_mii * 2:
        opps.append(('software-pipelining', theo - r.achieved_ipb))

    # loop unrolling / unroll-and-jam: expose more independent work when a single
    # iteration under-fills the lanes but is NOT a deep serial chain.
    if r.avg_ready < 4 and r.local_ideal_ipb >= 2.0 and theo - r.local_ideal_ipb > 0.25:
        opps.append(('loop-unrolling', min(theo, r.local_ideal_ipb * 1.5) - r.local_ideal_ipb))

    # better alias analysis: conservative memory edges inflate the recurrence /
    # critical path (flagged by the caller when disambiguation moved MII).
    if getattr(r, '_alias_slack', 0) > 0:
        opps.append(('better-alias-analysis', r._alias_slack))

    # vectorization: uniform elementwise body, unit-stride, no blocking recurrence
    if getattr(r, '_vectorizable', False):
        opps.append(('vectorization', max(0.0, 8.0 - theo)))

    # register renaming / allocation: anti/output deps cost the current schedule
    if r.local_ideal_ipb - r.achieved_ipb > 0.25:
        opps.append(('register-renaming', r.local_ideal_ipb - r.achieved_ipb))

    opps = [(n, round(g, 3)) for (n, g) in opps if g > 0.05]
    opps.sort(key=lambda x: -x[1])
    if not opps:
        opps = [('no-useful-optimization', 0.0)]
    r.opportunities = opps
    r.top_opportunity = opps[0][0]


# ── associativity / vectorizability heuristics (analysis only) ─────────────────

def _assoc_and_vector_flags(desc, graph, ops):
    """Detect (a) an associative reduction recurrence (accumulator updated by
    + / * / min / max), and (b) a vectorizable elementwise body (IV-strided array
    accesses, no non-associative loop-carried dep). Heuristic, read-only."""
    instrs = graph.instrs
    assoc = False
    has_store = has_load = False
    nonassoc_carry = False
    for k in ops:
        ins = instrs[k]
        c = type(ins).__name__
        if c in ('IRLoad', 'IRGlobalLoad'):
            has_load = True
        if c in ('IRStore', 'IRGlobalStore'):
            has_store = True
        if c == 'IRBinOp' and ins.op in ('+', '*'):
            assoc = True
        if c == 'IRBinOp' and ins.op in ('-', '/', '%', '<<', '>>'):
            nonassoc_carry = nonassoc_carry or False   # not necessarily carried
    # associative *recurrence* only if there is a loop-carried accumulator; we
    # approximate with: an associative op present AND a memory/register recurrence.
    assoc_recur = assoc
    vectorizable = has_load and has_store and not nonassoc_carry \
        and bool(desc.iv_terms)
    return assoc_recur, vectorizable


# ── the analyzer ────────────────────────────────────────────────────────────────

def analyze_loop(desc, graph):
    """Full oracle ILP analysis for one innermost loop. Pure analysis."""
    r = LoopILP(getattr(graph.instrs[desc.func_slice[0]], 'name', '?'), desc.header)
    r.label = desc.label()
    r.trip = desc.trip_count.value if desc.trip_count \
        and desc.trip_count.kind == TripCount.KNOWN else None

    ops = _body_ops(desc, graph)
    r.n_instr = len(ops)
    if not ops:
        r.limiter = 'empty'
        r.top_opportunity = 'no-useful-optimization'
        return r
    opset = set(ops)
    intra, carried = _edges(graph, opset)
    r.n_edges = len(intra) + len(carried)
    r.n_recur_edges = len(carried)

    # Phase 2: metrics via the M11 statistics framework (reused, not reimplemented)
    analyze_profile(desc)
    r.crit_path = desc.crit_path_height
    r.crit_path_true = desc.crit_path_true
    r.rec_mii = desc.rec_mii
    r.res_mii = desc.res_mii
    r.mii = desc.mii
    r.reg_pressure = desc.reg_pressure_peak
    st = desc.profile_stats
    r.mem_recurrence = bool(st.get('mem_recurrence'))
    r.rec_nodes = st.get('rec_nodes', 0)
    r.mem_term = st.get('mem_lane_term', 0)
    r.width_term = st.get('width_term', 0)
    r.div_term = st.get('div_term', 0)
    r.n_mem_ops = st.get('n_mem_ops', 0)
    r.n_div_ops = sum(1 for k in ops if _iclass(graph.instrs[k]) == 'DIV')

    depth = _depths(ops, intra)
    r.max_depth = max(depth.values()) if depth else 0
    r.avg_depth = (sum(depth.values()) / len(depth)) if depth else 0.0
    r.longest_recurrence = _longest_recurrence(ops, intra, carried)

    # Phase 3: ideal ready-set (infinite registers -> TRUE deps only)
    cls_of = lambda o: _iclass(graph.instrs[o])
    lat_of = lambda o: _latency(graph.instrs[o])
    true_intra = [(u, v, l, k) for (u, v, l, k) in intra if k in _TRUE_KINDS]
    height_true = _heights(ops, true_intra, lat_of)
    li_bundles, hist, avg_rdy, max_rdy = _list_schedule(ops, true_intra, cls_of,
                                                        height_true)
    r.ready_hist = hist
    r.avg_ready = avg_rdy
    r.max_ready = max_rdy
    r.local_ideal_ipb = min(r.n_instr / li_bundles, 8.0) if li_bundles else 0.0

    # achieved: current in-order local pack over ALL deps (memory-backed hazards)
    ach_bundles = _inorder_pack(ops, intra, cls_of)
    r.achieved_ipb = min(r.n_instr / ach_bundles, 8.0) if ach_bundles else 0.0

    # Phase 4: theoretical ceiling = N / MII, capped at issue width
    r.theoretical_ipb = min(r.n_instr / r.mii, 8.0) if r.mii else 0.0
    r.utilization = (r.achieved_ipb / r.theoretical_ipb) if r.theoretical_ipb else 0.0
    r.pipelining_gap = max(0.0, r.theoretical_ipb - r.local_ideal_ipb)
    r.scheduler_gap = max(0.0, r.local_ideal_ipb - r.achieved_ipb)
    r.total_gap = max(0.0, r.theoretical_ipb - r.achieved_ipb)

    # control-heavy: INTERNAL conditional branches (beyond the loop's own header
    # guard + latch back-edge) -- i.e. if/else inside the body serialising it.
    n_condjump = 0
    for b in sorted(desc.body_blocks):
        blk = graph.cfg.blocks[b]
        for k in range(blk.lo, blk.hi + 1):
            if type(graph.instrs[k]).__name__ == 'IRCondJump':
                n_condjump += 1
    r._control_heavy = n_condjump > 1        # >1 => an internal branch exists

    # associativity / vectorizability heuristics for the opportunity report
    r._assoc_recur, r._vectorizable = _assoc_and_vector_flags(desc, graph, ops)
    r._alias_slack = 0.0

    _classify(r)
    _opportunities(r)
    return r


def analyze_function(instrs, lo, hi):
    """Oracle ILP analysis for every INNERMOST loop in one function slice."""
    descs = discover_function(instrs, lo, hi)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    disamb = MemoryDisambiguator(instrs, lo, hi, descs)
    graph = DependenceGraph(instrs, lo, hi, disambiguator=disamb)
    out = []
    for d in descs:
        if not d.is_innermost:
            continue
        out.append(analyze_loop(d, graph))
    return out


def analyze_module(instrs):
    """Oracle ILP analysis for every innermost loop in a module. Returns
    [LoopILP]. Pure analysis -- `instrs` is never mutated."""
    results = []
    for (lo, hi) in func_slices(instrs):
        results.extend(analyze_function(instrs, lo, hi))
    return results

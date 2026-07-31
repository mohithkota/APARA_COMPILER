"""
dependency_graph.py -- vector-IR dependence structure (Milestone R6.1).

ANALYSIS ONLY.  Builds no IR, moves no instruction.

--------------------------------------------------------------------------------
WHAT IT CONSUMES  (reused, nothing re-derived)
--------------------------------------------------------------------------------
    loopopt.depgraph.DependenceGraph      R2.1 typed dependence graph:
                                          RAW / WAR / WAW / MEM_RAW / MEM_WAR /
                                          MEM_WAW / CONTROL edges, each flagged
                                          `carried` for loop-carried (recurrence)
                                          edges.  Already vector-aware: it keys on
                                          ir_utils.dest_names/src_names, which
                                          cover IRVecArith / IRVecDot /
                                          IRVecDot128 / IRVecReduce / IRLoadWide /
                                          IRStoreWide.
    loopopt.depgraph_disambig             R2.2 memory precision (fewer false
                                          memory edges -> honest parallelism).
    loopopt.modulo._edge_latency          the frozen edge-latency function.
    loopopt.discovery / analysis_iv /     loop descriptors, induction variables
    analysis_mem                          and memory effects.
    vector_backend.latency                issue model + per-node latency.

The genuinely new code here is (a) the vector-loop VIEW of that graph -- the body
nodes of one vectorized loop with its intra-iteration DAG and its recurrence
edges separated -- and (b) the measurements the R6.1 report needs: latency-
weighted critical path, dependency depth, available parallelism, and the ready-
queue simulation.

--------------------------------------------------------------------------------
WHY BOTH "true" AND "all" DEPENDENCES ARE MEASURED
--------------------------------------------------------------------------------
Flow dependences (RAW / MEM_RAW) are real dataflow: no compiler can remove them
without changing the algorithm.  Anti/output dependences (WAR / WAW) exist only
because a name is reused, and disappear under renaming.  Reporting the critical
path under BOTH separates "this kernel is serial" from "this kernel was
serialised by the register allocator" -- which is exactly the question the
empty-slot classification has to answer at the bundle level.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir_utils import func_slices                                    # noqa: E402
from loopopt.discovery import discover_function                     # noqa: E402
from loopopt.analysis_iv import annotate_induction_vars, TripCount  # noqa: E402
from loopopt.analysis_mem import annotate_memory_effects            # noqa: E402
from loopopt.depgraph import (DependenceGraph, RAW, WAR, WAW,       # noqa: E402
                              MEM_RAW, MEM_WAR, MEM_WAW, CONTROL,
                              MEMORY_KINDS)
from loopopt.depgraph_disambig import MemoryDisambiguator           # noqa: E402
from loopopt.modulo import _edge_latency, _CONTROL_CLS              # noqa: E402

from . import latency as lat                                        # noqa: E402

TRUE_KINDS = frozenset({RAW, MEM_RAW})
ANTI_KINDS = frozenset({WAR, WAW, MEM_WAR, MEM_WAW})
EDGE_KINDS = (RAW, WAR, WAW, MEM_RAW, MEM_WAR, MEM_WAW, CONTROL)


# ── one vector loop's dependence view ─────────────────────────────────────────

class VectorLoopGraph:
    """The dependence graph of ONE loop body, with vector-aware statistics.

    Nodes are IR indices of the loop's data operations (control excluded, exactly
    as R3.0's oracle scopes them).  Edges are 4-tuples (src, dst, latency, kind);
    `intra` are loop-independent, `carried` are the recurrence edges closed by the
    back-edge."""

    def __init__(self, func, desc, graph):
        self.func = func
        self.desc = desc
        self.graph = graph
        self.label = desc.label()
        self.header = desc.header
        self.depth = desc.depth
        self.is_innermost = desc.is_innermost
        tc = desc.trip_count
        self.trip = tc.value if tc and tc.kind == TripCount.KNOWN else None

        self.ops = _body_ops(desc, graph)
        opset = set(self.ops)
        self.intra, self.carried = _split_edges(graph, opset)

        # ── composition ────────────────────────────────────────────────────────
        self.n_ops = len(self.ops)
        self.n_vector_ops = sum(1 for k in self.ops
                                if lat.ir_is_vector(graph.instrs[k]))
        self.n_mem_ops = sum(1 for k in self.ops
                             if lat.ir_class(graph.instrs[k]) == 'MEM')
        self.n_wide_mem = sum(1 for k in self.ops
                              if lat.ir_is_wide_mem(graph.instrs[k]))
        self.is_vector_loop = self.n_vector_ops > 0

        # ── edge census ────────────────────────────────────────────────────────
        self.edge_counts = {k: 0 for k in EDGE_KINDS}
        for (_u, _v, _l, k) in self.intra + self.carried:
            self.edge_counts[k] = self.edge_counts.get(k, 0) + 1
        self.n_edges = len(self.intra) + len(self.carried)
        self.n_carried = len(self.carried)
        self.n_true = sum(1 for e in self.intra if e[3] in TRUE_KINDS)
        self.n_anti = sum(1 for e in self.intra if e[3] in ANTI_KINDS)
        self.n_mem_edges = sum(1 for e in self.intra + self.carried
                               if e[3] in MEMORY_KINDS)

        # ── critical path (latency-weighted, MODEL) ────────────────────────────
        lat_of = lambda o: lat.ir_latency(graph.instrs[o])           # noqa: E731
        true_intra = [e for e in self.intra if e[3] in TRUE_KINDS]
        self.height_true = _heights(self.ops, true_intra, lat_of)
        self.height_all = _heights(self.ops, self.intra, lat_of)
        self.crit_path_true = max(self.height_true.values()) if self.ops else 0
        self.crit_path_all = max(self.height_all.values()) if self.ops else 0
        self.total_latency = sum(lat_of(o) for o in self.ops)

        # ── dependency depth (unit hops over true deps) ────────────────────────
        depth = _depths(self.ops, true_intra)
        self.dep_depth = max(depth.values()) if depth else 0
        self.avg_depth = (sum(depth.values()) / len(depth)) if depth else 0.0

        # ── available parallelism ──────────────────────────────────────────────
        # work / span, the classic ILP bound: how many operations could issue per
        # step if the machine were infinitely wide.
        self.available_parallelism = (self.total_latency / self.crit_path_true
                                      if self.crit_path_true else float(self.n_ops))

        # ── recurrence (loop-carried) latency ──────────────────────────────────
        self.recurrence_latency = _longest_recurrence(self.ops, self.intra,
                                                      self.carried)
        self.recurrence_nodes = sorted({e[0] for e in self.carried} |
                                       {e[1] for e in self.carried})

        # ── resource / recurrence lower bounds on a steady-state schedule ──────
        self.res_mii = _res_mii(self.ops, graph)
        self.rec_mii = max(1, self.recurrence_latency)
        self.mii = max(self.res_mii, self.rec_mii)

        # ── ready-queue simulation (ideal, infinite registers) ─────────────────
        sim = ready_queue_simulation(self.ops, true_intra,
                                     lambda o: lat.ir_class(graph.instrs[o]),
                                     self.height_true)
        self.ideal_steps = sim['steps']
        self.avg_ready = sim['avg_ready']
        self.max_ready = sim['max_ready']
        self.ready_hist = sim['hist']
        self.ideal_ipb = (self.n_ops / self.ideal_steps) if self.ideal_steps else 0.0

        # ── in-order model of the CURRENT bundler (all deps, program order) ────
        self.inorder_steps = inorder_pack(self.ops, self.intra,
                                          lambda o: lat.ir_class(graph.instrs[o]))
        self.inorder_ipb = (self.n_ops / self.inorder_steps) if self.inorder_steps else 0.0

        # ── IR-level value lifetimes (a renaming-pressure estimate) ────────────
        self.peak_live, self.avg_live = _ir_lifetimes(self.ops, graph)

    # -- presentation -----------------------------------------------------------
    def op_text(self, k):
        return f"{k}: {type(self.graph.instrs[k]).__name__}  {self.graph.instrs[k]}"

    def ascii_graph(self, max_nodes=32):
        """A compact, deterministic textual rendering of the body DAG."""
        out = []
        succ = {o: [] for o in self.ops}
        for (u, v, l, k) in self.intra:
            succ[u].append((v, l, k))
        for o in self.ops[:max_nodes]:
            ins = self.graph.instrs[o]
            tag = 'V' if lat.ir_is_vector(ins) else \
                  ('M' if lat.ir_class(ins) == 'MEM' else ' ')
            edges = ', '.join(f"{v}({k},{l})" for (v, l, k) in sorted(succ[o]))
            out.append(f"  [{tag}] n{o:<4} lat={lat.ir_latency(ins):<2} "
                       f"h={self.height_true.get(o, 0):<3} {type(ins).__name__:<14}"
                       f" -> {edges or '(sink)'}")
        for (u, v, l, k) in self.carried:
            out.append(f"  [C] n{u} =={k}/{l}==> n{v}   (LOOP-CARRIED)")
        if len(self.ops) > max_nodes:
            out.append(f"  ... {len(self.ops) - max_nodes} more nodes")
        return '\n'.join(out)

    def __repr__(self):
        return (f"VectorLoopGraph({self.func}@{self.label} N={self.n_ops} "
                f"vec={self.n_vector_ops} trip={self.trip} "
                f"crit={self.crit_path_true} par={self.available_parallelism:.2f} "
                f"ready={self.avg_ready:.2f})")


# ── graph construction over a module / function ───────────────────────────────

def analyze_function(instrs, lo, hi):
    """Every loop in one function slice, as VectorLoopGraph objects."""
    descs = discover_function(instrs, lo, hi)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    disamb = MemoryDisambiguator(instrs, lo, hi, descs)
    graph = DependenceGraph(instrs, lo, hi, disambiguator=disamb)
    fname = getattr(instrs[lo], 'name', '?')
    return [VectorLoopGraph(fname, d, graph) for d in descs]


def analyze_module(instrs):
    """Every loop in the module.  `instrs` is never mutated."""
    out = []
    for (lo, hi) in func_slices(instrs):
        try:
            out.extend(analyze_function(instrs, lo, hi))
        except Exception:                      # analysis must never break a build
            continue
    return out


def vector_loops(instrs):
    """Innermost loops that contain at least one real vector operation."""
    return [g for g in analyze_module(instrs) if g.is_vector_loop and g.is_innermost]


# ── shared graph primitives ───────────────────────────────────────────────────

def _body_ops(desc, graph):
    """Data operations of the loop body in program order (control excluded)."""
    idxs = []
    for b in sorted(desc.body_blocks):
        blk = graph.cfg.blocks[b]
        idxs.extend(range(blk.lo, blk.hi + 1))
    idxs.sort()
    return [k for k in idxs
            if type(graph.instrs[k]).__name__ not in _CONTROL_CLS]


def _split_edges(graph, opset):
    """(intra, carried) edge lists among `opset`, annotated with latency."""
    intra, carried = [], []
    for e in graph.edges:
        if e.src not in opset or e.dst not in opset or e.kind == CONTROL:
            continue
        rec = (e.src, e.dst, _edge_latency(graph, e), e.kind)
        (carried if e.carried else intra).append(rec)
    return intra, carried


def _topo(ops, edges):
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
    return order, succ


def _heights(ops, edges, lat_of):
    """Latency-weighted longest path from each node to a sink."""
    order, succ = _topo(ops, edges)
    h = {o: lat_of(o) for o in ops}
    for u in reversed(order):
        for (v, _l) in succ[u]:
            if h[v] + lat_of(u) > h[u]:
                h[u] = h[v] + lat_of(u)
    return h


def _depths(ops, edges):
    """Unit-hop longest path from a DAG root to each node."""
    order, succ = _topo(ops, edges)
    d = {o: 0 for o in ops}
    for u in order:
        for (v, _l) in succ[u]:
            if d[u] + 1 > d[v]:
                d[v] = d[u] + 1
    return d


def _longest_recurrence(ops, intra, carried):
    """Longest latency around any recurrence cycle (carried edge + the intra path
    that closes it).  Identical construction to R3.0's oracle."""
    if not carried:
        return 0
    succ = {o: [] for o in ops}
    for (u, v, l, _k) in intra:
        succ[u].append((v, l))
    best = 0

    def longest(src, dst):
        memo = {}

        def dfs(n):
            if n == dst:
                return 0
            if n in memo:
                return memo[n]
            b = None
            for (w, l) in succ.get(n, []):
                sub = dfs(w)
                if sub is not None:
                    b = l + sub if b is None else max(b, l + sub)
            memo[n] = b
            return b
        return dfs(src)

    for (u, v, l, _k) in carried:
        if v == u:
            best = max(best, l)
            continue
        p = longest(v, u)
        if p is not None:
            best = max(best, l + p)
    return best


def _res_mii(ops, graph):
    """Resource-bound lower bound on bundles per iteration (issue width + lanes)."""
    n = len(ops)
    n_mem = sum(1 for o in ops if lat.ir_class(graph.instrs[o]) == 'MEM')
    n_div = sum(1 for o in ops if lat.ir_class(graph.instrs[o]) == 'DIV')
    width = -(-n // lat.ISSUE_WIDTH)
    mem = -(-n_mem // lat.MEM_LANES)
    div = -(-n_div // lat.DIV_LANES)
    return max(1, width, mem, div)


def _ir_lifetimes(ops, graph):
    """Peak / average simultaneously-live IR values across the body, in program
    order.  An IR-level proxy for register pressure (the real allocation is
    codegen's; occupancy.py measures the register lifetimes that actually
    shipped)."""
    from ir_utils import dest_names, src_names
    last_use = {}
    for pos, o in enumerate(ops):
        for nm in src_names(graph.instrs[o]):
            last_use[nm] = pos
    live, peak, total = set(), 0, 0
    for pos, o in enumerate(ops):
        for nm in dest_names(graph.instrs[o]):
            if last_use.get(nm, -1) > pos:
                live.add(nm)
        for nm in list(live):
            if last_use.get(nm, -1) <= pos:
                live.discard(nm)
        peak = max(peak, len(live))
        total += len(live)
    return peak, (total / len(ops) if ops else 0.0)


# ── scheduling simulations (measurements, not transforms) ─────────────────────

def _fits(caps, cls):
    if caps['total'] >= lat.ISSUE_WIDTH:
        return False
    if cls == 'MEM' and caps['MEM'] >= lat.MEM_LANES:
        return False
    if cls == 'DIV' and caps['DIV'] >= lat.DIV_LANES:
        return False
    if cls == 'CTL' and caps['CTL'] >= lat.CTL_LANES:
        return False
    return True


def _add(caps, cls):
    caps['total'] += 1
    if cls in caps:
        caps[cls] += 1


def _fresh_caps():
    return {'total': 0, 'MEM': 0, 'DIV': 0, 'CTL': 0}


def ready_queue_simulation(ops, edges, cls_of, height):
    """Ideal ready-set list schedule over `edges` (pass TRUE deps only to model
    infinite registers).  Dependent operations land in a LATER step; the real
    lane caps apply; ties break by dependence height.

    Returns {'steps', 'avg_ready', 'max_ready', 'hist', 'issued_per_step'}.
    This is the "average ready instructions per scheduling step" the milestone
    asks for -- the direct measure of how much independent work exists."""
    succ = {o: [] for o in ops}
    indeg = {o: 0 for o in ops}
    for (u, v, _l, _k) in edges:
        succ[u].append(v)
        indeg[v] += 1
    remaining = set(ops)
    steps, hist, ready_counts, issued_per_step = 0, {}, [], []
    ordered = sorted(ops, key=lambda o: (-height.get(o, 0), o))
    while remaining:
        ready = [o for o in ordered if o in remaining and indeg[o] == 0]
        if not ready:
            break                                    # safety: never stall forever
        ready_counts.append(len(ready))
        bucket = min(len(ready), lat.ISSUE_WIDTH)
        hist[bucket] = hist.get(bucket, 0) + 1
        caps, issued = _fresh_caps(), []
        for o in ready:
            c = cls_of(o)
            if _fits(caps, c):
                _add(caps, c)
                issued.append(o)
        for o in issued:
            remaining.discard(o)
            for v in succ[o]:
                indeg[v] -= 1
        issued_per_step.append(len(issued))
        steps += 1
    return {'steps': steps,
            'avg_ready': (sum(ready_counts) / len(ready_counts)) if ready_counts else 0.0,
            'max_ready': max(ready_counts) if ready_counts else 0,
            'hist': hist,
            'issued_per_step': issued_per_step}


def inorder_pack(ops, edges, cls_of):
    """Model the CURRENT local bundler: walk `ops` in PROGRAM order and keep
    filling one bundle until a dependence on something already in it (or a lane
    cap) forces a new one.  `edges` should include anti/output deps -- those are
    real hazards for the in-order packer."""
    dep_src = {o: set() for o in ops}
    for (u, v, _l, _k) in edges:
        dep_src[v].add(u)
    bundles, cur, caps = 0, set(), _fresh_caps()
    for o in ops:
        c = cls_of(o)
        if cur and (dep_src[o] & cur or not _fits(caps, c)):
            bundles += 1
            cur, caps = set(), _fresh_caps()
        cur.add(o)
        _add(caps, c)
    if cur:
        bundles += 1
    return bundles

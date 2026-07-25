"""
depgraph.py -- Reusable IR Dependence Graph (Milestone R2.1, analysis only).

The standard dependency representation over IR instructions that every future
scheduling / restructuring pass consumes instead of rebuilding its own. It is
pure ANALYSIS: it never mutates the IR, never changes generated assembly, never
touches the bundler, and nothing in the production pipeline consumes it yet. It
is added, like every milestone before it, purely by ADDITION.

================================================================================
REUSED ANALYSES  (this module builds NO analysis of its own; it composes these)
================================================================================
    ir_utils.dest_names / src_names / jump_targets
                                     -- the shared def/use/branch primitives that
                                        DefUse, Liveness and MemEffects are built on
    analysis.DefUse                  -- def sites, use sites, single-definition map
                                        (register dependence endpoints; the
                                        IRLoadAddr slot map for memory keys)
    analysis.build_cfg / CFG         -- basic blocks + succ/pred edges; the
                                        instruction->block map and the block
                                        reachability the may-precede relation uses
    analysis.compute_dominators      -- dominance (loop-header attribution / clean
                                        integration; carried-edge sanity)
    analysis.build_loop_info         -- natural loops (bodies + headers): the ONLY
                                        source of loop-carried classification
    loopopt.analysis_mem._access_key -- the M2 alias-key CLASSIFIER (stack slot /
                                        global object / computed pointer), reused
                                        verbatim so memory keys match the loop
                                        framework exactly
    loopopt.analysis_mem.AliasSummary.may_alias
                                     -- the M2 conservative alias ORACLE (mirrors
                                        the bundler's `_mem_may_alias`): two keys
                                        may alias unless provably disjoint

Nothing above is duplicated. The only NEW logic here is edge construction and the
graph data structure / query surface.

================================================================================
DEPENDENCE MODEL
================================================================================
A graph is built over ONE function slice (temp names and block ids restart per
function -- see ir_utils). Nodes are IR instruction indices; edges are directed
"must be ordered" constraints from an earlier producer to a later consumer.

may-precede relation (the backbone).  For instructions i, j:
    * same block            -> i precedes j iff index(i) < index(j)
    * different blocks bi,bj -> i may precede j iff bj is reachable from bi in
                               the CFG (transitive closure over successor edges,
                               which already INCLUDE loop back-edges).
Two instructions on mutually-unreachable branches (neither block reaches the
other) never both execute along one path, so no ordering edge is needed -- this
is what keeps the graph from connecting exclusive if/else arms.

Register dependences (shared temp name n, at least one side writes):
    earlier E, later L ->  E def / L use  = RAW   (flow)
                           E use / L def  = WAR   (anti)
                           E def / L def  = WAW   (output)
                           E use / L use  = (none)

Memory dependences (shared alias key, at least one side writes; classifier +
oracle reused from M2). A call / indirect call is a conservative memory BARRIER:
it may read and write all escaped/global memory, modelled with key=None so the
M2 oracle makes it conflict with every other memory op.
    earlier E, later L ->  E write / L read  = MEM_RAW
                           E read  / L write = MEM_WAR
                           E write / L write = MEM_WAW

Control ordering (minimal, only where required for correctness): a block's
terminator (IRJump / IRCondJump / IRReturn / IRHalt) is pinned AFTER every other
instruction of its own block with a CONTROL edge, so the branch stays last. No
other control edges are represented -- data and memory edges already carry the
rest, and the frozen block-local bundler owns final placement.

Intra-iteration vs loop-carried.  For every conflicting pair the "forward"
constraint (E before L in a single iteration) is an INTRA edge E->L. When the
CFG additionally lets L's block reach E's block (they sit in a cycle) AND both
lie in a common natural loop, the value/order also recurs across the back edge:
a separate CARRIED (recurrence) edge L->E is added, tagged with that loop's
header. Recurrence edges are thus represented SEPARATELY from intra-iteration
edges (distinct `carried` flag + `loop_header`), never conflated.

INVARIANT enforced by validate(): a non-carried edge always runs low->high index
(src < dst); a carried edge always runs high->low index (src > dst).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import Const, Temp                                            # noqa: E402
from ir_utils import (dest_names, src_names, func_slices)             # noqa: E402
from analysis import (DefUse, build_cfg, compute_dominators,          # noqa: E402
                      build_loop_info)
from .analysis_mem import _access_key, AliasSummary                   # noqa: E402


# ── edge kinds ────────────────────────────────────────────────────────────────

RAW = 'RAW'          # register flow  : read-after-write
WAR = 'WAR'          # register anti  : write-after-read
WAW = 'WAW'          # register output: write-after-write
MEM_RAW = 'MEM_RAW'  # memory flow    : load-after-store
MEM_WAR = 'MEM_WAR'  # memory anti    : store-after-load
MEM_WAW = 'MEM_WAW'  # memory output  : store-after-store
CONTROL = 'CONTROL'  # ordering constraint (block terminator last)

REGISTER_KINDS = frozenset({RAW, WAR, WAW})
MEMORY_KINDS = frozenset({MEM_RAW, MEM_WAR, MEM_WAW})

_TERMINATORS = frozenset({'IRJump', 'IRCondJump', 'IRReturn', 'IRHalt'})


# ── data structures ───────────────────────────────────────────────────────────

class DepNode:
    """One IR instruction in the dependence graph."""
    __slots__ = ('index', 'instr', 'cls', 'block')

    def __init__(self, index, instr, cls, block):
        self.index = index          # IR instruction index (function-absolute)
        self.instr = instr          # the IR object
        self.cls = cls              # class name, e.g. 'IRBinOp'
        self.block = block          # owning CFG block id

    def __repr__(self):
        return f"N{self.index}[{self.cls}@B{self.block}]"


class DepEdge:
    """A directed dependence src -> dst (src must be ordered before dst).

    `proven` / `reason` are populated only when a memory disambiguator classified
    the edge (R2.2). By default (R2.1 behaviour) they are False / None and the
    edge repr is unchanged -- a memory edge with no disambiguator is simply a
    conservative edge whose precise status is unknown."""
    __slots__ = ('src', 'dst', 'kind', 'resource', 'carried', 'loop_header',
                 'proven', 'reason')

    def __init__(self, src, dst, kind, resource=None, carried=False,
                 loop_header=None, proven=False, reason=None):
        self.src = src              # producer instruction index
        self.dst = dst              # consumer instruction index
        self.kind = kind            # one of the *_KINDS / CONTROL
        self.resource = resource    # temp name (register) or alias key (memory)
        self.carried = carried      # True => loop-carried recurrence edge
        self.loop_header = loop_header  # header block id of the carrying loop
        self.proven = proven        # memory edge a disambiguator proved MUST-alias
        self.reason = reason        # disambiguation reason tag (memory edges only)

    def is_register(self):
        return self.kind in REGISTER_KINDS

    def is_memory(self):
        return self.kind in MEMORY_KINDS

    def __repr__(self):
        tag = f" carried@B{self.loop_header}" if self.carried else ""
        res = f" {self.resource}" if self.resource is not None else ""
        why = f" [{'proven' if self.proven else 'maybe'}:{self.reason}]" \
            if self.reason is not None else ""
        return f"{self.src}->{self.dst} {self.kind}{res}{tag}{why}"


# ── the graph ─────────────────────────────────────────────────────────────────

class DependenceGraph:
    """Dependence graph over one function slice instrs[lo..hi].

    Construct directly for a single function, or use build_function_graphs() /
    build_dependence_graph() for whole modules. Existing analyses are reused if
    passed in (cfg / dom / li / du), else built here. Treat the result as an
    immutable snapshot: rebuild after any IR mutation."""

    def __init__(self, instrs, lo=None, hi=None, *,
                 cfg=None, dom=None, li=None, du=None, disambiguator=None):
        if lo is None:
            lo = 0
        if hi is None:
            hi = len(instrs) - 1
        self.instrs = instrs
        self.lo = lo
        self.hi = hi

        # R2.2 memory disambiguation (optional; None => exact R2.1 behaviour).
        self.disambiguator = disambiguator
        self.eliminated_memory_edges = 0     # provably-disjoint pairs dropped
        self.eliminated = []                 # (src, dst, kind, carried, reason)

        # ---- reuse (or build) the shared analyses -----------------------------
        self.cfg = cfg if cfg is not None else build_cfg(instrs, lo, hi)
        self.dom = dom if dom is not None else compute_dominators(self.cfg)
        self.loop_info = li if li is not None else build_loop_info(self.cfg, self.dom)
        self.defuse = du if du is not None else DefUse(instrs, lo, hi)

        # ---- nodes + index->block map -----------------------------------------
        self._block_of = {}
        for b in self.cfg.blocks:
            for k in range(b.lo, b.hi + 1):
                self._block_of[k] = b.id
        self.nodes = {}
        for k in range(lo, hi + 1):
            self.nodes[k] = DepNode(k, instrs[k], type(instrs[k]).__name__,
                                    self._block_of.get(k))

        # ---- derived reuse ----------------------------------------------------
        self._reach = self._block_reachability()      # bid -> set of reachable bids
        self._loop_of_block = self._innermost_loop_map()

        # ---- edges ------------------------------------------------------------
        self.edges = []
        self._succ = {k: [] for k in range(lo, hi + 1)}
        self._pred = {k: [] for k in range(lo, hi + 1)}
        self._build_register_edges()
        self._build_memory_edges()
        self._build_control_edges()

    # ── reachability + loop maps (derived from CFG / LoopInfo) ─────────────────

    def _block_reachability(self):
        """bid -> set of blocks reachable via >=1 CFG edge (back-edges included).
        A block appears in its own set only when it lies on a cycle."""
        reach = {}
        for b in self.cfg.blocks:
            seen = set()
            stack = list(b.succs)
            while stack:
                s = stack.pop()
                if s not in seen:
                    seen.add(s)
                    stack.extend(self.cfg.blocks[s].succs)
            reach[b.id] = seen
        return reach

    def _innermost_loop_map(self):
        """block id -> header of the innermost natural loop containing it (or
        None). Innermost = smallest body among loops whose body holds the block."""
        best = {}
        for loop in self.loop_info.loops:
            size = len(loop.body)
            for blk in loop.body:
                cur = best.get(blk)
                if cur is None or size < cur[1]:
                    best[blk] = (loop.header, size)
        return {blk: hv[0] for blk, hv in best.items()}

    def _may_precede(self, i, j):
        """True if instruction i may execute before instruction j on some path.

        Same block: i precedes j within one iteration iff i<j; the reverse
        (i>=j) is possible ONLY by going around a back edge, i.e. when the block
        lies on a cycle (is reachable from itself). Missing that case would drop
        every loop-carried dependence internal to a single-block loop body (e.g.
        an accumulator's store feeding the next iteration's load)."""
        bi, bj = self._block_of.get(i), self._block_of.get(j)
        if bi is None or bj is None:
            return i < j
        if bi == bj:
            if i < j:
                return True
            return bi in self._reach[bi]          # only around a back edge
        return bj in self._reach[bi]

    def _common_loop_header(self, i, j):
        """Header of the innermost natural loop whose body contains BOTH i and j,
        or None. Reuses LoopInfo bodies; dominance sanity via self.dom."""
        bi, bj = self._block_of.get(i), self._block_of.get(j)
        best = None
        best_sz = None
        for loop in self.loop_info.loops:
            if bi in loop.body and bj in loop.body:
                sz = len(loop.body)
                if best_sz is None or sz < best_sz:
                    best, best_sz = loop.header, sz
        return best

    # ── edge insertion ────────────────────────────────────────────────────────

    def _add_edge(self, src, dst, kind, resource=None, carried=False,
                  loop_header=None, proven=False, reason=None):
        e = DepEdge(src, dst, kind, resource, carried, loop_header, proven, reason)
        self.edges.append(e)
        self._succ[src].append(e)
        self._pred[dst].append(e)

    def _emit_pair(self, i, j, roles, kind_map, resource, mem):
        """Given a conflicting UNORDERED pair with i < j (program order), emit the
        intra edge (E=i -> L=j) and, when the CFG puts them in a cycle inside a
        common loop, the carried recurrence edge (E=j -> L=i).

        roles: (i_writes, i_reads, j_writes, j_reads) booleans.
        kind_map(e_writes, e_reads, l_writes, l_reads) -> kind or None.
        mem: True for a memory pair (used only for edge naming clarity)."""
        iw, ir_, jw, jr = roles
        # intra: i is the earlier instruction
        if self._may_precede(i, j):
            k = kind_map(iw, ir_, jw, jr)
            if k is not None:
                self._add_edge(i, j, k, resource, carried=False)
        # carried: j (later in program order) feeds i in the next iteration
        if self._may_precede(j, i):
            hdr = self._common_loop_header(i, j)
            if hdr is None:
                # cycle without a shared natural-loop header (irreducible flow):
                # still record the recurrence so no cross-iteration dependence is
                # dropped -- attribute it to the nearest enclosing loop or, absent
                # one, the block itself (a non-None sentinel keeping validate()
                # happy). Reducible C never reaches this branch.
                hdr = (self._loop_of_block.get(self._block_of.get(i))
                       or self._loop_of_block.get(self._block_of.get(j))
                       or self._block_of.get(i))
            k = kind_map(jw, jr, iw, ir_)
            if k is not None:
                self._add_edge(j, i, k, resource, carried=True,
                               loop_header=hdr)

    # ── register dependences ──────────────────────────────────────────────────

    @staticmethod
    def _reg_kind(e_writes, e_reads, l_writes, l_reads):
        if e_writes and l_reads:
            return RAW
        if e_reads and l_writes:
            return WAR
        if e_writes and l_writes:
            return WAW
        return None

    def _build_register_edges(self):
        """RAW / WAR / WAW over shared temp names, using DefUse def/use sites."""
        # per-name event map: name -> {index: (writes, reads)}
        events = {}
        for k in range(self.lo, self.hi + 1):
            ins = self.instrs[k]
            for d in dest_names(ins):
                slot = events.setdefault(d, {})
                w, r = slot.get(k, (False, False))
                slot[k] = (True, r)
            for u in src_names(ins):
                slot = events.setdefault(u, {})
                w, r = slot.get(k, (False, False))
                slot[k] = (w, True)

        for name, ev in events.items():
            idxs = sorted(ev)
            if len(idxs) < 2:
                continue
            for a in range(len(idxs)):
                i = idxs[a]
                iw, ir_ = ev[i]
                for b in range(a + 1, len(idxs)):
                    j = idxs[b]
                    jw, jr = ev[j]
                    if not (iw or jw):
                        continue          # read/read: no dependence
                    self._emit_pair(i, j, (iw, ir_, jw, jr),
                                    self._reg_kind, name, mem=False)

    # ── memory dependences ────────────────────────────────────────────────────

    @staticmethod
    def _mem_kind(e_writes, e_reads, l_writes, l_reads):
        if e_writes and l_reads:
            return MEM_RAW
        if e_reads and l_writes:
            return MEM_WAR
        if e_writes and l_writes:
            return MEM_WAW
        return None

    def _memory_accesses(self):
        """[(index, key, writes, reads)] for every memory op / barrier in the
        slice, using the M2 classifier. A call is a barrier (key=None, r+w)."""
        # slot-address temps: single-def IRLoadAddr -> fp_offset (as M2 does).
        addr_off = {}
        for name, k in self.defuse.single_defs().items():
            if type(self.instrs[k]).__name__ == 'IRLoadAddr':
                addr_off[name] = self.instrs[k].fp_offset

        out = []
        for k in range(self.lo, self.hi + 1):
            ins = self.instrs[k]
            c = type(ins).__name__
            if c in ('IRCall', 'IRIndirectCall'):
                out.append((k, None, True, True))     # conservative barrier
                continue
            acc = _access_key(self.instrs, ins, addr_off)
            if acc is None:
                continue
            _space, key, is_w = acc
            out.append((k, key, is_w, not is_w))
        return out

    def _build_memory_edges(self):
        """Memory RAW/WAR/WAW over aliasing accesses. With no disambiguator this
        is byte-identical to R2.1 (same candidate pairs, same intra/carried
        emission, same order). A disambiguator (R2.2) may additionally prove a
        specific intra or carried pair disjoint -- that edge is dropped and
        counted -- or prove it a MUST-alias, tagging the kept edge `proven`."""
        accs = self._memory_accesses()
        for a in range(len(accs)):
            i, ki, iw, ir_ = accs[a]
            for b in range(a + 1, len(accs)):
                j, kj, jw, jr = accs[b]
                if not (iw or jw):
                    continue              # read/read: no memory dependence
                if not AliasSummary.may_alias(ki, kj):
                    continue              # provably disjoint (base oracle)
                resource = ki if ki == kj else None
                # intra-iteration: i (earlier) -> j
                if self._may_precede(i, j):
                    kind = self._mem_kind(iw, ir_, jw, jr)
                    if kind is not None:
                        self._emit_mem(i, j, i, j, kind, resource,
                                       carried=False, loop_header=None)
                # loop-carried: j (later) feeds i next iteration
                if self._may_precede(j, i):
                    hdr = self._common_loop_header(i, j)
                    if hdr is None:
                        hdr = (self._loop_of_block.get(self._block_of.get(i))
                               or self._loop_of_block.get(self._block_of.get(j))
                               or self._block_of.get(i))
                    kind = self._mem_kind(jw, jr, iw, ir_)
                    if kind is not None:
                        self._emit_mem(j, i, i, j, kind, resource,
                                       carried=True, loop_header=hdr)

    def _emit_mem(self, src, dst, lo_idx, hi_idx, kind, resource, carried,
                  loop_header):
        """Add one memory edge src->dst, consulting the disambiguator (if any) on
        the unordered pair (lo_idx, hi_idx). Drops provably-disjoint edges;
        tags kept edges proven/reason."""
        proven, reason = False, None
        if self.disambiguator is not None:
            v = self.disambiguator.classify(lo_idx, hi_idx, carried)
            if v.disjoint:
                self.eliminated_memory_edges += 1
                self.eliminated.append((src, dst, kind, carried, v.reason))
                return
            proven, reason = v.proven, v.reason
        self._add_edge(src, dst, kind, resource, carried, loop_header,
                       proven=proven, reason=reason)

    # ── control ordering (minimal) ────────────────────────────────────────────

    def _build_control_edges(self):
        """Pin each block's terminator after the rest of its block."""
        for b in self.cfg.blocks:
            if b.hi < b.lo:
                continue
            if type(self.instrs[b.hi]).__name__ in _TERMINATORS:
                t = b.hi
                for k in range(b.lo, t):
                    self._add_edge(k, t, CONTROL)

    # ── query surface ─────────────────────────────────────────────────────────

    def node(self, index):
        return self.nodes.get(index)

    def indices(self):
        """All node indices in program order."""
        return list(range(self.lo, self.hi + 1))

    def all_edges(self, kind=None, carried=None):
        """Every edge, optionally filtered by kind and/or carried flag."""
        for e in self.edges:
            if kind is not None and e.kind != kind:
                continue
            if carried is not None and e.carried != carried:
                continue
            yield e

    def out_edges(self, index):
        return list(self._succ.get(index, ()))

    def in_edges(self, index):
        return list(self._pred.get(index, ()))

    def successors(self, index, kind=None, carried=None):
        """Successor indices of `index` (optionally filtered)."""
        out = []
        for e in self._succ.get(index, ()):
            if kind is not None and e.kind != kind:
                continue
            if carried is not None and e.carried != carried:
                continue
            out.append(e.dst)
        return out

    def predecessors(self, index, kind=None, carried=None):
        out = []
        for e in self._pred.get(index, ()):
            if kind is not None and e.kind != kind:
                continue
            if carried is not None and e.carried != carried:
                continue
            out.append(e.src)
        return out

    def num_edges(self):
        return len(self.edges)

    def num_nodes(self):
        return len(self.nodes)

    def carried_edges(self):
        return [e for e in self.edges if e.carried]

    def memory_edges(self, proven=None):
        """Memory edges, optionally filtered by proven status (True = MUST-alias
        the disambiguator proved; False = conservative)."""
        out = [e for e in self.edges if e.is_memory()]
        if proven is not None:
            out = [e for e in out if e.proven == proven]
        return out

    def proven_memory_edges(self):
        """Memory edges a disambiguator proved to be genuine (MUST-alias)."""
        return [e for e in self.edges if e.is_memory() and e.proven]

    def conservative_memory_edges(self):
        """Memory edges kept conservatively (alias possible but not proven)."""
        return [e for e in self.edges if e.is_memory() and not e.proven]

    def register_edges(self):
        return [e for e in self.edges if e.is_register()]

    # ── SCC detection (Tarjan) ────────────────────────────────────────────────

    def sccs(self):
        """Strongly connected components over ALL edges (register + memory +
        control + carried), as lists of indices. Tarjan's algorithm; components
        are returned in reverse-topological order. Trivial single-node SCCs are
        included -- filter with recurrences() for genuine cycles."""
        index_counter = [0]
        stack = []
        on_stack = set()
        indices = {}
        lowlink = {}
        result = []

        # iterative Tarjan to avoid recursion limits on large functions
        for start in range(self.lo, self.hi + 1):
            if start in indices:
                continue
            work = [(start, iter(self._succ_targets(start)))]
            indices[start] = lowlink[start] = index_counter[0]
            index_counter[0] += 1
            stack.append(start)
            on_stack.add(start)
            while work:
                v, it = work[-1]
                advanced = False
                for w in it:
                    if w not in indices:
                        indices[w] = lowlink[w] = index_counter[0]
                        index_counter[0] += 1
                        stack.append(w)
                        on_stack.add(w)
                        work.append((w, iter(self._succ_targets(w))))
                        advanced = True
                        break
                    elif w in on_stack:
                        lowlink[v] = min(lowlink[v], indices[w])
                if advanced:
                    continue
                # done with v
                if lowlink[v] == indices[v]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    result.append(comp)
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[v])
        return result

    def _succ_targets(self, index):
        # deduplicated successor indices (Tarjan only needs the target set)
        seen = []
        s = set()
        for e in self._succ.get(index, ()):
            if e.dst not in s:
                s.add(e.dst)
                seen.append(e.dst)
        return seen

    def recurrences(self):
        """Non-trivial SCCs: components with >1 node, or a single node carrying a
        self dependence. These are the loop recurrences future software-pipelining
        will reason about."""
        out = []
        for comp in self.sccs():
            if len(comp) > 1:
                out.append(comp)
            elif len(comp) == 1:
                v = comp[0]
                if any(e.dst == v for e in self._succ.get(v, ())):
                    out.append(comp)
        return out

    # ── topological order (acyclic region) ────────────────────────────────────

    def topo_order(self):
        """Topological order over the ACYCLIC subgraph (intra-iteration edges;
        carried recurrence edges are excluded because they intentionally close
        cycles). Kahn's algorithm; ties broken by program order. If a residual
        cycle exists among non-carried edges (e.g. irreducible control flow), the
        remaining nodes are appended in program order and is_acyclic() reports
        False."""
        import bisect
        indeg = {k: 0 for k in range(self.lo, self.hi + 1)}
        succ = {k: set() for k in range(self.lo, self.hi + 1)}
        for e in self.edges:
            if e.carried:
                continue
            if e.dst not in succ[e.src]:
                succ[e.src].add(e.dst)
                indeg[e.dst] += 1
        ready = sorted(k for k in indeg if indeg[k] == 0)
        order = []
        while ready:
            v = ready.pop(0)
            order.append(v)
            for w in sorted(succ[v]):
                indeg[w] -= 1
                if indeg[w] == 0:
                    bisect.insort(ready, w)
        if len(order) != self.num_nodes():
            placed = set(order)
            order.extend(k for k in range(self.lo, self.hi + 1) if k not in placed)
        return order

    def is_acyclic(self):
        """True iff the non-carried subgraph has no cycle (topo covers all)."""
        indeg = {k: 0 for k in range(self.lo, self.hi + 1)}
        succ = {k: [] for k in range(self.lo, self.hi + 1)}
        for e in self.edges:
            if e.carried:
                continue
            indeg[e.dst] += 1
            succ[e.src].append(e.dst)
        ready = [k for k in indeg if indeg[k] == 0]
        seen = 0
        while ready:
            v = ready.pop()
            seen += 1
            for w in succ[v]:
                indeg[w] -= 1
                if indeg[w] == 0:
                    ready.append(w)
        return seen == self.num_nodes()

    # ── validation ────────────────────────────────────────────────────────────

    def validate(self):
        """Structural self-consistency check. Returns a list of human-readable
        problems ([] == a well-formed graph). Observe-only."""
        problems = []
        n_lo, n_hi = self.lo, self.hi
        # 1. endpoints exist and are in range
        for e in self.edges:
            if e.src not in self.nodes:
                problems.append(f"edge src {e.src} not a node ({e})")
            if e.dst not in self.nodes:
                problems.append(f"edge dst {e.dst} not a node ({e})")
            if not (n_lo <= e.src <= n_hi and n_lo <= e.dst <= n_hi):
                problems.append(f"edge index out of slice range ({e})")
            if e.src == e.dst:
                problems.append(f"self-loop edge ({e})")
            # 2. direction invariant
            if e.carried:
                if e.src <= e.dst:
                    problems.append(f"carried edge not high->low ({e})")
                if e.loop_header is None:
                    problems.append(f"carried edge lacks loop header ({e})")
            else:
                if e.src >= e.dst:
                    problems.append(f"intra edge not low->high ({e})")
        # 3. succ/pred index consistency
        succ_count = sum(len(v) for v in self._succ.values())
        pred_count = sum(len(v) for v in self._pred.values())
        if succ_count != len(self.edges):
            problems.append(f"succ index holds {succ_count} != {len(self.edges)} edges")
        if pred_count != len(self.edges):
            problems.append(f"pred index holds {pred_count} != {len(self.edges)} edges")
        for e in self.edges:
            if e not in self._succ.get(e.src, ()):
                problems.append(f"edge missing from succ[{e.src}] ({e})")
            if e not in self._pred.get(e.dst, ()):
                problems.append(f"edge missing from pred[{e.dst}] ({e})")
        return problems

    # ── debugging / dumping ───────────────────────────────────────────────────

    def dump(self, show_instrs=True):
        """Human-readable dump of nodes and edges."""
        lines = [f"DependenceGraph [{self.lo}..{self.hi}]  "
                 f"{self.num_nodes()} nodes, {self.num_edges()} edges "
                 f"({len(self.carried_edges())} carried)"]
        if show_instrs:
            lines.append("  nodes:")
            for k in range(self.lo, self.hi + 1):
                n = self.nodes[k]
                lines.append(f"    N{k} B{n.block} {n.cls:14} {self.instrs[k]!r}")
        lines.append("  edges:")
        for e in self.edges:
            lines.append(f"    {e}")
        return "\n".join(lines)

    def to_dot(self, name="depgraph"):
        """Graphviz DOT rendering (debug only). Carried edges dashed."""
        lines = [f"digraph {name} {{", "  rankdir=TB;"]
        for k in range(self.lo, self.hi + 1):
            n = self.nodes[k]
            label = f"{k}: {n.cls}".replace('"', "'")
            lines.append(f'  {k} [label="{label}"];')
        for e in self.edges:
            style = "dashed" if e.carried else "solid"
            color = {'CONTROL': 'gray'}.get(e.kind,
                     'blue' if e.is_memory() else 'black')
            lines.append(f'  {e.src} -> {e.dst} '
                         f'[label="{e.kind}", style={style}, color={color}];')
        lines.append("}")
        return "\n".join(lines)


# ── module-level construction helpers ─────────────────────────────────────────

def build_dependence_graph(instrs, lo=None, hi=None, **kw):
    """Convenience constructor for one function slice."""
    return DependenceGraph(instrs, lo, hi, **kw)


def build_function_graphs(instrs):
    """Build one DependenceGraph per function slice of a whole module.

    Returns [(func_name, DependenceGraph)] in program order. This is the standard
    entry point a whole-module pass uses; per-function scoping keeps temp names
    and block ids from aliasing across functions."""
    out = []
    for (lo, hi) in func_slices(instrs):
        name = getattr(instrs[lo], 'name', None)
        out.append((name, DependenceGraph(instrs, lo, hi)))
    return out

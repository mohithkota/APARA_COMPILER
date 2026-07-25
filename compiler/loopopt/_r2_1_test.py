"""
_r2_1_test.py -- unit tests for R2.1 DependenceGraph (reusable IR dep-graph).

Targeted structural checks that the graph records the right dependences and that
its query surface is self-consistent:

  * RAW / WAR / WAW register edges
  * memory RAW/WAR/WAW edges + alias disjointness + call barrier
  * independent instructions produce no spurious edges
  * SCC detection (Tarjan) + recurrence extraction
  * loop-carried recurrence edges (separate from intra-iteration edges)
  * topological order over the acyclic (non-carried) subgraph
  * graph validation / self-consistency (succ/pred indices, direction invariant)
  * regression compatibility: building the graph never mutates the IR

The end-to-end proof that the compiler's output is byte-identical with this
module present lives in depgraph_corpus.py; these are the small unit checks.

Run:  python3 compiler/loopopt/_r2_1_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (Temp, Const, IRFuncBegin, IRFuncEnd, IRBinOp, IRAssign,  # noqa
                IRLoadAddr, IRLoad, IRStore, IRGlobalLoad, IRGlobalStore,
                IRLabel, IRCondJump, IRJump, IRReturn, IRCall)
from loopopt.depgraph import (DependenceGraph, build_function_graphs,     # noqa
                              RAW, WAR, WAW, MEM_RAW, MEM_WAR, MEM_WAW,
                              CONTROL)

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _edge_kinds(g, resource=None):
    """{(src,dst,kind,carried)} for edges, optionally of one resource."""
    out = set()
    for e in g.edges:
        if resource is not None and e.resource != resource:
            continue
        out.add((e.src, e.dst, e.kind, e.carried))
    return out


def _has(g, src, dst, kind, carried=False):
    return any(e.src == src and e.dst == dst and e.kind == kind
               and e.carried == carried for e in g.edges)


# ── 1. register RAW / WAR / WAW ────────────────────────────────────────────────

def test_register_edges():
    print("register RAW / WAR / WAW")
    Temp.reset()
    a, b, t1, t2 = Temp('a'), Temp('b'), Temp('t1'), Temp('t2')
    instrs = [
        IRFuncBegin('f', [], {}, 0),          # 0
        IRBinOp(t1, '+', a, b),               # 1 def t1
        IRBinOp(t2, '*', t1, Const(2)),       # 2 use t1, def t2
        IRBinOp(t1, '+', t2, Const(1)),       # 3 use t2, def t1
        IRReturn(t2),                         # 4 use t2
        IRFuncEnd('f'),                       # 5
    ]
    g = DependenceGraph(instrs, 0, 5)
    check("RAW t1 (1->2)", _has(g, 1, 2, RAW))
    check("WAW t1 (1->3)", _has(g, 1, 3, WAW))
    check("WAR t1 (2->3)", _has(g, 2, 3, WAR))
    check("RAW t2 (2->3)", _has(g, 2, 3, RAW))
    check("RAW t2 (2->4)", _has(g, 2, 4, RAW))
    check("no self loops", all(e.src != e.dst for e in g.edges))
    check("validate clean", g.validate() == [])


# ── 2. independent instructions -> no register edges ───────────────────────────

def test_independent():
    print("independent instructions")
    Temp.reset()
    a, b, c, d, p, q = (Temp('a'), Temp('b'), Temp('c'), Temp('d'),
                        Temp('p'), Temp('q'))
    instrs = [
        IRFuncBegin('f', [], {}, 0),          # 0
        IRBinOp(p, '+', a, b),                # 1 def p from a,b
        IRBinOp(q, '+', c, d),                # 2 def q from c,d  (independent)
        IRReturn(p),                          # 3
        IRFuncEnd('f'),                       # 4
    ]
    g = DependenceGraph(instrs, 0, 4)
    # no register dependence between 1 and 2 (disjoint temps)
    reg = [e for e in g.edges if e.is_register()]
    check("no register edge between independent 1,2",
          not any({e.src, e.dst} == {1, 2} for e in reg))
    check("validate clean", g.validate() == [])


# ── 3. memory edges + alias disjointness + call barrier ────────────────────────

def test_memory_edges():
    print("memory RAW/WAR/WAW + disjoint slots + call barrier")
    Temp.reset()
    pa, pb, v1, v2, r = (Temp('pa'), Temp('pb'), Temp('v1'),
                         Temp('v2'), Temp('r'))
    instrs = [
        IRFuncBegin('f', [], {}, 0),               # 0
        IRLoadAddr(pa, -8),                         # 1 &slot(-8)
        IRLoadAddr(pb, -16),                        # 2 &slot(-16)
        IRStore(pa, Const(0), Const(5), 4),         # 3 W slot(-8)
        IRLoad(v1, pa, Const(0), 4),                # 4 R slot(-8)  RAW 3->4
        IRLoad(v2, pb, Const(0), 4),                # 5 R slot(-16) disjoint
        IRCall(r, 'g', []),                         # 6 barrier
        IRStore(pb, Const(0), v1, 4),               # 7 W slot(-16)
        IRReturn(r),                                # 8
        IRFuncEnd('f'),                             # 9
    ]
    g = DependenceGraph(instrs, 0, 9)
    check("MEM_RAW same slot (3->4)", _has(g, 3, 4, MEM_RAW))
    check("disjoint slots: no mem edge 3<->5",
          not any({e.src, e.dst} == {3, 5} and e.is_memory() for e in g.edges))
    check("disjoint slots: no mem edge 4<->7",
          not any({e.src, e.dst} == {4, 7} and e.is_memory() for e in g.edges))
    check("call barrier orders store 3->6", _has(g, 3, 6, MEM_RAW))
    check("call barrier orders load 4->6", _has(g, 4, 6, MEM_WAR))
    check("call barrier orders 6->7 (store after call)",
          any(e.src == 6 and e.dst == 7 and e.is_memory() for e in g.edges))
    check("validate clean", g.validate() == [])


def test_global_alias():
    print("global-object alias disjointness")
    Temp.reset()
    v, w = Temp('v'), Temp('w')
    instrs = [
        IRFuncBegin('f', [], {}, 0),                        # 0
        IRGlobalStore(0x400, Const(0), Const(1), 4),        # 1 W glob A off0
        IRGlobalLoad(v, 0x400, Const(0), elem_bytes=4),     # 2 R glob A off0 RAW 1->2
        IRGlobalLoad(w, 0x408, Const(0), elem_bytes=4),     # 3 R glob B (diff addr) disjoint
        IRReturn(v),                                        # 4
        IRFuncEnd('f'),                                     # 5
    ]
    g = DependenceGraph(instrs, 0, 5)
    check("global RAW same object (1->2)", _has(g, 1, 2, MEM_RAW))
    check("distinct globals disjoint: no edge 1<->3",
          not any({e.src, e.dst} == {1, 3} and e.is_memory() for e in g.edges))
    check("validate clean", g.validate() == [])


# ── 4. control edges (terminator pinned last) ──────────────────────────────────

def test_control_edges():
    print("control ordering (block terminator last)")
    Temp.reset()
    a, b, t = Temp('a'), Temp('b'), Temp('t')
    instrs = [
        IRFuncBegin('f', [], {}, 0),          # 0
        IRBinOp(t, '+', a, b),                # 1
        IRReturn(t),                          # 2 terminator of its block
        IRFuncEnd('f'),                       # 3
    ]
    g = DependenceGraph(instrs, 0, 3)
    # every instruction before the IRReturn in its block has a CONTROL edge to it
    check("CONTROL 0->2", _has(g, 0, 2, CONTROL))
    check("CONTROL 1->2", _has(g, 1, 2, CONTROL))
    check("validate clean", g.validate() == [])


# ── 5. loop-carried recurrence edges ───────────────────────────────────────────

def _counted_loop():
    """t initialised in a preheader, used in the header, redefined in the latch:
    a classic scalar recurrence around the back edge."""
    Temp.reset()
    t, x, n = Temp('t'), Temp('x'), Temp('n')
    return [
        IRFuncBegin('f', [], {}, 0),              # 0
        IRAssign(t, Const(0)),                    # 1 def t   (preheader)
        IRLabel('H'),                             # 2 header
        IRBinOp(x, '+', t, Const(1)),             # 3 use t, def x
        IRCondJump(x, '<', n, 'BODY', 'EXIT'),    # 4 top test
        IRLabel('BODY'),                          # 5
        IRBinOp(t, '*', x, Const(2)),             # 6 def t   (latch)
        IRJump('H'),                              # 7 back edge
        IRLabel('EXIT'),                          # 8
        IRReturn(x),                              # 9
        IRFuncEnd('f'),                           # 10
    ]


def test_loop_carried():
    print("loop-carried recurrence edges")
    instrs = _counted_loop()
    g = DependenceGraph(instrs, 0, 10)
    # intra-iteration: preheader def feeds header use, header def feeds latch use
    check("intra RAW t (1->3)", _has(g, 1, 3, RAW, carried=False))
    check("intra RAW x (3->6)", _has(g, 3, 6, RAW, carried=False))
    # loop-carried: latch def of t feeds NEXT iteration's header use of t
    check("carried RAW t (6->3)", _has(g, 6, 3, RAW, carried=True))
    # loop-carried anti: next iter's def of x must wait on this iter's use of x
    check("carried WAR x (6->3)", _has(g, 6, 3, WAR, carried=True))
    # every carried edge is tagged with the loop header and runs high->low
    for e in g.carried_edges():
        check(f"carried edge {e} tagged header",
              e.loop_header is not None and e.src > e.dst)
    check("validate clean", g.validate() == [])


# ── 6. SCC detection + recurrences ─────────────────────────────────────────────

def test_sccs():
    print("SCC detection + recurrence extraction")
    instrs = _counted_loop()
    g = DependenceGraph(instrs, 0, 10)
    sccs = g.sccs()
    # every node appears in exactly one component
    flat = [v for comp in sccs for v in comp]
    check("SCC partition covers all nodes", sorted(flat) == g.indices())
    check("SCC partition disjoint", len(flat) == len(set(flat)))
    rec = g.recurrences()
    # One non-trivial SCC: the t/x recurrence. It contains the carried scalar
    # recurrence on t (6->3) AND node 4 (the header's second use of x), because x
    # is a reused temp -- next iteration's def of x (3) must wait on this
    # iteration's uses of x (4 and 6): genuine register anti-dependences.
    check("one recurrence found", len(rec) == 1)
    check("recurrence is {3,4,6}", rec and sorted(rec[0]) == [3, 4, 6])
    check("recurrence carries the t scalar (6->3 RAW t)",
          _has(g, 6, 3, RAW, carried=True))


def test_scc_straightline_trivial():
    print("SCC on acyclic code -> all trivial")
    Temp.reset()
    a, b, t1, t2 = Temp('a'), Temp('b'), Temp('t1'), Temp('t2')
    instrs = [
        IRFuncBegin('f', [], {}, 0),
        IRBinOp(t1, '+', a, b),
        IRBinOp(t2, '*', t1, Const(2)),
        IRReturn(t2),
        IRFuncEnd('f'),
    ]
    g = DependenceGraph(instrs, 0, 4)
    check("no recurrences in straight-line code", g.recurrences() == [])
    check("all SCCs singletons", all(len(c) == 1 for c in g.sccs()))


# ── 7. topological order ───────────────────────────────────────────────────────

def test_topo_order():
    print("topological order over acyclic (non-carried) subgraph")
    instrs = _counted_loop()
    g = DependenceGraph(instrs, 0, 10)
    order = g.topo_order()
    pos = {v: i for i, v in enumerate(order)}
    check("topo covers all nodes", sorted(order) == g.indices())
    check("acyclic non-carried subgraph", g.is_acyclic())
    # every NON-carried edge respects the order; carried edges are allowed to
    # violate it (that is what makes them recurrences)
    ok = all(pos[e.src] < pos[e.dst] for e in g.edges if not e.carried)
    check("all non-carried edges respect topo order", ok)


# ── 8. graph consistency / validation ──────────────────────────────────────────

def test_validation_consistency():
    print("graph self-consistency")
    instrs = _counted_loop()
    g = DependenceGraph(instrs, 0, 10)
    check("validate() empty", g.validate() == [])
    # succ/pred mirror each other
    for e in g.edges:
        check_ok = (e in g._succ[e.src]) and (e in g._pred[e.dst])
        if not check_ok:
            check(f"succ/pred mirror {e}", False)
            return
    check("succ/pred fully mirrored", True)
    # successors()/predecessors() agree with out_edges()/in_edges()
    for i in g.indices():
        s1 = sorted(g.successors(i))
        s2 = sorted(e.dst for e in g.out_edges(i))
        if s1 != s2:
            check(f"successors() matches out_edges() at {i}", False)
            return
    check("successors()/predecessors() consistent with edge lists", True)


# ── 9. regression compatibility: no IR mutation ────────────────────────────────

def test_no_ir_mutation():
    print("regression: building the graph never mutates the IR")
    instrs = _counted_loop()
    before = [repr(x) for x in instrs]
    _ = DependenceGraph(instrs, 0, 10)
    after = [repr(x) for x in instrs]
    check("IR identical before/after graph build", before == after)


# ── 10. whole-module helper ────────────────────────────────────────────────────

def test_build_function_graphs():
    print("build_function_graphs over a two-function module")
    Temp.reset()
    a, b, c = Temp('a'), Temp('b'), Temp('c')
    instrs = [
        IRFuncBegin('f', [], {}, 0),
        IRBinOp(a, '+', b, c),
        IRReturn(a),
        IRFuncEnd('f'),
        IRFuncBegin('g', [], {}, 0),
        IRReturn(None),
        IRFuncEnd('g'),
    ]
    graphs = build_function_graphs(instrs)
    check("two function graphs", len(graphs) == 2)
    check("named f and g", [n for n, _ in graphs] == ['f', 'g'])
    check("each graph validates",
          all(g.validate() == [] for _n, g in graphs))


def main():
    for t in (test_register_edges, test_independent, test_memory_edges,
              test_global_alias, test_control_edges, test_loop_carried,
              test_sccs, test_scc_straightline_trivial, test_topo_order,
              test_validation_consistency, test_no_ir_mutation,
              test_build_function_graphs):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R2.1 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

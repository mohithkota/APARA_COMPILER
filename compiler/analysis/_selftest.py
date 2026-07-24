"""
_selftest.py -- unit-style validation for the Milestone 4 analyses.

Builds small hand-written IR for the required control-flow shapes and checks
predecessor/successor edges, dominance, natural-loop detection, and liveness
convergence. Run:  python3 compiler/analysis/_selftest.py
Analysis only -- constructs throwaway IR, never touches the compiler pipeline.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRAssign, Temp, Const)
from analysis import (build_cfg, reverse_post_order, compute_dominators,
                      build_loop_info, compute_liveness)

_fails = []
def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def analyze(instrs):
    cfg = build_cfg(instrs)
    dom = compute_dominators(cfg)
    li = build_loop_info(cfg, dom)
    lv = compute_liveness(cfg)
    return cfg, dom, li, lv


def _fb():   return IRFuncBegin('f', [], {}, 0)
def _fe():   return IRFuncEnd('f')
def T(n):    return Temp(n)


# ── 1. straight-line ───────────────────────────────────────────────────────────
def test_straightline():
    print("straight-line:")
    ins = [_fb(),
           IRBinOp(T('t1'), '+', T('a'), T('b')),
           IRBinOp(T('t2'), '+', T('t1'), T('c')),
           IRReturn(T('t2')),
           _fe()]
    cfg, dom, li, lv = analyze(ins)
    rpo = reverse_post_order(cfg)
    check("entry dominates all reachable", all(dom.dominates(0, b) for b in rpo))
    check("no loops", li.num_loops() == 0)
    # a, b, c are live-in to the entry block (used, never defined)
    check("a,b,c live-in at entry",
          {'a', 'b', 'c'} <= set(lv.live_in(0)))


# ── 2. if / else ───────────────────────────────────────────────────────────────
def test_ifelse():
    print("if/else:")
    ins = [_fb(),
           IRCondJump(T('x'), '>', Const(0), 'then', 'els'),
           IRLabel('then'), IRAssign(T('t'), Const(1)), IRJump('end'),
           IRLabel('els'),  IRAssign(T('t'), Const(2)), IRJump('end'),
           IRLabel('end'),  IRReturn(T('t')),
           _fe()]
    cfg, dom, li, lv = analyze(ins)
    bthen = cfg.label_to_block['then']; bels = cfg.label_to_block['els']
    bend = cfg.label_to_block['end']
    check("entry has two successors", len(cfg.succs(0)) == 2)
    check("then and else both -> end",
          cfg.succs(bthen) == [bend] and cfg.succs(bels) == [bend])
    check("end has two predecessors", set(cfg.preds(bend)) == {bthen, bels})
    check("entry dominates end", dom.dominates(0, bend))
    check("then does NOT dominate end", not dom.dominates(bthen, bend))
    check("idom(end) == entry", dom.immediate_dominator(bend) == 0)


# ── 3. nested branches ─────────────────────────────────────────────────────────
def test_nested_branches():
    print("nested branches:")
    ins = [_fb(),
           IRCondJump(T('x'), '>', Const(0), 'outer', 'end'),
           IRLabel('outer'),
           IRCondJump(T('y'), '>', Const(0), 'inner', 'end'),
           IRLabel('inner'), IRAssign(T('t'), Const(1)), IRJump('end'),
           IRLabel('end'), IRReturn(Const(0)),
           _fe()]
    cfg, dom, li, lv = analyze(ins)
    bo = cfg.label_to_block['outer']; bi = cfg.label_to_block['inner']
    bend = cfg.label_to_block['end']
    check("entry dominates inner", dom.dominates(0, bi))
    check("outer dominates inner", dom.dominates(bo, bi))
    check("inner does NOT dominate end", not dom.dominates(bi, bend))
    check("idom(inner) == outer", dom.immediate_dominator(bi) == bo)


# ── 4. simple loop ─────────────────────────────────────────────────────────────
def test_simple_loop():
    print("simple loop:")
    ins = [_fb(),
           IRAssign(T('i'), Const(0)),
           IRLabel('head'),
           IRCondJump(T('i'), '<', T('n'), 'body', 'exit'),
           IRLabel('body'),
           IRBinOp(T('i'), '+', T('i'), Const(1)),
           IRJump('head'),
           IRLabel('exit'), IRReturn(T('i')),
           _fe()]
    cfg, dom, li, lv = analyze(ins)
    bhead = cfg.label_to_block['head']; bbody = cfg.label_to_block['body']
    check("one loop detected", li.num_loops() == 1)
    lp = li.loops[0]
    check("loop header is 'head' block", lp.header == bhead)
    check("body block in loop", bbody in lp.body)
    check("back edge body->head", (bbody, bhead) in lp.back_edges)
    check("header dominates body", dom.dominates(bhead, bbody))
    check("max nesting depth 1", li.max_depth == 1)
    check("n live across loop (live-out of header)", 'n' in lv.live_out(bhead))


# ── 5. nested loops ────────────────────────────────────────────────────────────
def test_nested_loops():
    print("nested loops:")
    ins = [_fb(),
           IRAssign(T('i'), Const(0)),
           IRLabel('oh'),
           IRCondJump(T('i'), '<', T('n'), 'ob', 'oe'),
           IRLabel('ob'),
           IRAssign(T('j'), Const(0)),
           IRLabel('ih'),
           IRCondJump(T('j'), '<', T('n'), 'ib', 'ie'),
           IRLabel('ib'),
           IRBinOp(T('j'), '+', T('j'), Const(1)),
           IRJump('ih'),
           IRLabel('ie'),
           IRBinOp(T('i'), '+', T('i'), Const(1)),
           IRJump('oh'),
           IRLabel('oe'), IRReturn(T('i')),
           _fe()]
    cfg, dom, li, lv = analyze(ins)
    check("two loops detected", li.num_loops() == 2)
    check("max nesting depth 2", li.max_depth == 2)
    bib = cfg.label_to_block['ib']
    check("inner body at nesting depth 2", li.depth(bib) == 2)


# ── 6. multiple returns ────────────────────────────────────────────────────────
def test_multiple_returns():
    print("multiple returns:")
    ins = [_fb(),
           IRCondJump(T('x'), '>', Const(0), 'r1', 'r2'),
           IRLabel('r1'), IRReturn(Const(1)),
           IRLabel('r2'), IRReturn(Const(2)),
           _fe()]
    cfg, dom, li, lv = analyze(ins)
    b1 = cfg.label_to_block['r1']; b2 = cfg.label_to_block['r2']
    check("both return blocks have no successors",
          cfg.succs(b1) == [] and cfg.succs(b2) == [])
    check("entry dominates both returns",
          dom.dominates(0, b1) and dom.dominates(0, b2))
    check("no loops", li.num_loops() == 0)


def main():
    for t in (test_straightline, test_ifelse, test_nested_branches,
              test_simple_loop, test_nested_loops, test_multiple_returns):
        t()
    print()
    if _fails:
        print(f"SELFTEST FAILED ({len(_fails)}): {_fails}")
        return 1
    print("SELFTEST PASSED (all shapes)")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

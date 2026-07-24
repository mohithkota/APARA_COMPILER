"""
_m0_test.py -- unit tests for the M0 Loop Optimization Framework
(LoopDiscovery + LoopDescriptor + observe-only LoopVerifier).

Builds small hand-written IR for the required loop shapes and checks that
discovery produces structurally-correct descriptors and that the verifier finds
no violations. Analysis only -- throwaway IR, never touches the compiler
pipeline. Run:  python3 compiler/loopopt/_m0_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRAssign, Temp, Const)
from loopopt import (discover, discover_function, LoopVerifier,
                     TOP_TESTED)

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _fb():
    return IRFuncBegin('f', [], {}, 0)


def _fe():
    return IRFuncEnd('f')


def T(n):
    return Temp(n)


# ── 1. no loop ───────────────────────────────────────────────────────────────
def test_no_loop():
    print("no-loop:")
    ins = [_fb(),
           IRBinOp(T('t1'), '+', T('a'), T('b')),
           IRReturn(T('t1')),
           _fe()]
    descs = discover(ins)
    check("zero loops discovered", len(descs) == 0)


# ── 2. single top-tested while loop ──────────────────────────────────────────
def test_single_loop():
    print("single top-tested loop:")
    # pre: i=0 ; head: if i<n goto body else exit ; body ; step i++ ; goto head
    ins = [_fb(),
           IRAssign(T('i'), Const(0)),                              # preheader
           IRLabel('head'),
           IRCondJump(T('i'), '<', T('n'), 'body', 'exit'),         # guard
           IRLabel('body'),
           IRBinOp(T('s'), '+', T('s'), T('i')),
           IRLabel('step'),
           IRBinOp(T('i'), '+', T('i'), Const(1)),
           IRJump('head'),                                          # back-edge
           IRLabel('exit'),
           IRReturn(T('s')),
           _fe()]
    descs = discover(ins)
    check("one loop discovered", len(descs) == 1)
    d = descs[0]
    hb = d.cfg.label_to_block['head']
    check("header is the guard block", d.header == hb)
    check("shape is top-tested", d.shape == TOP_TESTED)
    check("single latch", len(d.latches) == 1)
    check("preheader is set (unique external pred)", d.preheader is not None)
    check("header label is 'head'", d.label() == 'head')
    check("innermost and outermost", d.is_innermost and d.is_outermost)
    check("depth == 1", d.depth == 1)
    # exit edge leaves the loop to 'exit'
    ebe = d.cfg.label_to_block['exit']
    check("exactly one exit edge to 'exit'",
          len(d.exit_edges) == 1 and d.exit_blocks == [ebe])
    r = LoopVerifier().verify(d)
    check("verifier clean", r.ok)


# ── 3. nested loops (parent/child, depth, innermost) ─────────────────────────
def test_nested_loops():
    print("nested loops:")
    ins = [_fb(),
           IRAssign(T('i'), Const(0)),
           IRLabel('outer'),
           IRCondJump(T('i'), '<', T('n'), 'obody', 'oexit'),
           IRLabel('obody'),
           IRAssign(T('j'), Const(0)),
           IRLabel('inner'),
           IRCondJump(T('j'), '<', T('m'), 'ibody', 'iexit'),
           IRLabel('ibody'),
           IRBinOp(T('j'), '+', T('j'), Const(1)),
           IRJump('inner'),                                         # inner back-edge
           IRLabel('iexit'),
           IRBinOp(T('i'), '+', T('i'), Const(1)),
           IRJump('outer'),                                         # outer back-edge
           IRLabel('oexit'),
           IRReturn(T('i')),
           _fe()]
    descs = discover(ins)
    check("two loops discovered", len(descs) == 2)
    by_label = {d.label(): d for d in descs}
    outer, inner = by_label.get('outer'), by_label.get('inner')
    check("both loops found by label", outer is not None and inner is not None)
    if outer and inner:
        check("inner depth 2, outer depth 1", inner.depth == 2 and outer.depth == 1)
        check("inner is innermost", inner.is_innermost and not inner.is_outermost)
        check("outer is outermost", outer.is_outermost and not outer.is_innermost)
        check("inner.parent is outer", inner.parent is outer)
        check("outer.children == [inner]", outer.children == [inner])
        check("inner body subset of outer body",
              inner.body_blocks <= outer.body_blocks)
        check("innermost-first ordering", descs[0] is inner)
    r = LoopVerifier().verify_all(descs)
    check("verifier clean on nest", r.ok)


# ── 4. verifier is observe-only (does not mutate) ────────────────────────────
def test_verifier_readonly():
    print("verifier observe-only:")
    ins = [_fb(),
           IRAssign(T('i'), Const(0)),
           IRLabel('head'),
           IRCondJump(T('i'), '<', T('n'), 'body', 'exit'),
           IRLabel('body'),
           IRBinOp(T('i'), '+', T('i'), Const(1)),
           IRJump('head'),
           IRLabel('exit'),
           IRReturn(T('i')),
           _fe()]
    before = [repr(x) for x in ins]
    descs = discover(ins)
    LoopVerifier().verify_all(descs)
    after = [repr(x) for x in ins]
    check("IR unchanged by discovery+verify", before == after)


def main():
    for t in (test_no_loop, test_single_loop, test_nested_loops,
              test_verifier_readonly):
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"M0 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("M0 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

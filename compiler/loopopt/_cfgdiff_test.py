"""
_cfgdiff_test.py -- unit tests for the CFG-differencing utility (M4 dev tool).

Builds known before/after CFGs by hand and checks that diff_cfg / diff_loop
report exactly the blocks and edges that changed. The utility is a developer
tool, so these tests pin its behaviour independently of the canonicalizer.
Run:  python3 compiler/loopopt/_cfgdiff_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, Temp, Const)
from analysis import build_cfg
from loopopt.cfgdiff import diff_cfg, diff_loop
from loopopt import discover

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _cfg(ins):
    return build_cfg(ins, 0, len(ins) - 1)


# A canonical top-tested loop (has a preheader "pre").
def _canonical():
    return [IRFuncBegin('f', [], {}, 0),
            IRLabel('pre'),
            IRJump('head'),
            IRLabel('head'),
            IRCondJump(Temp('i'), '<', Temp('n'), 'body', 'exit'),
            IRLabel('body'),
            IRBinOp(Temp(), '+', Temp('a'), Temp('b')),
            IRJump('head'),
            IRLabel('exit'),
            IRReturn(Const(0)),
            IRFuncEnd('f')]


# Same loop with NO preheader: two external predecessors jump straight to head.
def _no_preheader():
    return [IRFuncBegin('f', [], {}, 0),
            IRCondJump(Temp('c'), '==', Const(0), 'head', 'alt'),
            IRLabel('alt'),
            IRJump('head'),
            IRLabel('head'),
            IRCondJump(Temp('i'), '<', Temp('n'), 'body', 'exit'),
            IRLabel('body'),
            IRBinOp(Temp(), '+', Temp('a'), Temp('b')),
            IRJump('head'),
            IRLabel('exit'),
            IRReturn(Const(0)),
            IRFuncEnd('f')]


def test_identical_is_empty():
    print("identical CFGs -> empty diff:")
    d = diff_cfg(_cfg(_canonical()), _cfg(_canonical()))
    check("empty diff on identical CFG", d.empty)
    check("no blocks added/removed", not d.blocks_added and not d.blocks_removed)
    check("no edges added/removed", not d.edges_added and not d.edges_removed)
    check("report says no change", "no structural change" in d.report())


def test_added_block_and_edges():
    print("added preheader block + rerouted edges:")
    before = _cfg(_no_preheader())
    after = _cfg(_canonical())
    d = diff_cfg(before, after)
    check("new 'pre' block reported as added", "L:pre" in d.blocks_added)
    check("edge pre->head reported as added", ("L:pre", "L:head") in d.edges_added)
    check("something changed (not empty)", not d.empty)
    # the canonical form has no 'alt' block; going the other direction removes it
    d2 = diff_cfg(before, before)
    check("self-diff still empty", d2.empty)


def test_removed_block():
    print("removed block direction:")
    d = diff_cfg(_cfg(_canonical()), _cfg(_no_preheader()))
    check("'pre' reported as removed", "L:pre" in d.blocks_removed)
    check("edge pre->head reported as removed", ("L:pre", "L:head") in d.edges_removed)


def test_edge_retarget_only():
    print("pure edge retarget (no block count change):")
    # before: body -> head ; after: body -> pre  (retarget the back edge target)
    before = [IRFuncBegin('f', [], {}, 0),
              IRLabel('head'), IRCondJump(Temp('i'), '<', Temp('n'), 'body', 'exit'),
              IRLabel('body'), IRJump('head'),
              IRLabel('exit'), IRReturn(Const(0)), IRFuncEnd('f')]
    after = [IRFuncBegin('f', [], {}, 0),
             IRLabel('head'), IRCondJump(Temp('i'), '<', Temp('n'), 'body', 'exit'),
             IRLabel('body'), IRJump('exit'),
             IRLabel('exit'), IRReturn(Const(0)), IRFuncEnd('f')]
    d = diff_cfg(_cfg(before), _cfg(after))
    check("no blocks added/removed on retarget", not d.blocks_added and not d.blocks_removed)
    check("edge body->head removed", ("L:body", "L:head") in d.edges_removed)
    check("edge body->exit added", ("L:body", "L:exit") in d.edges_added)


def test_diff_loop_preheader_change():
    print("diff_loop reports preheader appearing:")
    before = discover(_no_preheader())[0]
    after = discover(_canonical())[0]
    ld = diff_loop(before, after)
    check("header identity preserved", ld.identity_preserved)
    check("structurally changed", ld.structurally_changed)
    check("preheader change None -> pre",
          ld.preheader_change == (None, 'pre'))
    check("cfg_diff embedded and non-empty", not ld.cfg_diff.empty)


def test_diff_loop_no_change():
    print("diff_loop on an unchanged loop:")
    before = discover(_canonical())[0]
    after = discover(_canonical())[0]
    ld = diff_loop(before, after)
    check("no structural change", not ld.structurally_changed)
    check("identity preserved", ld.identity_preserved)
    check("no preheader/latch/exit change",
          ld.preheader_change is None and ld.latch_change is None
          and ld.exit_change is None)


def main():
    tests = [test_identical_is_empty, test_added_block_and_edges,
             test_removed_block, test_edge_retarget_only,
             test_diff_loop_preheader_change, test_diff_loop_no_change]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"CFGDIFF TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("CFGDIFF TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""
_licm_test.py -- unit-style validation for conservative LICM (licm2, M8).
Run:  python3 compiler/analysis/_licm_test.py
Builds throwaway IR; never touches the compiler pipeline.
"""

import os
import sys
os.environ['APARA_LICM'] = '1'          # the pass is opt-in; enable it for tests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRUnaryOp, IRCast,
                IRCondJump, IRJump, IRReturn, IRAssign, IRLoad, IRStore, IRCall,
                Temp, Const)
from licm2 import loop_invariant_code_motion as licm

_fails = []
def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)

def T(n): return Temp(n)
def fb():  return IRFuncBegin('f', [], {}, 0)
def fe():  return IRFuncEnd('f')

def idx_of_dest(instrs, dest):
    for i, ins in enumerate(instrs):
        if getattr(ins, 'dest', None) is not None and ins.dest.name == dest:
            return i
    return -1

def label_idx(instrs, name):
    for i, ins in enumerate(instrs):
        if type(ins).__name__ == 'IRLabel' and ins.name == name:
            return i
    return -1


# Standard single-loop shape with a preheader block (the block before 'head').
# preheader ends by falling through to head.
def _loop(body):
    return ([fb(),
             IRLabel('pre'),                                  # preheader block
             IRAssign(T('inv_a'), Const(10)),                 # invariant source (outside loop)
             IRLabel('head'),
             IRCondJump(T('i'), '<', T('n'), 'body', 'exit')]
            + body +
            [IRLabel('exit'), IRReturn(None), fe()])


def test_invariant_arith():
    print("invariant arithmetic hoisted:")
    ins = _loop([IRLabel('body'),
                 IRBinOp(T('x'), '+', T('inv_a'), Const(5)),   # invariant -> hoist
                 IRStore(T('p'), Const(0), T('x'), 8),
                 IRJump('head')])
    out = licm(ins)
    check("x hoisted before head", idx_of_dest(out, 'x') < label_idx(out, 'head'))


def test_invariant_unary_cast():
    print("invariant unary + cast hoisted:")
    ins = _loop([IRLabel('body'),
                 IRUnaryOp(T('u'), '-', T('inv_a')),
                 IRCast(T('cst'), T('u'), '$i64', '$i32'),
                 IRStore(T('p'), Const(0), T('cst'), 8),
                 IRJump('head')])
    out = licm(ins)
    check("unary hoisted", idx_of_dest(out, 'u') < label_idx(out, 'head'))
    check("cast hoisted", idx_of_dest(out, 'cst') < label_idx(out, 'head'))


def test_loop_carried_not_hoisted():
    print("loop-carried NOT hoisted:")
    ins = _loop([IRLabel('body'),
                 IRBinOp(T('acc'), '+', T('acc'), T('inv_a')),   # acc uses itself -> variant
                 IRStore(T('p'), Const(0), T('acc'), 8),
                 IRJump('head')])
    out = licm(ins)
    check("acc stays in loop", idx_of_dest(out, 'acc') > label_idx(out, 'head'))


def test_load_not_hoisted():
    print("invariant load NOT hoisted:")
    ins = _loop([IRLabel('body'),
                 IRLoad(T('ld'), T('inv_a'), Const(0), 8),       # a load -> never hoisted
                 IRStore(T('p'), Const(0), T('ld'), 8),
                 IRJump('head')])
    out = licm(ins)
    check("load stays in loop", idx_of_dest(out, 'ld') > label_idx(out, 'head'))


def test_call_not_hoisted():
    print("call NOT hoisted:")
    ins = _loop([IRLabel('body'),
                 IRCall(T('cr'), 'g', [T('inv_a')]),
                 IRStore(T('p'), Const(0), T('cr'), 8),
                 IRJump('head')])
    out = licm(ins)
    check("call stays in loop", idx_of_dest(out, 'cr') > label_idx(out, 'head'))


def test_used_after_loop_not_hoisted():
    print("value used after loop NOT hoisted (conservative dominance):")
    ins = [fb(), IRLabel('pre'), IRAssign(T('inv_a'), Const(10)),
           IRLabel('head'), IRCondJump(T('i'), '<', T('n'), 'body', 'exit'),
           IRLabel('body'), IRBinOp(T('x'), '+', T('inv_a'), Const(5)), IRJump('head'),
           IRLabel('exit'), IRStore(T('p'), Const(0), T('x'), 8),   # x used after loop
           IRReturn(None), fe()]
    out = licm(ins)
    check("x not hoisted (used after loop)", idx_of_dest(out, 'x') > label_idx(out, 'head'))


def test_no_invariants_identical():
    print("loop with no invariants -> unchanged:")
    ins = _loop([IRLabel('body'),
                 IRBinOp(T('i'), '+', T('i'), Const(1)),  # i defined in loop -> variant
                 IRBinOp(T('y'), '+', T('i'), T('n')),    # depends on variant i -> variant
                 IRStore(T('p'), Const(0), T('y'), 8),
                 IRJump('head')])
    before = [type(i).__name__ for i in ins]
    out = licm(ins)
    check("output identical (no invariants)", [type(i).__name__ for i in out] == before)


def main():
    for t in (test_invariant_arith, test_invariant_unary_cast,
              test_loop_carried_not_hoisted, test_load_not_hoisted,
              test_call_not_hoisted, test_used_after_loop_not_hoisted,
              test_no_invariants_identical):
        t()
    print()
    if _fails:
        print(f"LICM SELFTEST FAILED ({len(_fails)}): {_fails}")
        return 1
    print("LICM SELFTEST PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

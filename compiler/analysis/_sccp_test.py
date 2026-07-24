"""
_sccp_test.py -- unit-style validation for SCCP (Milestone 5).
Run:  python3 compiler/analysis/_sccp_test.py
Builds throwaway IR; never touches the compiler pipeline.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRUnaryOp, IRCondJump,
                IRJump, IRReturn, IRAssign, IRStore, IRCall, Temp, Const)
from sccp import sparse_conditional_constant_propagation as sccp

_fails = []
def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)

def T(n): return Temp(n)
def fb():  return IRFuncBegin('f', [], {}, 0)
def fe():  return IRFuncEnd('f')

def kinds(instrs):
    return [type(i).__name__ for i in instrs]

def find_binops(instrs):
    return [i for i in instrs if type(i).__name__ == 'IRBinOp']


# arithmetic constants: t = 2 + 3 ; use t
def test_arith():
    print("arithmetic constants:")
    ins = [fb(), IRBinOp(T('t'), '+', Const(2), Const(3)),
           IRStore(T('p'), Const(0), T('t'), 8), IRReturn(None), fe()]
    out = sccp(ins)
    st = [i for i in out if type(i).__name__ == 'IRStore'][0]
    check("store source folded to Const(5)", isinstance(st.src, Const) and st.src.value == 5)


# chained constants: a=4; b=a+1; c=b*2; use c
def test_chained():
    print("chained constants:")
    ins = [fb(), IRAssign(T('a'), Const(4)),
           IRBinOp(T('b'), '+', T('a'), Const(1)),
           IRBinOp(T('c'), '*', T('b'), Const(2)),
           IRStore(T('p'), Const(0), T('c'), 8), IRReturn(None), fe()]
    out = sccp(ins)
    st = [i for i in out if type(i).__name__ == 'IRStore'][0]
    check("c folded to (4+1)*2 = 10", isinstance(st.src, Const) and st.src.value == 10)


# constant comparison in a branch
def test_const_cmp_true():
    print("always-true branch:")
    ins = [fb(), IRAssign(T('x'), Const(5)),
           IRCondJump(T('x'), '>', Const(0), 'then', 'els'),
           IRLabel('then'), IRStore(T('p'), Const(0), Const(1), 8), IRJump('end'),
           IRLabel('els'),  IRStore(T('p'), Const(0), Const(2), 8), IRJump('end'),
           IRLabel('end'), IRReturn(None), fe()]
    out = sccp(ins)
    ks = kinds(out)
    check("no IRCondJump remains", 'IRCondJump' not in ks)
    check("'els' block removed (unreachable)",
          not any(type(i).__name__ == 'IRLabel' and i.name == 'els' for i in out))
    check("'then' block kept",
          any(type(i).__name__ == 'IRLabel' and i.name == 'then' for i in out))


def test_const_cmp_false():
    print("always-false branch:")
    ins = [fb(), IRAssign(T('x'), Const(0)),
           IRCondJump(T('x'), '>', Const(0), 'then', 'els'),
           IRLabel('then'), IRStore(T('p'), Const(0), Const(1), 8), IRJump('end'),
           IRLabel('els'),  IRStore(T('p'), Const(0), Const(2), 8), IRJump('end'),
           IRLabel('end'), IRReturn(None), fe()]
    out = sccp(ins)
    check("no IRCondJump remains", 'IRCondJump' not in kinds(out))
    check("'then' block removed (unreachable)",
          not any(type(i).__name__ == 'IRLabel' and i.name == 'then' for i in out))
    check("'els' block kept",
          any(type(i).__name__ == 'IRLabel' and i.name == 'els' for i in out))


# loop condition NOT constant -> nothing folded, branch preserved
def test_nonconst_loop():
    print("non-constant loop condition:")
    ins = [fb(), IRAssign(T('i'), Const(0)),
           IRLabel('head'),
           IRCondJump(T('i'), '<', T('n'), 'body', 'exit'),   # n is unknown
           IRLabel('body'), IRBinOp(T('i'), '+', T('i'), Const(1)), IRJump('head'),
           IRLabel('exit'), IRReturn(T('i')), fe()]
    out = sccp(ins)
    check("loop branch preserved (condition not constant)",
          any(type(i).__name__ == 'IRCondJump' for i in out))
    check("loop body kept",
          any(type(i).__name__ == 'IRLabel' and i.name == 'body' for i in out))


# mixed: t = a + 5 where a is a call result (OVER) -> not folded
def test_mixed():
    print("mixed constant / non-constant:")
    ins = [fb(), IRCall(T('a'), 'g', []),
           IRBinOp(T('t'), '+', T('a'), Const(5)),
           IRStore(T('p'), Const(0), T('t'), 8), IRReturn(None), fe()]
    out = sccp(ins)
    st = [i for i in out if type(i).__name__ == 'IRStore'][0]
    check("t NOT folded (a is over-defined)", isinstance(st.src, Temp))


def main():
    for t in (test_arith, test_chained, test_const_cmp_true, test_const_cmp_false,
              test_nonconst_loop, test_mixed):
        t()
    print()
    if _fails:
        print(f"SCCP SELFTEST FAILED ({len(_fails)}): {_fails}")
        return 1
    print("SCCP SELFTEST PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

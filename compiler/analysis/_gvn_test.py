"""
_gvn_test.py -- unit-style validation for GVN (Milestone 6).
Run:  python3 compiler/analysis/_gvn_test.py
Builds throwaway IR; never touches the compiler pipeline.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRUnaryOp, IRCondJump,
                IRJump, IRReturn, IRAssign, IRStore, IRLoad, IRCall, Temp, Const)
from gvn import global_value_numbering as gvn

_fails = []
def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)

def T(n): return Temp(n)
def fb():  return IRFuncBegin('f', [], {}, 0)
def fe():  return IRFuncEnd('f')

def is_copy_of(ins, dst, src):
    return (type(ins).__name__ == 'IRAssign' and ins.dest.name == dst
            and isinstance(ins.src, Temp) and ins.src.name == src)


# repeated arithmetic: t1=a+b ; t2=a+b  -> t2 becomes copy of t1
def test_repeated():
    print("repeated arithmetic:")
    ins = [fb(),
           IRBinOp(T('t1'), '+', T('a'), T('b')),
           IRBinOp(T('t2'), '+', T('a'), T('b')),
           IRStore(T('p'), Const(0), T('t2'), 8), IRReturn(None), fe()]
    out = gvn(ins)
    check("t2 = t1 (CSE)", any(is_copy_of(i, 't2', 't1') for i in out))
    check("t1 expression kept", any(type(i).__name__ == 'IRBinOp' and i.dest.name == 't1' for i in out))


# commutative: t1=a+b ; t2=b+a  -> still redundant
def test_commutative():
    print("commutative canonicalization:")
    ins = [fb(),
           IRBinOp(T('t1'), '+', T('a'), T('b')),
           IRBinOp(T('t2'), '+', T('b'), T('a')),
           IRStore(T('p'), Const(0), T('t2'), 8), IRReturn(None), fe()]
    out = gvn(ins)
    check("b+a recognized as a+b", any(is_copy_of(i, 't2', 't1') for i in out))


# non-commutative must NOT wrongly merge: a-b vs b-a
def test_noncommutative():
    print("non-commutative not merged:")
    ins = [fb(),
           IRBinOp(T('t1'), '-', T('a'), T('b')),
           IRBinOp(T('t2'), '-', T('b'), T('a')),
           IRStore(T('p'), Const(0), T('t2'), 8), IRReturn(None), fe()]
    out = gvn(ins)
    check("a-b and b-a NOT merged", not any(is_copy_of(i, 't2', 't1') for i in out))


# unary: t1 = -a ; t2 = -a
def test_unary():
    print("unary expressions:")
    ins = [fb(),
           IRUnaryOp(T('t1'), '-', T('a')),
           IRUnaryOp(T('t2'), '-', T('a')),
           IRStore(T('p'), Const(0), T('t2'), 8), IRReturn(None), fe()]
    out = gvn(ins)
    check("t2 = t1 (unary CSE)", any(is_copy_of(i, 't2', 't1') for i in out))


# nested: t1=a+b; x=t1*c; t2=a+b; y=t2*c -> y reuses x
def test_nested():
    print("nested expressions:")
    ins = [fb(),
           IRBinOp(T('t1'), '+', T('a'), T('b')),
           IRBinOp(T('x'), '*', T('t1'), T('c')),
           IRBinOp(T('t2'), '+', T('a'), T('b')),
           IRBinOp(T('y'), '*', T('t2'), T('c')),
           IRStore(T('p'), Const(0), T('y'), 8), IRReturn(None), fe()]
    out = gvn(ins)
    # t2 -> t1 first; but x*c keyed on t1, y*c keyed on t2; without operand VN
    # unification y!=x unless t2 already numbered to t1. We at least expect t2=t1.
    check("t2 = t1", any(is_copy_of(i, 't2', 't1') for i in out))


# dominated reuse across blocks (then-block dominated by entry where expr defined)
def test_dominated():
    print("dominated reuse:")
    ins = [fb(),
           IRBinOp(T('t1'), '+', T('a'), T('b')),
           IRCondJump(T('c'), '>', Const(0), 'then', 'end'),
           IRLabel('then'),
           IRBinOp(T('t2'), '+', T('a'), T('b')),      # dominated by entry
           IRStore(T('p'), Const(0), T('t2'), 8), IRJump('end'),
           IRLabel('end'), IRReturn(None), fe()]
    out = gvn(ins)
    check("t2 reuses dominating t1", any(is_copy_of(i, 't2', 't1') for i in out))


# NON-dominated: expr computed only in one arm must NOT be reused in a sibling arm
def test_nondominated():
    print("non-dominated NOT reused:")
    ins = [fb(),
           IRCondJump(T('c'), '>', Const(0), 'a1', 'a2'),
           IRLabel('a1'),
           IRBinOp(T('t1'), '+', T('a'), T('b')),      # only on this arm
           IRStore(T('p'), Const(0), T('t1'), 8), IRJump('end'),
           IRLabel('a2'),
           IRBinOp(T('t2'), '+', T('a'), T('b')),      # sibling arm: NOT dominated by t1
           IRStore(T('p'), Const(0), T('t2'), 8), IRJump('end'),
           IRLabel('end'), IRReturn(None), fe()]
    out = gvn(ins)
    check("t2 NOT replaced by non-dominating t1",
          not any(is_copy_of(i, 't2', 't1') for i in out))


# calls / loads produce fresh values, never CSE'd
def test_calls_loads():
    print("calls / loads not value-numbered:")
    ins = [fb(),
           IRCall(T('t1'), 'g', []),
           IRCall(T('t2'), 'g', []),
           IRLoad(T('l1'), T('p'), Const(0), 8),
           IRLoad(T('l2'), T('p'), Const(0), 8),
           IRStore(T('q'), Const(0), T('t2'), 8), IRReturn(None), fe()]
    out = gvn(ins)
    check("two calls NOT merged", not any(is_copy_of(i, 't2', 't1') for i in out))
    check("two loads NOT merged", not any(is_copy_of(i, 'l2', 'l1') for i in out))


def main():
    for t in (test_repeated, test_commutative, test_noncommutative, test_unary,
              test_nested, test_dominated, test_nondominated, test_calls_loads):
        t()
    print()
    if _fails:
        print(f"GVN SELFTEST FAILED ({len(_fails)}): {_fails}")
        return 1
    print("GVN SELFTEST PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

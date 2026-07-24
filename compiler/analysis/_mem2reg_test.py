"""
_mem2reg_test.py -- unit-style validation for Mem2Reg (Milestone 7).
Run:  python3 compiler/analysis/_mem2reg_test.py
Builds throwaway IR; never touches the compiler pipeline.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRAssign, IRLoad, IRStore, IRLoadAddr, IRCall, Temp, Const)
from mem2reg import mem2reg

_fails = []
def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)

def T(n): return Temp(n)
def fb():  return IRFuncBegin('f', [], {}, 0)
def fe():  return IRFuncEnd('f')

def counts(instrs):
    from collections import Counter
    return Counter(type(i).__name__ for i in instrs)


# simple local: store x; load x; use.  x has offset -8, address never taken.
def test_simple():
    print("simple local:")
    ins = [fb(),
           IRLoadAddr(T('a0'), -8), IRStore(T('a0'), Const(0), T('v'), 8),
           IRLoadAddr(T('a1'), -8), IRLoad(T('t'), T('a1'), Const(0), 8),
           IRStore(T('p'), Const(0), T('t'), 8), IRReturn(None), fe()]
    out = mem2reg(ins)
    c = counts(out)
    check("load of slot promoted away", c['IRLoad'] == 0)
    # note: the store to p (a different, escaping base) stays
    check("slot store promoted to IRAssign", any(type(i).__name__ == 'IRAssign' for i in out))


# repeated loads of the same local -> all become temp reads
def test_repeated_loads():
    print("repeated loads:")
    ins = [fb(),
           IRLoadAddr(T('a0'), -8), IRStore(T('a0'), Const(0), T('v'), 8),
           IRLoadAddr(T('a1'), -8), IRLoad(T('t1'), T('a1'), Const(0), 8),
           IRLoadAddr(T('a2'), -8), IRLoad(T('t2'), T('a2'), Const(0), 8),
           IRBinOp(T('r'), '+', T('t1'), T('t2')),
           IRStore(T('p'), Const(0), T('r'), 8), IRReturn(None), fe()]
    out = mem2reg(ins)
    check("both loads of the local eliminated", counts(out)['IRLoad'] == 0)


# repeated stores to the same local -> MULTIPLE definitions, conservatively
# left in memory (a value merge would need a phi this IR cannot express).
def test_repeated_stores():
    print("repeated stores (multi-store -> left in memory):")
    ins = [fb(),
           IRLoadAddr(T('a0'), -8), IRStore(T('a0'), Const(0), Const(1), 8),
           IRLoadAddr(T('a1'), -8), IRStore(T('a1'), Const(0), Const(2), 8),
           IRLoadAddr(T('a2'), -8), IRLoad(T('t'), T('a2'), Const(0), 8),
           IRStore(T('p'), Const(0), T('t'), 8), IRReturn(None), fe()]
    out = mem2reg(ins)
    check("multi-store local NOT promoted (load kept)", counts(out)['IRLoad'] >= 1)


# if/else: each arm writes x, read after merge -> two stores merging at a join.
# Conservatively left in memory (would require a phi).
def test_ifelse():
    print("if/else merge (multi-store -> left in memory):")
    ins = [fb(),
           IRCondJump(T('c'), '>', Const(0), 'then', 'els'),
           IRLabel('then'), IRLoadAddr(T('a0'), -8), IRStore(T('a0'), Const(0), Const(1), 8), IRJump('end'),
           IRLabel('els'),  IRLoadAddr(T('a1'), -8), IRStore(T('a1'), Const(0), Const(2), 8), IRJump('end'),
           IRLabel('end'),  IRLoadAddr(T('a2'), -8), IRLoad(T('t'), T('a2'), Const(0), 8),
           IRStore(T('p'), Const(0), T('t'), 8), IRReturn(None), fe()]
    out = mem2reg(ins)
    check("merged local NOT promoted (load kept)", counts(out)['IRLoad'] >= 1)


# address-taken local: &x passed to a call -> MUST NOT promote
def test_address_taken():
    print("address-taken local (must NOT promote):")
    ins = [fb(),
           IRLoadAddr(T('a0'), -8), IRStore(T('a0'), Const(0), Const(1), 8),
           IRLoadAddr(T('a1'), -8), IRCall(T('r'), 'g', [T('a1')]),   # &x escapes into call
           IRLoadAddr(T('a2'), -8), IRLoad(T('t'), T('a2'), Const(0), 8),
           IRStore(T('p'), Const(0), T('t'), 8), IRReturn(None), fe()]
    out = mem2reg(ins)
    check("slot NOT promoted (address escaped)", counts(out)['IRLoad'] >= 1)


# pointer alias: address used in arithmetic -> MUST NOT promote
def test_alias():
    print("aliased local (must NOT promote):")
    ins = [fb(),
           IRLoadAddr(T('a0'), -8), IRStore(T('a0'), Const(0), Const(1), 8),
           IRLoadAddr(T('a1'), -8), IRBinOp(T('q'), '+', T('a1'), Const(4)),  # &x + 4 : address escapes
           IRLoadAddr(T('a2'), -8), IRLoad(T('t'), T('a2'), Const(0), 8),
           IRStore(T('p'), Const(0), T('t'), 8), IRReturn(None), fe()]
    out = mem2reg(ins)
    check("slot NOT promoted (address in arithmetic)", counts(out)['IRLoad'] >= 1)


def main():
    for t in (test_simple, test_repeated_loads, test_repeated_stores, test_ifelse,
              test_address_taken, test_alias):
        t()
    print()
    if _fails:
        print(f"MEM2REG SELFTEST FAILED ({len(_fails)}): {_fails}")
        return 1
    print("MEM2REG SELFTEST PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

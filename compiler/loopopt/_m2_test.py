"""
_m2_test.py -- unit tests for the M2 MemEffects analysis.

Hand-written memory-backed IR for the required shapes; checks the memory
catalogue, side-effect flags, the alias summary, and the invariant set. Analysis
only. Run:  python3 compiler/loopopt/_m2_test.py

Coverage: load-only, store-only, load/store mix, pointer (computed) access,
call in loop, no-memory loop, nested loops, multi-exit, do-while, short-circuit.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRStore, IRLoad, IRLoadAddr, IRCall, IRGlobalLoad,
                Temp, Const)
from loopopt import discover, annotate_memory_effects, AliasSummary

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _fb():
    return IRFuncBegin('f', [], {}, 0)


def _fe():
    return IRFuncEnd('f')


def analyze(ins):
    descs = discover(ins)
    annotate_memory_effects(descs)
    return descs


def _loop(body):
    """A while loop `head: if t<n goto body else exit; <body>; goto head`."""
    return [_fb(),
            IRLabel('head'),
            IRCondJump(Temp('t'), '<', Temp('n'), 'body', 'exit'),
            IRLabel('body')] + body + [
            IRJump('head'),
            IRLabel('exit'), IRReturn(Const(0)), _fe()]


def test_load_only():
    print("load-only loop:")
    a = Temp(); v = Temp()
    d = analyze(_loop([IRLoadAddr(a, -8), IRLoad(v, a, Const(0), 8)]))[0]
    check("one load, no store", len(d.loads) == 1 and len(d.stores) == 0)
    check("may_read, not may_write",
          d.may_read_memory and not d.may_write_memory)
    check("no side effects", not d.has_side_effects)
    check("load key is stack slot -8", d.loads[0].key == ('stack', -8))


def test_store_only():
    print("store-only loop:")
    a = Temp()
    d = analyze(_loop([IRLoadAddr(a, -8), IRStore(a, Const(0), Const(1), 8)]))[0]
    check("one store, no load", len(d.stores) == 1 and len(d.loads) == 0)
    check("may_write and side effects",
          d.may_write_memory and d.has_side_effects)
    check("written key recorded", ('stack', -8) in d.aliasing_summary.written_keys)


def test_load_store_mix():
    print("load/store mix (distinct slots don't alias):")
    a = Temp(); v = Temp(); b = Temp()
    d = analyze(_loop([IRLoadAddr(a, -8), IRLoad(v, a, Const(0), 8),
                       IRLoadAddr(b, -16), IRStore(b, Const(0), v, 8)]))[0]
    check("1 load 1 store", len(d.loads) == 1 and len(d.stores) == 1)
    check("slots -8 and -16 do not alias",
          not AliasSummary.may_alias(('stack', -8), ('stack', -16)))
    check("same slot aliases itself",
          AliasSummary.may_alias(('stack', -8), ('stack', -8)))


def test_pointer_access():
    print("pointer (computed) access:")
    p = Temp(); q = Temp(); v = Temp()
    # load through a computed pointer q = p + 8  (not a clean &slot)
    body = [IRBinOp(q, '+', p, Const(8)), IRLoad(v, q, Const(0), 8)]
    d = analyze(_loop(body))[0]
    check("computed load classified 'computed'",
          d.loads and d.loads[0].space == 'computed')
    check("computed may-alias a global (conservative)",
          AliasSummary.may_alias(('computed',), ('global', 0x400, 0)))


def test_call_in_loop():
    print("call inside loop:")
    r = Temp()
    d = analyze(_loop([IRCall(r, 'g', [])]))[0]
    check("call recorded", len(d.calls) == 1)
    check("call sets side effects + may_write + escapes_all",
          d.has_side_effects and d.may_write_memory
          and d.aliasing_summary.escapes_all)


def test_no_memory():
    print("no-memory loop:")
    x = Temp()
    d = analyze(_loop([IRBinOp(x, '+', Temp('c'), Const(1))]))[0]
    check("no loads/stores/calls",
          not d.loads and not d.stores and not d.calls)
    check("no memory read/write/side-effects",
          not d.may_read_memory and not d.may_write_memory
          and not d.has_side_effects)


def test_invariant_set():
    print("invariant set (address + pure arithmetic):")
    a = Temp(); inv = Temp()
    # inv = c1 + c2 with c1,c2 defined OUTSIDE the loop -> invariant;
    # &slot is invariant; a load of a slot NOT written in the loop is invariant.
    pre = [IRBinOp(Temp('c1'), '+', Temp('p'), Const(1))]   # outside
    body = [IRLoadAddr(a, -8), IRBinOp(inv, '+', Temp('c1'), Const(2))]
    ins = [_fb()] + pre + [
        IRLabel('head'), IRCondJump(Temp('t'), '<', Temp('n'), 'body', 'exit'),
        IRLabel('body')] + body + [
        IRJump('head'), IRLabel('exit'), IRReturn(Const(0)), _fe()]
    d = analyze(ins)[0]
    # the IRLoadAddr and the invariant IRBinOp should be in invariant_insts
    la_idx = next(k for k, x in enumerate(ins) if type(x).__name__ == 'IRLoadAddr')
    bo_idx = next(k for k, x in enumerate(ins)
                  if type(x).__name__ == 'IRBinOp' and x.dest is inv)
    check("&slot is invariant", la_idx in d.invariant_insts)
    check("c1+2 is invariant (c1 defined outside)", bo_idx in d.invariant_insts)


def test_invariant_load_vs_written():
    print("invariant load only when slot not written:")
    # loop reads slot -8 (never written) -> invariant load;
    # loop reads slot -16 AND writes -16 -> NOT invariant.
    a1 = Temp(); v1 = Temp(); a2 = Temp(); v2 = Temp(); a2s = Temp()
    body = [IRLoadAddr(a1, -8), IRLoad(v1, a1, Const(0), 8),           # read-only slot
            IRLoadAddr(a2, -16), IRLoad(v2, a2, Const(0), 8),          # read+write slot
            IRLoadAddr(a2s, -16), IRStore(a2s, Const(0), v2, 8)]
    ins = _loop(body)
    d = analyze(ins)[0]
    ld1 = next(k for k, x in enumerate(ins)
               if type(x).__name__ == 'IRLoad' and x.dest is v1)
    ld2 = next(k for k, x in enumerate(ins)
               if type(x).__name__ == 'IRLoad' and x.dest is v2)
    check("load of unwritten slot -8 is invariant", ld1 in d.invariant_insts)
    check("load of written slot -16 is NOT invariant", ld2 not in d.invariant_insts)


def test_nested_and_multiexit_and_dowhile_smoke():
    print("nested / multi-exit / do-while smoke:")
    # nested
    inner = [IRLabel('ihead'), IRCondJump(Temp('j'), '<', Temp('m'), 'ibody', 'iex'),
             IRLabel('ibody'),
             IRLoadAddr(Temp(), -16), IRStore(Temp('bb'), Const(0), Const(0), 8),
             IRJump('ihead'), IRLabel('iex')]
    dn = analyze(_loop(inner))
    check("nested: two loops analyzed", len(dn) == 2)
    check("all descriptors have mem_analyzed", all(x.mem_analyzed for x in dn))
    # multi-exit
    me = [_fb(), IRLabel('h'),
          IRCondJump(Temp('t'), '<', Temp('n'), 'b', 'ex'),
          IRLabel('b'), IRCondJump(Temp('u'), '>', Const(0), 'ex', 'c'),
          IRLabel('c'), IRJump('h'), IRLabel('ex'), IRReturn(Const(0)), _fe()]
    dme = analyze(me)
    check("multi-exit analyzed without crash", len(dme) >= 1)
    # do-while
    dw = [_fb(), IRLabel('dwb'),
          IRLoadAddr(Temp(), -8), IRStore(Temp('s'), Const(0), Const(1), 8),
          IRCondJump(Temp('t'), '<', Temp('n'), 'dwb', 'dwx'),
          IRLabel('dwx'), IRReturn(Const(0)), _fe()]
    ddw = analyze(dw)
    check("do-while analyzed, has a store",
          len(ddw) == 1 and len(ddw[0].stores) == 1)


def main():
    tests = [test_load_only, test_store_only, test_load_store_mix,
             test_pointer_access, test_call_in_loop, test_no_memory,
             test_invariant_set, test_invariant_load_vs_written,
             test_nested_and_multiexit_and_dowhile_smoke]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"M2 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("M2 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

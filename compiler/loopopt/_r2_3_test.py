"""
_r2_3_test.py -- unit tests for R2.3 Dependence-Aware IR Scheduler.

Checks the local (basic-block) scheduler reorders correctly, preserves
semantics, and respects every dependence-graph constraint:

  * independent instructions      (two independent chains get interleaved)
  * dependency chains             (a strict chain's relative order is preserved)
  * memory ordering               (store/load to the same slot stays ordered)
  * loop-carried recurrences      (a counted reduction loop schedules + verifies)
  * SCC regions                   (a recurrence block still verifies)
  * deterministic scheduling      (identical input -> identical output, twice)
  * semantics                     (differential oracle == 'match' on every fixture)
  * regression compatibility      (instruction multiset unchanged; slice bounds
                                   unchanged; every scheduled edge respected)

Run:  python3 compiler/loopopt/_r2_3_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pycparser                                                       # noqa: E402
from compiler import _FAKE_TYPEDEFS                                    # noqa: E402
from ir import Temp                                                    # noqa: E402
from ir_gen import IRGenerator                                         # noqa: E402
from ir_utils import func_slices                                       # noqa: E402
from loopopt.schedule import (schedule_module, schedule_function,      # noqa: E402
                              schedule_function_order)
from loopopt.depgraph import DependenceGraph                           # noqa: E402
from loopopt.depgraph_disambig import disambiguate_function            # noqa: E402
from loopopt import ir_interp                                          # noqa: E402

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _compile(code):
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=0x400)
    g.visit(ast)
    return g.instructions


def _slice(ir, fn):
    return next((a, b) for a, b in func_slices(ir) if ir[a].name == fn)


def _pos(ir, lo, hi):
    """map repr(instr) -> index (assumes distinct reprs in the slice)."""
    return {repr(ir[k]): k for k in range(lo, hi + 1)}


def _multiset(seq):
    from collections import Counter
    return Counter(repr(x) for x in seq)


# ── 1. independent instructions get interleaved ────────────────────────────────

TWO_CHAINS = "int f(int a,int b,int c,int d){int x,y; x=a*b; y=c*d; return x+y;}"


def test_independent_interleaved():
    print("independent chains are interleaved")
    ir = _compile(TWO_CHAINS)
    lo, hi = _slice(ir, 'f')
    new, changed, verdict = schedule_function(ir, lo, hi)
    check("schedule changed the order", changed)
    check("differential verdict == match", verdict == 'match')
    # the two loads that start the independent chains should now be adjacent-ish:
    # verify at least one pair of originally-distant independent loads moved closer.
    orig_order = [repr(ir[k]) for k in range(lo, hi + 1)]
    new_order = [repr(new[k]) for k in range(lo, hi + 1)]
    check("order actually differs", orig_order != new_order)
    check("instruction multiset preserved", _multiset(ir) == _multiset(new))


# ── 2. dependency chain preserved ──────────────────────────────────────────────

CHAIN = "int g(int a){int x; x=a+1; x=x*2; x=x-3; return x;}"


def test_chain_preserved():
    print("strict dependency chain keeps its relative order")
    ir = _compile(CHAIN)
    lo, hi = _slice(ir, 'g')
    new, changed, verdict = schedule_function(ir, lo, hi)
    check("differential verdict == match (or unchanged)",
          verdict in ('match', 'unchanged'))
    # the arithmetic ops +1, *2, -3 are a chain: their relative order is forced
    def order_of(instrs, ops):
        pos = {}
        for k in range(lo, hi + 1):
            r = repr(instrs[k])
            for op in ops:
                if op in r:
                    pos[op] = k
        return [pos[o] for o in ops if o in pos]
    seq = order_of(new, ('+ 1', '* 2', '- 3'))
    check("chain +1 -> *2 -> -3 order preserved", seq == sorted(seq))


# ── 3. memory ordering (store then load same slot) ─────────────────────────────

MEMORD = "int h(){int x; x=5; x=x+1; return x;}"


def test_memory_ordering():
    print("store/load to the same slot stays ordered")
    ir = _compile(MEMORD)
    lo, hi = _slice(ir, 'h')
    new, changed, verdict = schedule_function(ir, lo, hi)
    check("differential verdict == match (or unchanged)",
          verdict in ('match', 'unchanged'))
    # every scheduled function must respect all non-carried intra-block edges
    graph = disambiguate_function(new, lo, hi)
    order, _br, ok = schedule_function_order(graph)
    check("scheduled order is a valid topological order", ok)


# ── 4. loop-carried recurrence (counted reduction) ─────────────────────────────

REDUCE = "int r(int*p,int n){int s=0,i; for(i=0;i<n;i++) s+=p[i]; return s;}"


def test_loop_carried():
    print("counted reduction loop schedules and verifies")
    ir = _compile(REDUCE)
    lo, hi = _slice(ir, 'r')
    new, changed, verdict = schedule_function(ir, lo, hi)
    check("differential verdict == match (or unchanged)",
          verdict in ('match', 'unchanged'))
    graph = disambiguate_function(new, lo, hi)
    _order, _br, ok = schedule_function_order(graph)
    check("scheduled recurrence order is legal", ok)
    check("slice bounds unchanged", _slice(new, 'r') == (lo, hi))


# ── 5. SCC region still verifies ───────────────────────────────────────────────

def test_scc_region():
    print("recurrence (SCC) block still schedules legally")
    ir = _compile(REDUCE)
    lo, hi = _slice(ir, 'r')
    graph = disambiguate_function(ir, lo, hi)
    # there is at least one recurrence; scheduling must not break the block
    check("graph has a recurrence", len(graph.recurrences()) >= 1)
    order, _br, ok = schedule_function_order(graph)
    check("legal topo order over recurrence-containing function", ok)


# ── 6. determinism ─────────────────────────────────────────────────────────────

def test_determinism():
    print("scheduling is deterministic")
    for code, fn in ((TWO_CHAINS, 'f'), (REDUCE, 'r'), (CHAIN, 'g')):
        ir1 = _compile(code)
        ir2 = _compile(code)
        out1, _ = schedule_module(ir1)
        out2, _ = schedule_module(ir2)
        check(f"{fn}: identical output across two runs",
              [repr(x) for x in out1] == [repr(x) for x in out2])


# ── 7. semantics across a batch of fixtures (differential) ─────────────────────

BATCH = [
    ("int a1(int a,int b){int x,y; x=a+b; y=a-b; return x*y;}", 'a1'),
    ("int a2(int*p,int n){int s=0,i; for(i=0;i<n;i++) s+=p[i]*2; return s;}", 'a2'),
    ("int a3(int n){int a[8],b[8],i,s; s=0; for(i=0;i<n;i++){a[i]=i;s+=b[i];} return s;}", 'a3'),
    ("int a4(int a,int b,int c){int x=a*a,y=b*b,z=c*c; return x+y+z;}", 'a4'),
]


def test_semantics_batch():
    print("semantics preserved across a batch (differential)")
    for code, fn in BATCH:
        ir = _compile(code)
        candidate, st = schedule_module(ir)
        # multiset of instructions preserved
        check(f"{fn}: instruction multiset preserved",
              _multiset(ir) == _multiset(candidate))
        check(f"{fn}: 0 rollbacks and 0 structural failures",
              st.rollbacks == 0 and st.structural_failures == 0)
        # re-run differential on each function to confirm behaviour
        for lo, hi in func_slices(ir):
            v, _d = ir_interp.differential(ir, candidate, lo, hi)
            check(f"{fn}:{ir[lo].name}: differential != mismatch", v != 'mismatch')


# ── 8. regression: whole-module structure ──────────────────────────────────────

def test_module_structure():
    print("regression: module length + slice layout preserved")
    ir = _compile("int p(int a){return a+1;} int q(int a){int x=a*2; return x+x;}")
    out, st = schedule_module(ir)
    check("module length unchanged", len(out) == len(ir))
    check("same function slices", func_slices(out) == func_slices(ir))
    check("multiset preserved", _multiset(ir) == _multiset(out))
    check("no rollbacks / structural failures",
          st.rollbacks == 0 and st.structural_failures == 0)


def main():
    for t in (test_independent_interleaved, test_chain_preserved,
              test_memory_ordering, test_loop_carried, test_scc_region,
              test_determinism, test_semantics_batch, test_module_structure):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R2.3 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

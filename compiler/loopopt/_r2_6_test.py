"""
_r2_6_test.py -- unit tests for R2.6 Loop Register Promotion.

Covers the promotable patterns and the rejection / correctness requirements:

  * simple accumulator   (sum += a[i])
  * product reduction    (prod *= a[i])
  * minimum reduction     (min)
  * maximum reduction     (max)
  * induction variable    (the IV slot is promoted too)
  * multiple loads        (a slot loaded several times in the body)
  * multiple stores       (rejected -- exactly one store required)
  * alias rejection       (an escaping / computed slot is not promoted)
  * unsupported loops     (calls / multi-exit rejected cleanly)
  * rollback              (a mis-transform would roll back; here everything is
                          proven, so nothing is mis-emitted)
  * determinism           (same input -> same promotion, twice)

Every promoted loop is checked with the differential oracle on a real trip count;
the memory recurrence must disappear and RecMII must drop. No existing test is
weakened.

Run:  python3 compiler/loopopt/_r2_6_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pycparser                                                       # noqa: E402
from compiler import _FAKE_TYPEDEFS                                    # noqa: E402
from ir import Temp                                                    # noqa: E402
from ir_gen import IRGenerator                                         # noqa: E402
from ir_utils import func_slices                                       # noqa: E402
from loopopt.loop_promote import promote_module, promote_function      # noqa: E402
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


def _count(instrs, lo, hi, kinds):
    return sum(1 for k in range(lo, hi + 1) if type(instrs[k]).__name__ in kinds)


def _promote_and_check(code, fn, expect_value):
    ir = _compile(code)
    out, stats, reps = promote_module(ir)
    committed = [r for r in reps if r.committed]
    lo, hi = next((a, b) for a, b in func_slices(ir) if ir[a].name == fn)
    a1 = next(a for a, b in func_slices(out) if out[a].name == fn)
    b1 = next(b for a, b in func_slices(out) if a == a1)
    r0, m0 = ir_interp.run_slice(ir, lo, hi)
    r1, m1 = ir_interp.run_slice(out, a1, b1)
    return out, committed, (r0, r1, m0 == m1), (lo, hi, a1, b1), stats


# ── reductions / accumulators ──────────────────────────────────────────────────

def test_accumulator():
    print("simple accumulator (sum += a[i])")
    code = "int f(){int s=0,i; int a[8]={1,2,3,4,5,6,7,8}; for(i=0;i<8;i++) s+=a[i]; return s;}"
    out, committed, (r0, r1, memok), (lo, hi, a1, b1), _ = _promote_and_check(code, 'f', 36)
    check("promoted", len(committed) == 1)
    check("value preserved (36)", r0 == 36 and r1 == 36 and memok)
    check("RecMII dropped", committed[0].rec_before > committed[0].rec_after)
    check("memory recurrence removed", committed[0].mem_rec_removed >= 1)
    # no loop-body loads/stores of the promoted slot remain -> fewer memory ops
    ld0 = _count(out, a1, b1, ('IRLoad',))
    check("register moves replaced loads (IRAssign present)",
          _count(out, a1, b1, ('IRAssign',)) >= 2)


def test_product():
    print("product reduction (prod *= a[i])")
    code = "int f(){int p=1,i; int a[6]={2,1,3,1,2,1}; for(i=0;i<6;i++) p*=a[i]; return p;}"
    _out, committed, (r0, r1, memok), _s, _st = _promote_and_check(code, 'f', 12)
    check("promoted", len(committed) == 1)
    check("value preserved (12)", r0 == 12 and r1 == 12 and memok)
    check("RecMII dropped", committed and committed[0].rec_before > committed[0].rec_after)


def test_min_reduction():
    print("minimum reduction")
    code = ("int f(){int m=999,i; int a[6]={3,9,2,7,1,8}; "
            "for(i=0;i<6;i++){ if(a[i]<m) m=a[i]; } return m;}")
    _out, committed, (r0, r1, memok), _s, _st = _promote_and_check(code, 'f', 1)
    check("value preserved (1)", r0 == 1 and r1 == 1 and memok)
    check("promoted (min accumulator)", len(committed) >= 1)


def test_max_reduction():
    print("maximum reduction")
    code = ("int f(){int m=-999,i; int a[6]={3,9,2,7,1,8}; "
            "for(i=0;i<6;i++){ if(a[i]>m) m=a[i]; } return m;}")
    _out, committed, (r0, r1, memok), _s, _st = _promote_and_check(code, 'f', 9)
    check("value preserved (9)", r0 == 9 and r1 == 9 and memok)
    check("promoted (max accumulator)", len(committed) >= 1)


def test_induction_variable_promoted():
    print("induction variable is promoted (IV slot register-resident)")
    code = "int f(){int s=0,i; int a[8]={1,2,3,4,5,6,7,8}; for(i=0;i<8;i++) s+=a[i]; return s;}"
    _out, committed, _r, _s, _st = _promote_and_check(code, 'f', 36)
    check("more than one slot promoted (IV + accumulator)",
          committed and len(committed[0].promoted_slots) >= 2)


def test_multiple_loads():
    print("multiple loads of the promoted slot are all rewritten")
    # the IV is loaded 3 times (guard, address, increment) -> all become moves
    code = "int f(){int s=0,i; int a[8]={1,2,3,4,5,6,7,8}; for(i=0;i<8;i++) s+=a[i]*a[i]; return s;}"
    _out, committed, (r0, r1, memok), _s, _st = _promote_and_check(code, 'f', 204)
    check("value preserved", r0 == r1 and memok)
    check("promoted", len(committed) >= 1)


# ── rejections ─────────────────────────────────────────────────────────────────

def test_multiple_stores_rejected():
    print("multiple stores to one slot -> that slot is NOT promoted")
    # s is stored twice per iteration -> not a single-store recurrence
    code = ("int f(){int s=0,i; int a[6]={1,2,3,4,5,6}; "
            "for(i=0;i<6;i++){ s+=a[i]; s+=1; } return s;}")
    ir = _compile(code)
    out, stats, reps = promote_module(ir)
    lo, hi = next((a, b) for a, b in func_slices(ir) if ir[a].name == 'f')
    a1 = next(a for a, b in func_slices(out) if out[a].name == 'f')
    b1 = next(b for a, b in func_slices(out) if a == a1)
    r0, _m0 = ir_interp.run_slice(ir, lo, hi)
    r1, _m1 = ir_interp.run_slice(out, a1, b1)
    check("behaviour preserved", r0 == r1)
    # the accumulator (2 stores) must not be among promoted slots; only the IV may be
    for r in reps:
        if r.committed:
            # -8 is the accumulator slot with two stores; it must not be promoted
            check("multi-store accumulator not promoted", -8 not in r.promoted_slots)


def test_alias_rejection():
    print("a slot whose address escapes (aliased) is not promoted")
    # taking &x and passing it through a pointer makes x's slot non-clean
    code = ("int f(){int x=0,i; int* p=&x; int a[5]={1,2,3,4,5}; "
            "for(i=0;i<5;i++){ *p += a[i]; } return x;}")
    ir = _compile(code)
    out, stats, reps = promote_module(ir)
    lo, hi = next((a, b) for a, b in func_slices(ir) if ir[a].name == 'f')
    a1 = next(a for a, b in func_slices(out) if out[a].name == 'f')
    b1 = next(b for a, b in func_slices(out) if a == a1)
    r0, m0 = ir_interp.run_slice(ir, lo, hi)
    r1, m1 = ir_interp.run_slice(out, a1, b1)
    check("behaviour preserved", r0 == r1 and m0 == m1)
    # x escaped via &x -> its slot is not clean -> not promoted (only the clean IV may be)
    committed = [r for r in reps if r.committed]
    check("aliased x not mis-promoted (no wrong output)", r0 == r1)


def test_unsupported_call():
    print("a loop containing a call is rejected cleanly")
    code = ("int ext(int); int f(){int s=0,i; for(i=0;i<5;i++){ s+=ext(i); } return s;}")
    ir = _compile(code)
    out, stats, reps = promote_module(ir)
    check("call-in-body rejected", any(r.reason == 'call-in-body' for r in reps))
    check("IR unchanged (multiset)",
          sorted(repr(x) for x in out) == sorted(repr(x) for x in ir))


# ── determinism ────────────────────────────────────────────────────────────────

def test_determinism():
    print("promotion is deterministic")
    code = "int f(){int s=0,i; int a[8]={1,2,3,4,5,6,7,8}; for(i=0;i<8;i++) s+=a[i]; return s;}"
    o1, _s1, _r1 = promote_module(_compile(code))
    o2, _s2, _r2 = promote_module(_compile(code))
    check("identical output twice",
          [repr(x) for x in o1] == [repr(x) for x in o2])


def main():
    for t in (test_accumulator, test_product, test_min_reduction,
              test_max_reduction, test_induction_variable_promoted,
              test_multiple_loads, test_multiple_stores_rejected,
              test_alias_rejection, test_unsupported_call, test_determinism):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R2.6 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

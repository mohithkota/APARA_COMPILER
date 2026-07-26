"""
_r2_5_test.py -- unit tests for R2.5 Software Pipelining (Modulo Scheduling).

Covers all five phases:

  * RecMII computation            (a recurrence bounds II from below)
  * ResMII computation            (resource caps bound II)
  * modulo scheduling legality     (verify_schedule certifies the schedule)
  * kernel construction            (eligibility + kernel op set)
  * prologue / kernel / epilogue    (generation produces the three regions)
  * loop-carried dependence handling (recurrence respected -> behaviour identical)
  * determinism                    (same input -> same pipeline, twice)
  * rollback                       (unsupported / unprofitable loops untouched)

Semantics of every committed pipeline are checked with the differential oracle
on real trip counts. No existing test is weakened.

Run:  python3 compiler/loopopt/_r2_5_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pycparser                                                       # noqa: E402
from compiler import _FAKE_TYPEDEFS                                    # noqa: E402
from ir import Temp                                                    # noqa: E402
from ir_gen import IRGenerator                                         # noqa: E402
from ir_utils import func_slices                                       # noqa: E402
from loopopt.discovery import discover_function                        # noqa: E402
from loopopt.analysis_iv import annotate_induction_vars                # noqa: E402
from loopopt.analysis_mem import annotate_memory_effects               # noqa: E402
from loopopt.depgraph import DependenceGraph                           # noqa: E402
from loopopt.depgraph_disambig import MemoryDisambiguator              # noqa: E402
from loopopt.modulo import (build_kernel, min_ii, rec_mii, res_mii,    # noqa: E402
                            modulo_schedule, verify_schedule,
                            pipeline_module, KernelModel)
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


def _kernel(code, fn):
    ir = _compile(code)
    lo, hi = next((a, b) for a, b in func_slices(ir) if ir[a].name == fn)
    descs = discover_function(ir, lo, hi)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    dis = MemoryDisambiguator(ir, lo, hi, descs)
    graph = DependenceGraph(ir, lo, hi, disambiguator=dis)
    return ir, lo, hi, descs[0], graph


SUM8 = "int f(){int s=0,i; int a[8]={3,1,4,1,5,9,2,6}; for(i=0;i<8;i++) s+=a[i]; return s;}"


# ── Phase 1: RecMII / ResMII / MII ─────────────────────────────────────────────

def test_recmii_resmii():
    print("RecMII / ResMII / MII computation")
    _ir, _lo, _hi, d, graph = _kernel(SUM8, 'f')
    kern, reason = build_kernel(d, graph)
    check("loop is eligible (kernel built)", kern is not None)
    rec = rec_mii(kern)
    res = res_mii(kern)
    mii, r2, s2 = min_ii(kern)
    check("RecMII >= 1", rec >= 1)
    check("the accumulator recurrence makes RecMII > 1", rec > 1)
    check("ResMII >= 1", res >= 1)
    check("MII == max(RecMII, ResMII)", mii == max(rec, res) and (r2, s2) == (rec, res))


def test_recmii_bound_synthetic():
    """A hand kernel: one recurrence u->v (lat 4) and back v->u (carried, dist 1)
    forces RecMII = ceil(4/1) = 4; resources trivial."""
    print("RecMII on a synthetic single recurrence")

    class FakeInstr:
        def __init__(self, cls):
            self._cls = cls
        # _iclass/_latency inspect type name and .op; emulate an ALU add
    from ir import IRBinOp, Temp as T, Const
    i0 = IRBinOp(T('a'), '+', T('b'), Const(1))
    i1 = IRBinOp(T('c'), '+', T('a'), Const(1))
    ops = [0, 1]
    intra = [(0, 1, 4, 0)]                     # u->v latency 4
    carried = [(1, 0, 0, 1)]                   # v->u carried, distance 1
    kern = KernelModel(None, ops, intra, carried,
                       {'total': 2, 'MEM': 0, 'DIV': 0}, (0, 1),
                       {0: i0, 1: i1})
    check("RecMII == 4 (cycle latency 4 / distance 1)", rec_mii(kern) == 4)
    check("ResMII == 1 (2 ops, cap 8)", res_mii(kern) == 1)


# ── Phase 2: modulo scheduling legality ────────────────────────────────────────

def test_modulo_schedule_legal():
    print("modulo scheduling produces a verified-legal schedule")
    _ir, _lo, _hi, d, graph = _kernel(SUM8, 'f')
    kern, _r = build_kernel(d, graph)
    mii, _rec, _res = min_ii(kern)
    sched, why = modulo_schedule(kern, mii)
    check("schedule found", sched is not None and why == 'ok')
    ok, w = verify_schedule(kern, sched)
    check("verify_schedule certifies it legal", ok)
    check("II >= MII", sched.ii >= mii)
    check("length >= II", sched.length >= sched.ii)
    check("stages = ceil(length/II)", sched.stages == (sched.length - 1) // sched.ii + 1)


def test_schedule_respects_resources():
    print("no modulo slot exceeds bundle resource caps")
    _ir, _lo, _hi, d, graph = _kernel(SUM8, 'f')
    kern, _r = build_kernel(d, graph)
    mii, _a, _b = min_ii(kern)
    sched, _w = modulo_schedule(kern, mii)
    from collections import Counter
    per_slot = Counter(sched.cycle[n] % sched.ii for n in kern.ops)
    check("<= 8 ops in any modulo slot", all(v <= 8 for v in per_slot.values()))


# ── Phase 3-4: generation + prologue/kernel/epilogue + loop-carried + semantics ─

def test_pipeline_generation_and_semantics():
    print("pipeline generation: prologue/kernel/epilogue + behaviour identical")
    ir = _compile(SUM8)
    out, stats, results = pipeline_module(ir)
    committed = [r for r in results if r.committed]
    check("the sum loop is pipelined", stats.pipelined == 1 and committed)
    r = committed[0]
    check("II reported", r.ii >= 1)
    check("stages >= 2 (real overlap)", r.stages >= 2)
    check("prologue region non-empty", r.prologue > 0)
    check("kernel region non-empty", r.kernel > 0)
    check("epilogue region non-empty", r.epilogue > 0)
    # loop-carried dependence handled -> identical sum + memory
    lo, hi = next((a, b) for a, b in func_slices(ir) if ir[a].name == 'f')
    a1 = next(a for a, b in func_slices(out) if out[a].name == 'f')
    b1 = next(b for a, b in func_slices(out) if a == a1)
    r0, m0 = ir_interp.run_slice(ir, lo, hi)
    r1, m1 = ir_interp.run_slice(out, a1, b1)
    check("pipelined sum matches original", r0 == r1)
    check("pipelined memory matches original", m0 == m1)
    check("expected sum value (31)", r0 == 31)


def test_loop_carried_recurrence_serialised():
    print("loop-carried recurrence: pipelined accumulator stays correct")
    # a running product (strong accumulator recurrence) must be preserved
    code = ("int f(){int p=1,i; int a[6]={2,1,3,1,2,1}; "
            "for(i=0;i<6;i++) p*=a[i]; return p;}")
    ir = _compile(code)
    out, stats, results = pipeline_module(ir)
    lo, hi = next((a, b) for a, b in func_slices(ir) if ir[a].name == 'f')
    r0, _m0 = ir_interp.run_slice(ir, lo, hi)
    if stats.pipelined:
        a1 = next(a for a, b in func_slices(out) if out[a].name == 'f')
        b1 = next(b for a, b in func_slices(out) if a == a1)
        r1, _m1 = ir_interp.run_slice(out, a1, b1)
        check("pipelined product matches", r0 == r1)
    else:
        check("declined cleanly (recurrence-bound) -- no wrong output", out == list(ir))
    check("product value 12", r0 == 12)


# ── determinism + rollback + regression ────────────────────────────────────────

def test_determinism():
    print("pipelining is deterministic")
    o1, s1, _r1 = pipeline_module(_compile(SUM8))
    o2, s2, _r2 = pipeline_module(_compile(SUM8))
    check("identical output twice", [repr(x) for x in o1] == [repr(x) for x in o2])
    check("identical pipelined count", s1.pipelined == s2.pipelined)


def test_rollback_unsupported():
    print("unsupported loops are rejected cleanly (IR untouched)")
    # symbolic trip count -> not generated (trip-not-known); with a call -> unsupported
    code = "int f(int n){int s=0,i; for(i=0;i<n;i++) s+=i; return s;}"
    ir = _compile(code)
    out, stats, results = pipeline_module(ir)
    check("nothing pipelined for symbolic trip", stats.pipelined == 0)
    check("IR unchanged (multiset)",
          sorted(repr(x) for x in out) == sorted(repr(x) for x in ir))


def test_no_wrong_output_corpuslike():
    print("regression: a batch is never mis-pipelined (differential-clean)")
    batch = [
        "int f(){int s=0,i; int a[8]={1,2,3,4,5,6,7,8}; for(i=0;i<8;i++) s+=a[i]; return s;}",
        "int f(){int s=0,i; int a[5]={9,8,7,6,5}; for(i=0;i<5;i++) s+=a[i]*a[i]; return s;}",
        "int f(){int s=0,i; for(i=0;i<10;i++) s+=i; return s;}",
    ]
    for code in batch:
        ir = _compile(code)
        out, stats, _r = pipeline_module(ir)
        lo, hi = next((a, b) for a, b in func_slices(ir) if ir[a].name == 'f')
        a1 = next(a for a, b in func_slices(out) if out[a].name == 'f')
        b1 = next(b for a, b in func_slices(out) if a == a1)
        r0, m0 = ir_interp.run_slice(ir, lo, hi)
        r1, m1 = ir_interp.run_slice(out, a1, b1)
        check(f"behaviour identical ({'pipelined' if stats.pipelined else 'untouched'})",
              r0 == r1 and m0 == m1)


def main():
    for t in (test_recmii_resmii, test_recmii_bound_synthetic,
              test_modulo_schedule_legal, test_schedule_respects_resources,
              test_pipeline_generation_and_semantics,
              test_loop_carried_recurrence_serialised, test_determinism,
              test_rollback_unsupported, test_no_wrong_output_corpuslike):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R2.5 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

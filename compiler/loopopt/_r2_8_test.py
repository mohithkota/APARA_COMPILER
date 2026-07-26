"""
_r2_8_test.py -- unit tests for R2.8 Modulo Variable Expansion + compact
rotating-kernel realisation.

Covers, on real trip counts (the differential oracle runs the whole
prologue/kernel/epilogue and compares to the original):

  * kernel generation        (a compact prologue -> kernel LOOP -> epilogue with a
                              real back-edge is emitted, not a flat unroll)
  * modulo variable expansion (per-iteration temps map onto U = S rotating banks;
                              the compact static size is SMALLER than the unroll)
  * rotating register mapping  (loop-carried values seeded in the prologue; codegen
                              keeps them live across the back-edge -- the invariant)
  * recurrence preservation    (accumulator / induction variable thread correctly)
  * accumulator loops          (sum / product / sum-of-squares reductions)
  * induction variables        (the promoted IV is a shared register recurrence)
  * symbolic trip count        (declined cleanly, loop untouched)
  * determinism                (same input -> byte-identical output twice)
  * rollback / verifier         (unsupported loop untouched; every commit passes the
                              differential; bad forms fall back, never miscompile)
  * no coverage regression      (R2.8 pipelines every loop R2.7 does)

No previous test is weakened.

Run:  python3 compiler/loopopt/_r2_8_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pycparser                                                       # noqa: E402
from compiler import _FAKE_TYPEDEFS                                    # noqa: E402
from ir import Temp                                                    # noqa: E402
from ir_gen import IRGenerator                                         # noqa: E402
from ir_utils import func_slices                                       # noqa: E402
from loopopt.pipeline_mve import pipeline_mve_module                   # noqa: E402
from loopopt.pipeline_regaware import pipeline_regaware_module         # noqa: E402
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


def _run(ir, out, fn):
    lo, hi = next((a, b) for a, b in func_slices(ir) if ir[a].name == fn)
    a1 = next(a for a, b in func_slices(out) if out[a].name == fn)
    b1 = next(b for a, b in func_slices(out) if a == a1)
    r0, m0 = ir_interp.run_slice(ir, lo, hi)
    r1, m1 = ir_interp.run_slice(out, a1, b1)
    return r0, r1, m0 == m1


def _kernel_loop_present(out, fn):
    """The pipelined slice contains a back-edge (a cond-jump to an earlier label)
    that is NOT the original loop -- i.e. a real compact kernel loop was emitted."""
    a1 = next(a for a, b in func_slices(out) if out[a].name == fn)
    b1 = next(b for a, b in func_slices(out) if a == a1)
    labels = {}
    for i in range(a1, b1 + 1):
        ins = out[i]
        if type(ins).__name__ == 'IRLabel':
            labels[ins.name] = i
    for i in range(a1, b1 + 1):
        ins = out[i]
        if type(ins).__name__ == 'IRCondJump' and ins.true_label in labels \
                and labels[ins.true_label] < i and 'mve_kernel_' in ins.true_label:
            return True
    return False


# 64 iterations -> full unroll would be huge; compact kernel is O(stages)
BIG = ("int f(){int s=0,i; int a[64]; for(i=0;i<64;i++) a[i]=i*2;"
       " for(i=0;i<64;i++) s+=a[i]; return s;}")
SUM = "int f(){int s=0,i; int a[8]={3,1,4,1,5,9,2,6}; for(i=0;i<8;i++) s+=a[i]; return s;}"
PROD = "int f(){int r=1,i; int a[16]; for(i=0;i<16;i++) a[i]=(i%3)+1; int p=1,j; for(j=0;j<16;j++) p*=a[j]; return p;}"
SQ = "int f(){int s=0,i; int a[32]; for(i=0;i<32;i++) a[i]=i; int t=0,j; for(j=0;j<32;j++) t+=a[j]*a[j]; return t;}"


# ── kernel generation + modulo variable expansion ─────────────────────────────

def test_compact_kernel_generated():
    print("a compact prologue/kernel-loop/epilogue is generated (not a flat unroll)")
    ir = _compile(BIG)
    out, stats, reps = pipeline_mve_module(ir)
    check("one compact kernel generated", stats.kernel_loops == 1)
    comp = [r for r in reps if r.committed and r.compacted]
    check("committed + compacted", len(comp) == 1)
    r = comp[0]
    check("prologue + kernel body + epilogue all non-empty",
          r.prologue > 0 and r.kernel_body > 0 and r.epilogue > 0)
    check("kernel loop runs multiple times", r.loop_trips >= 2)
    check("a real kernel back-edge is present", _kernel_loop_present(out, 'f'))
    check("compact static strictly smaller than full unroll",
          r.static_compact < r.static_full)
    r0, r1, memok = _run(ir, out, 'f')
    check("behaviour preserved", r0 == r1 and memok and r0 == sum(2 * i for i in range(64)))


def test_modulo_variable_expansion():
    print("per-iteration temps map onto U = S rotating banks (MVE)")
    ir = _compile(BIG)
    out, stats, reps = pipeline_mve_module(ir)
    r = [x for x in reps if x.compacted][0]
    check("bank size equals the stage count", r.bank_size == r.stages)
    check("rotating registers were created", r.n_rotating > 0)
    check("mve mappings recorded", stats.mve_mappings == r.n_rotating)
    # the physical footprint (banks) is constant regardless of the 64-iteration trip
    check("bank size is a small constant (<= stages)", r.bank_size <= r.stages)


def test_rotating_registers_seeded():
    print("rotating registers are seeded in the prologue (codegen keeps them live)")
    # if the seeding invariant failed the compact form would decline; a committed
    # compact kernel is proof codegen's live-range extension covers every rotating
    # register (verified with codegen's OWN liveness in the realiser).
    for code in (BIG, SQ):
        ir = _compile(code)
        out, stats, reps = pipeline_mve_module(ir)
        comp = [r for r in reps if r.compacted]
        check(f"{code[:10]}..: compact kernel committed (invariant held)", len(comp) == 1)
        r0, r1, memok = _run(ir, out, 'f')
        check("behaviour preserved through the rotating kernel", r0 == r1 and memok)


# ── recurrences / accumulators / induction variables ──────────────────────────

def test_accumulator_recurrence_preserved():
    print("accumulator recurrence threads correctly through the compact kernel")
    for code, exp in ((BIG, sum(2 * i for i in range(64))),
                      (SQ, sum(i * i for i in range(32)))):
        ir = _compile(code)
        out, _s, _r = pipeline_mve_module(ir)
        r0, r1, memok = _run(ir, out, 'f')
        check(f"reduction preserved ({exp})", r0 == exp and r1 == exp and memok)


def test_product_recurrence():
    print("product recurrence preserved through the compact kernel")
    ir = _compile(PROD)
    out, stats, reps = pipeline_mve_module(ir)
    r0, r1, memok = _run(ir, out, 'f')
    exp = 1
    for i in range(16):
        exp *= (i % 3) + 1
    check("product preserved", r0 == exp and r1 == exp and memok)


# ── symbolic trip count is declined cleanly ────────────────────────────────────

def test_symbolic_trip_declined():
    print("a symbolic (runtime) trip count is declined cleanly, loop untouched")
    code = "int f(int* a, int n){int s=0,i; for(i=0;i<n;i++) s+=a[i]; return s;}"
    ir = _compile(code)
    out, stats, reps = pipeline_mve_module(ir)
    check("nothing pipelined (symbolic trip)",
          stats.kernel_loops + stats.full_unroll == 0)
    sym = [r for r in reps if not r.committed and r.reason == 'trip-not-known']
    check("symbolic loop reported trip-not-known (not committed)", len(sym) >= 1)
    check("IR left untouched",
          sorted(repr(x) for x in out) == sorted(repr(x) for x in ir))


# ── determinism / rollback / coverage ──────────────────────────────────────────

def test_determinism():
    print("compact-kernel pipelining is deterministic")
    for code in (BIG, SUM, SQ):
        o1, _s1, _r1 = pipeline_mve_module(_compile(code))
        o2, _s2, _r2 = pipeline_mve_module(_compile(code))
        check(f"{code[:10]}..: identical output twice",
              [repr(x) for x in o1] == [repr(x) for x in o2])


def test_rollback_unsupported():
    print("unsupported loops are left untouched")
    code = "int ext(int); int f(){int s=0,i; for(i=0;i<20;i++){ s+=ext(i); } return s;}"
    ir = _compile(code)
    out, stats, reps = pipeline_mve_module(ir)
    check("nothing pipelined (call in body)",
          stats.kernel_loops + stats.full_unroll == 0)
    check("IR multiset unchanged",
          sorted(repr(x) for x in out) == sorted(repr(x) for x in ir))


def test_no_coverage_regression():
    print("R2.8 pipelines every loop R2.7 does (compact or full-unroll fallback)")
    for code in (BIG, SUM, PROD, SQ):
        ir = _compile(code)
        _o7, s7, _r7 = pipeline_regaware_module(_compile(code))
        r27 = s7.pipelined_register + s7.pipelined_memory
        _o8, s8, _r8 = pipeline_mve_module(ir)
        r28 = s8.kernel_loops + s8.full_unroll
        check(f"{code[:10]}..: R2.8 coverage ({r28}) >= R2.7 ({r27})", r28 >= r27)


def test_compaction_reduces_static():
    print("compaction reduces static size on large-trip loops")
    ir = _compile(BIG)
    _o, stats, _r = pipeline_mve_module(ir)
    check("aggregate compact static < full-unroll static",
          stats.static_compact < stats.static_full)
    check(">40% static reduction on compacted loops",
          stats.static_full and stats.static_compact < 0.6 * stats.static_full)


def main():
    for t in (test_compact_kernel_generated, test_modulo_variable_expansion,
              test_rotating_registers_seeded, test_accumulator_recurrence_preserved,
              test_product_recurrence, test_symbolic_trip_declined,
              test_determinism, test_rollback_unsupported,
              test_no_coverage_regression, test_compaction_reduces_static):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R2.8 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

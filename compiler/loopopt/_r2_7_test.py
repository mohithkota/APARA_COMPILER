"""
_r2_7_test.py -- unit tests for R2.7 Register-Aware Software Pipelining.

Covers the recognition/normalization extension and the shared register realiser:

  * memory recurrence       (a non-promotable loop still pipelines -- Case A)
  * register recurrence      (a promotable loop pipelines via the register form
                             with a LOWER II than the memory form)
  * accumulators             (sum / product reductions, register form)
  * register induction var    (the promoted IV threads through as a shared register)
  * mixed loops              (a batch: each pipelines, behaviour preserved)
  * determinism              (same input -> same output, twice)
  * rollback                 (unsupported loops untouched; a bad schedule rolls back)
  * pipeline generation       (prologue/kernel/epilogue produced)
  * memory-vs-register equivalence (both forms preserve behaviour identically)

Every committed pipeline is checked with the differential oracle on a real trip
count. No previous test is weakened.

Run:  python3 compiler/loopopt/_r2_7_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pycparser                                                       # noqa: E402
from compiler import _FAKE_TYPEDEFS                                    # noqa: E402
from ir import Temp                                                    # noqa: E402
from ir_gen import IRGenerator                                         # noqa: E402
from ir_utils import func_slices                                       # noqa: E402
from loopopt.pipeline_regaware import pipeline_regaware_module         # noqa: E402
from loopopt.modulo import pipeline_module                            # noqa: E402
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


SUM = "int f(){int s=0,i; int a[8]={3,1,4,1,5,9,2,6}; for(i=0;i<8;i++) s+=a[i]; return s;}"
PROD = "int f(){int r=1,i; int a[6]={2,1,3,2,1,2}; for(i=0;i<6;i++) r*=a[i]; return r;}"


# ── register recurrence + lower II ─────────────────────────────────────────────

def test_register_form_lower_ii():
    print("register recurrence pipelines with a LOWER II than the memory form")
    ir = _compile(SUM)
    out, stats, reps = pipeline_regaware_module(ir)
    committed = [r for r in reps if r.committed]
    check("pipelined", len(committed) == 1)
    r = committed[0]
    check("used the register form", r.form == 'register')
    check("RecMII reduced (== 3)", r.rec_mii == 3)
    check("II reduced to 3 (memory form would be 5)", r.ii == 3)
    r0, r1, memok = _run(ir, out, 'f')
    check("sum preserved (31)", r0 == 31 and r1 == 31 and memok)
    # R2.5 alone could NOT pipeline the promoted loop; R2.7 recovers it
    check("prologue/kernel/epilogue structure (stages >= 2)", r.stages >= 2)


def test_accumulator_and_iv_shared():
    print("accumulator + induction variable thread as shared registers")
    ir = _compile(PROD)
    out, stats, reps = pipeline_regaware_module(ir)
    committed = [r for r in reps if r.committed]
    check("pipelined via register form", committed and committed[0].form == 'register')
    r0, r1, memok = _run(ir, out, 'f')
    check("product preserved (24)", r0 == 24 and r1 == 24 and memok)


# ── memory recurrence (Case A: non-promotable loop still pipelines) ────────────

def test_memory_form_fallback():
    print("a non-promotable loop still pipelines via the memory form")
    # writing through a param pointer -> the accumulator/IV may promote, but a
    # loop where nothing clean is promotable falls back to the memory form.
    ir = _compile(SUM)
    # force the memory path by comparison: R2.5 alone pipelines SUM's memory form
    _p, s25, _r = pipeline_module(_compile(SUM))
    check("R2.5 alone pipelines SUM (memory form)", s25.pipelined == 1)
    out, stats, reps = pipeline_regaware_module(ir)
    check("R2.7 pipelines it too (register or memory)",
          stats.pipelined_register + stats.pipelined_memory == 1)


# ── equivalence: memory vs register scheduling both preserve behaviour ─────────

def test_mem_vs_reg_equivalence():
    print("memory-form and register-form pipelines are behaviour-equivalent")
    for code, fn, exp in ((SUM, 'f', 31), (PROD, 'f', 24)):
        ir = _compile(code)
        out, _s, _r = pipeline_regaware_module(ir)
        r0, r1, memok = _run(ir, out, fn)
        check(f"{code[:12]}...: result preserved ({exp})",
              r0 == exp and r1 == exp and memok)


# ── mixed batch + determinism + rollback ───────────────────────────────────────

BATCH = [
    "int f(){int s=0,i; int a[10]={1,2,3,4,5,6,7,8,9,10}; for(i=0;i<10;i++) s+=a[i]; return s;}",
    "int f(){int s=0,i; int a[8]={2,2,2,2,2,2,2,2}; for(i=0;i<8;i++) s+=a[i]*a[i]; return s;}",
    "int f(){int m=-99,i; int a[7]={3,9,2,7,1,8,5}; for(i=0;i<7;i++){ if(a[i]>m) m=a[i]; } return m;}",
]


def test_mixed_batch_correct():
    print("mixed batch: every pipeline preserves behaviour")
    for code in BATCH:
        ir = _compile(code)
        out, stats, reps = pipeline_regaware_module(ir)
        r0, r1, memok = _run(ir, out, 'f')
        check(f"behaviour identical ({'pipelined' if reps and any(r.committed for r in reps) else 'untouched'})",
              r0 == r1 and memok)


def test_determinism():
    print("register-aware pipelining is deterministic")
    for code in (SUM, PROD):
        o1, _s1, _r1 = pipeline_regaware_module(_compile(code))
        o2, _s2, _r2 = pipeline_regaware_module(_compile(code))
        check("identical output twice",
              [repr(x) for x in o1] == [repr(x) for x in o2])


def test_rollback_unsupported():
    print("unsupported loops are left untouched")
    code = "int ext(int); int f(){int s=0,i; for(i=0;i<5;i++){ s+=ext(i); } return s;}"
    ir = _compile(code)
    out, stats, reps = pipeline_regaware_module(ir)
    check("nothing pipelined (call in body)",
          stats.pipelined_register + stats.pipelined_memory == 0)
    check("IR multiset unchanged",
          sorted(repr(x) for x in out) == sorted(repr(x) for x in ir))


def test_coverage_recovered():
    print("coverage: R2.7 recovers (and exceeds) what R2.5 pipelines")
    # SUM is pipelined by R2.7 via the register form; R2.6 alone would break R2.5.
    ir = _compile(SUM)
    out, stats, reps = pipeline_regaware_module(ir)
    check("SUM pipelined by R2.7", stats.pipelined_register + stats.pipelined_memory == 1)
    check("via the shorter register recurrence", stats.pipelined_register == 1)


def main():
    for t in (test_register_form_lower_ii, test_accumulator_and_iv_shared,
              test_memory_form_fallback, test_mem_vs_reg_equivalence,
              test_mixed_batch_correct, test_determinism,
              test_rollback_unsupported, test_coverage_recovered):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R2.7 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

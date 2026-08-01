"""_r6_8_test.py -- unit tests for R6.8 Vector Software Pipelining.

Five groups:
  1. ELIGIBILITY   -- exactly elementwise and AXPY; reduction, GEMM, convolution
     and dot are rejected, each with a named reason;
  2. NON-INTERFERENCE -- the four excluded families compile byte-identically with
     the pass on and off, and the kill switch restores everything;
  3. THE INVARIANT FIX -- loop-invariant live-ins are shared across rotating
     banks, which is what lets R2.8 emit a COMPACT kernel instead of falling back
     to full unroll;
  4. GATES -- a spilling or unprofitable pipeline is rolled back, not shipped;
  5. THE ESTIMATOR -- a pipelined candidate must be costed with the proven trip
     counts, or the measurement is meaningless (regression test for a real bug).
"""
import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'verification'))

import vector_swp                                       # noqa: E402
from vector_backend import ilp_analysis as ia           # noqa: E402
from loopopt.pipeline_mve import (pipeline_mve_function, MVEStats,   # noqa: E402
                                  _kernel_invariants, _seed_cache)
from loopopt import modulo                              # noqa: E402
from ir import IRLabel                                  # noqa: E402
import suite                                            # noqa: E402

GB = ia.GB
_fails = []


def check(n, c):
    print(f"  [{'ok' if c else 'FAIL'}] {n}")
    if not c:
        _fails.append(n)


_CACHE = {}


def prod(fam, fn, mk):
    """The production-optimized vector IR for one kernel."""
    key = (fam, mk)
    if key not in _CACHE:
        vec, st, _r = ia.vectorize_all_module(copy.deepcopy(ia.build_ir(fn(mk))))
        sel, _m, _t = ia.production_codegen(copy.deepcopy(vec))
        _CACHE[key] = sel
    return _CACHE[key]


KERNELS = [('elementwise', suite.elementwise, 'vi16_t', True),
           ('axpy', suite.axpy, 'vi16_t', True),
           ('reduction', suite.reduction, 'vi32_t', False),
           ('gemm', suite.gemm, 'vi16_t', False),
           ('conv3', suite.conv3, 'vi8_t', False),
           ('dot', suite.dot, 'vi8_t', False)]


def _verdicts(ir):
    return [(l, ok, why) for (f, lo, hi, d, l, ok, why)
            in vector_swp.eligible_loops(ir)]


# ── 1. eligibility ────────────────────────────────────────────────────────────

def test_eligibility():
    print("exactly elementwise and AXPY are eligible; the other four are not")
    for fam, fn, mk, want in KERNELS:
        ir = prod(fam, fn, mk)
        v = _verdicts(ir)
        got = any(ok for _l, ok, _w in v)
        check(f"{fam} {mk}: eligible={got} (expected {want})", got == want)
        if not want:
            # every rejection must NAME a reason, never be a bare False
            for _l, ok, why in v:
                if not ok and not why:
                    check(f"{fam}: rejection has a reason", False)
                    break

    # the specific exclusions the milestone names
    red = _verdicts(prod('reduction', suite.reduction, 'vi32_t'))
    check("reduction is rejected for lacking vector ARITHMETIC (it reduces)",
          any(w == 'no-vector-arithmetic' for _l, ok, w in red if not ok))
    gem = _verdicts(prod('gemm', suite.gemm, 'vi16_t'))
    check("GEMM has no compact vector loop to pipeline",
          all(not ok for _l, ok, _w in gem))


def test_exclusion_predicate_directly():
    """The structural predicate must reject the forbidden shapes even if such a
    loop were ever produced -- convolution has no compact realisation today, so
    the sliding-window rule is otherwise unreachable and would go untested."""
    print("the exclusion rules reject the forbidden shapes on construction")
    from loopopt.analysis_iv import TripCount
    from ir import IRBinOp, Temp, IRVecArith, IRVecReduce

    class _Blk:
        def __init__(s, lo, hi):
            s.lo, s.hi = lo, hi

    class _Cfg:
        def __init__(s, blocks):
            s.blocks = blocks

    class _G:
        def __init__(s, blocks):
            s.cfg = _Cfg(blocks)

    class _Trip:
        kind = TripCount.KNOWN
        value = 8

    class _D:
        is_innermost = True
        trip_count = _Trip()
        header = 0
        body_blocks = {1}

    def verdict(body_ops, label='vcl_9_cond'):
        # block 0 is the header (the label); block 1 is the body
        sub = [IRLabel(label)] + list(body_ops)
        g = _G({0: _Blk(0, 0), 1: _Blk(1, len(sub) - 1)})
        return vector_swp.eligible_loop(_D(), sub, g)

    va = IRVecArith(Temp('t'), '+', Temp('a'), Temp('b'), '$vi8')

    ok, why = verdict([va])
    check("a plain vector-arithmetic body IS eligible (control)", ok and why == 'ok')

    ok, why = verdict([va, IRBinOp(Temp('w'), '|', Temp('x'), Temp('y'))])
    check("a body containing `|` is rejected as a sliding window",
          not ok and why.startswith('sliding-window-op'))

    ok, why = verdict([va, IRBinOp(Temp('w'), '>>', Temp('x'), Temp('y'))])
    check("a body containing `>>` is rejected as a sliding window",
          not ok and why.startswith('sliding-window-op'))

    ok, why = verdict([va, IRVecReduce(Temp('t2'), Temp('a'), '$vi8', '+')])
    check("a body containing $vreduce is rejected",
          not ok and why == 'excluded-kernel:IRVecReduce')

    ok, why = verdict([va], label='fb_3')
    check("a non-`vcl_` loop is not a compact vector loop",
          not ok and why == 'not-a-compact-vector-loop')


# ── 2. non-interference ───────────────────────────────────────────────────────

def test_non_interference():
    print("the four excluded families are untouched")
    for fam, fn, mk, want in KERNELS:
        if want:
            continue
        ir = prod(fam, fn, mk)
        out, _recs, summ = vector_swp.apply_vector_swp(copy.deepcopy(ir),
                                                       global_base=GB)
        check(f"{fam} {mk}: IR unchanged", not summ.changed
              and [repr(i) for i in out] == [repr(i) for i in ir])


def test_kill_switch():
    print("the kill switch restores the pre-R6.8 program")
    os.environ['APARA_NO_VECTOR_SWP'] = '1'
    try:
        ir = prod('axpy', suite.axpy, 'vi16_t')
        out, _r, summ = vector_swp.apply_vector_swp(copy.deepcopy(ir),
                                                    global_base=GB)
        check("APARA_NO_VECTOR_SWP=1 disables the pass",
              not summ.changed and [repr(i) for i in out] == [repr(i) for i in ir])
    finally:
        os.environ.pop('APARA_NO_VECTOR_SWP', None)


# ── 3. the invariant fix ──────────────────────────────────────────────────────

def test_invariants_shared():
    print("loop-invariant live-ins are SHARED across rotating banks, not rotated")
    ir = prod('axpy', suite.axpy, 'vi16_t')
    hdrs = [d.header for (f, lo, hi, d, l, ok, w)
            in vector_swp.eligible_loops(ir) if ok]
    check("the axpy vector loop was found", len(hdrs) == 1)
    if not hdrs:
        return

    # invariants must be non-empty for this loop and must not include anything the
    # kernel writes
    from loopopt.discovery import discover_function
    from loopopt.analysis_iv import annotate_induction_vars
    from loopopt.analysis_mem import annotate_memory_effects
    from loopopt.depgraph import DependenceGraph
    from loopopt.depgraph_disambig import MemoryDisambiguator
    from ir_utils import func_slices, dest_names
    with vector_swp._RelaxedKernelScope():
        for (lo, hi) in func_slices(ir):
            sub = ir[lo:hi + 1]
            ds = discover_function(sub, 0, len(sub) - 1)
            annotate_induction_vars(ds)
            annotate_memory_effects(ds)
            dis = MemoryDisambiguator(sub, 0, len(sub) - 1, ds)
            g = DependenceGraph(sub, 0, len(sub) - 1, disambiguator=dis)
            for d in ds:
                if d.header not in hdrs:
                    continue
                k, _w = modulo.build_kernel(d, g)
                if k is None:
                    continue
                inv = _kernel_invariants(g, k)
                written = set()
                for op in k.ops:
                    written.update(dest_names(g.instrs[op]) or ())
                check("invariants are non-empty (the loop reads live-ins)",
                      len(inv) > 0)
                check("no invariant is also written by the kernel",
                      not (inv & written))

    # and the seeded cache must map them to THEMSELVES in every bank
    cache = _seed_cache(['rec'], 3, ['inv'])
    check("an invariant maps to itself in all 3 banks",
          all(cache[('inv', b)].name == 'inv' for b in range(3)))
    check("all banks share ONE object for an invariant",
          cache[('inv', 0)] is cache[('inv', 2)])
    check("recurrence registers are still shared too",
          cache[('rec', 0)] is cache[('rec', 1)])


def test_compact_kernel_committed():
    """Before the invariant fix R2.8 declined with `unseeded-rotating-reg` and
    fell back to FULL UNROLL, which is not software pipelining at all."""
    print("R2.8 now emits a COMPACT kernel for the vector loop")
    ir = prod('axpy', suite.axpy, 'vi16_t')
    lo, hi = vector_swp._slice_of(ir, 'main')
    hdrs = [d.header for (f, a, b, d, l, ok, w)
            in vector_swp.eligible_loops(ir) if ok]
    with vector_swp._RelaxedKernelScope():
        stats = MVEStats()
        reps = []
        new = pipeline_mve_function(
            ir, lo, hi, stats, reps,
            select=lambda d, sub, g: (d.header in hdrs
                                      and vector_swp.eligible_loop(d, sub, g)[0]))
    committed = [r for r in reps if r.committed]
    check("a pipeline was committed", len(committed) == 1)
    if committed:
        r = committed[0]
        check(f"it is COMPACT, not a full-unroll fallback (compacted={r.compacted})",
              bool(r.compacted))
        check(f"more than one stage (II={r.ii}, stages={r.stages})", r.stages >= 2)
        check("the kernel loop runs a positive number of trips", r.loop_trips > 0)
        labs_old = {i.name for i in ir[lo:hi + 1] if isinstance(i, IRLabel)}
        labs_new = [i.name for i in new if isinstance(i, IRLabel)]
        check("a new kernel-loop label exists (the loop was not unrolled away)",
              any(l not in labs_old for l in labs_new))


# ── 4. gates ──────────────────────────────────────────────────────────────────

def test_gates():
    print("a pipeline that spills or does not pay is rolled back, not shipped")
    # elementwise vi16 pipelines cleanly but SPILLS -- the zero-spill gate must
    # reject it. This is a real measured case, not a synthetic one.
    ir = prod('elementwise', suite.elementwise, 'vi16_t')
    out, recs, summ = vector_swp.apply_vector_swp(copy.deepcopy(ir), global_base=GB)
    check("elementwise vi16 is attempted", summ.attempted == 1)
    check("it is NOT committed (it spills)", summ.committed == 0)
    check("the reason is recorded", recs and recs[0].reason == 'spilled')
    check("the IR is returned untouched",
          [repr(i) for i in out] == [repr(i) for i in ir])

    # an impossible profitability margin must reject even a good pipeline
    ir2 = prod('axpy', suite.axpy, 'vi16_t')
    out2, recs2, summ2 = vector_swp.apply_vector_swp(copy.deepcopy(ir2),
                                                     global_base=GB, margin=2.0)
    check("an unreachable margin rejects the axpy pipeline", summ2.committed == 0)
    check("and the IR is unchanged",
          [repr(i) for i in out2] == [repr(i) for i in ir2])

    # trip-count gate
    out3, recs3, summ3 = vector_swp.apply_vector_swp(copy.deepcopy(ir2),
                                                     global_base=GB,
                                                     min_trip=10 ** 6)
    check("a trip-count floor rejects it before any scheduling",
          summ3.committed == 0 and summ3.eligible == 0)


def test_axpy_commits():
    print("axpy vi16 DOES pipeline, and the estimate falls")
    ir = prod('axpy', suite.axpy, 'vi16_t')
    out, recs, summ = vector_swp.apply_vector_swp(copy.deepcopy(ir), global_base=GB)
    check("committed", summ.committed == 1 and summ.changed)
    if recs:
        r = recs[0]
        check(f"estimated dynamic bundles fell ({r.bundles_before}->{r.bundles_after})",
              r.bundles_after is not None and r.bundles_after < r.bundles_before)
        check("the record carries the schedule facts", r.ii and r.stages)


# ── 5. the estimator ──────────────────────────────────────────────────────────

def test_estimator_needs_proven_frequencies():
    """Regression test. The register form promotes loop registers across the whole
    function, which erases the memory-slot induction variable `analysis_iv` needs.
    Trip counts for UNTOUCHED loops then collapse to 1 and the estimate becomes
    meaningless -- it once read 702 -> 76, a 90% "gain" that was pure error."""
    print("costing a pipelined candidate requires the proven trip counts")
    ir = prod('axpy', suite.axpy, 'vi16_t')
    out, recs, summ = vector_swp.apply_vector_swp(copy.deepcopy(ir), global_base=GB)
    if not summ.changed:
        check("axpy pipelined (precondition)", False)
        return
    naive = vector_swp._estimated_dynamic_bundles(out, GB)          # no override
    honest = recs[0].bundles_after
    check(f"the naive estimate is optimistic and WRONG ({naive} vs {honest})",
          naive is not None and naive < honest)
    freq_before, _u = ia.label_frequencies(ir)
    freq_after, _u2 = ia.label_frequencies(out)
    lost = {k for k in freq_before if k not in freq_after}
    check(f"pipelining really does lose proven trip counts ({sorted(lost)})",
          len(lost) > 0)


def main():
    for t in (test_eligibility, test_exclusion_predicate_directly,
              test_non_interference, test_kill_switch,
              test_invariants_shared, test_compact_kernel_committed,
              test_gates, test_axpy_commits,
              test_estimator_needs_proven_frequencies):
        t()
    print()
    if _fails:
        print(f"{len(_fails)} FAILURES:")
        for f in _fails:
            print(f"  - {f}")
        return 1
    print("ALL R6.8 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

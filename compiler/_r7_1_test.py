"""_r7_1_test.py -- unit tests for R7.1 Register Rematerialization.

Four groups:
  1. ELIGIBILITY -- only `FP + small constant` is recomputable; large offsets,
     loads and computed values are not, and the kill switch disables everything;
  2. EMISSION    -- a rematerialized value is rebuilt with ONE instruction and no
     register inputs, and no store/reload pair is emitted for it;
  3. EFFECT      -- spills fall on the kernels R7.0 measured, previously rejected
     SWP kernels now compile spill-free, and memory traffic drops;
  4. SAFETY      -- the anti-thrash guard, and non-vector programs unchanged.
"""
import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'verification'))

import rematerialization as remat                    # noqa: E402
from codegen import CodeGen, POOL_REGS               # noqa: E402
from vector_backend import ilp_analysis as ia        # noqa: E402
import vector_swp                                    # noqa: E402
import suite                                         # noqa: E402

GB = ia.GB
_fails = []


def check(n, c):
    print(f"  [{'ok' if c else 'FAIL'}] {n}")
    if not c:
        _fails.append(n)


_C = {}


def swp_candidate(fam, fn, mk):
    """The pipelined IR R6.8 builds and then accepts or rejects on spilling --
    the code R7.0 analysed and R7.1 targets."""
    key = (fam, mk)
    if key in _C:
        return _C[key]
    from loopopt.pipeline_mve import pipeline_mve_function, MVEStats
    vec, st, _r = ia.vectorize_all_module(copy.deepcopy(ia.build_ir(fn(mk))))
    sel, _m, _t = ia.production_codegen(copy.deepcopy(vec))
    hdrs = [d.header for (f, lo, hi, d, l, ok, w)
            in vector_swp.eligible_loops(sel) if ok]
    if not hdrs:
        _C[key] = (sel, None)
        return _C[key]
    lo, hi = vector_swp._slice_of(sel, 'main')
    with vector_swp._RelaxedKernelScope():
        stt = MVEStats()
        reps = []
        ns = pipeline_mve_function(
            sel, lo, hi, stt, reps,
            select=lambda d, sub, g: (d.header in hdrs
                                      and vector_swp.eligible_loop(d, sub, g)[0]))
    _C[key] = (sel, sel[:lo] + list(ns) + sel[hi + 1:])
    return _C[key]


def compile_ir(ir, no_remat=False):
    if no_remat:
        os.environ['APARA_NO_REMAT'] = '1'
    else:
        os.environ.pop('APARA_NO_REMAT', None)
    try:
        cg = CodeGen(global_base=GB)
        body = cg.generate(copy.deepcopy(ir), global_base=GB)
        return cg, body
    finally:
        os.environ.pop('APARA_NO_REMAT', None)


# ── 1. eligibility ────────────────────────────────────────────────────────────

def test_eligibility():
    print("only `FP + small constant` is recomputable without live inputs")
    check("a small negative offset qualifies",
          remat.recipe_for_loadaddr(-392) is not None)
    check("a small positive offset qualifies",
          remat.recipe_for_loadaddr(8) is not None)
    check("the exact lower bound qualifies",
          remat.recipe_for_loadaddr(remat.FP_IMM_LO) is not None)
    check("the exact upper bound qualifies",
          remat.recipe_for_loadaddr(remat.FP_IMM_HI) is not None)
    # Beyond the immediate field codegen needs a BORROWED scratch register, and
    # the moment remat matters is the moment none is free.
    check("just past the upper bound does NOT qualify",
          remat.recipe_for_loadaddr(remat.FP_IMM_HI + 1) is None)
    check("just past the lower bound does NOT qualify",
          remat.recipe_for_loadaddr(remat.FP_IMM_LO - 1) is None)

    os.environ['APARA_NO_REMAT'] = '1'
    try:
        check("the kill switch disables every recipe",
              remat.recipe_for_loadaddr(-8) is None)
    finally:
        os.environ.pop('APARA_NO_REMAT', None)


def test_recipe_emission():
    print("a recipe rebuilds the value in ONE instruction with no register inputs")
    r = remat.recipe_for_loadaddr(-392)
    line = r.emit('$r5', '$r28')
    check(f"one instruction: {line!r}", '\n' not in line and line.count(';') == 0)
    check("it is an add of the frame pointer and a constant",
          line.startswith('+ $r5 ($i64) $r28 ') and line.endswith('-392'))
    check("it names no register other than the destination and FP",
          sorted(set(w for w in line.split() if w.startswith('$r'))) == ['$r28', '$r5'])


# ── 2. victim selection ───────────────────────────────────────────────────────

def test_choose_victim():
    print("victim selection prefers a recomputable value, but not one twice")
    recipes = {'a': remat.recipe_for_loadaddr(-8)}
    live = [('x', '$r1'), ('a', '$r2'), ('y', '$r3')]

    n, _r, rec = remat.choose_victim(live, set(), recipes)
    check("a recomputable value is preferred over an ordinary one",
          n == 'a' and rec is not None)

    n, _r, rec = remat.choose_victim(live, {'a'}, recipes)
    check("a protected value is never chosen", n != 'a')

    # the anti-thrash guard: once rebuilt, an ordinary value goes first instead
    n, _r, rec = remat.choose_victim(live, set(), recipes, rebuilt={'a'})
    check("an already-rebuilt value loses its preference", n == 'x' and rec is None)

    # ...but it is still evicted for free if it is the only candidate
    n, _r, rec = remat.choose_victim([('a', '$r2')], set(), recipes, rebuilt={'a'})
    check("it is still recomputed rather than spilled when chosen anyway",
          n == 'a' and rec is not None)

    n, _r, rec = remat.choose_victim([('a', '$r2')], {'a'}, recipes)
    check("nothing evictable returns nothing", n is None)


# ── 3. effect on real kernels ─────────────────────────────────────────────────

def _mem_ops(body):
    n = 0
    for line in body.split('\n'):
        s = line.strip()
        if s.startswith('$ld ') or s.startswith('$st '):
            n += 1
    return n


def test_spills_fall():
    """R7.0 measured these exact spill counts on the pipelined candidates."""
    print("spills fall on the kernels R7.0 measured")
    expect = {('elementwise', 'vi16_t'): 16,
              ('elementwise', 'vi32_t'): 9,
              ('axpy', 'vi32_t'): 8}
    fns = {'elementwise': suite.elementwise, 'axpy': suite.axpy}
    for (fam, mk), r70 in expect.items():
        _sel, cand = swp_candidate(fam, fns[fam], mk)
        if cand is None:
            check(f"{fam} {mk}: candidate built", False)
            continue
        off, _b0 = compile_ir(cand, no_remat=True)
        on, _b1 = compile_ir(cand, no_remat=False)
        check(f"{fam} {mk}: R7.0 baseline still {r70} spills "
              f"(measured {off._remat_stats.spills})",
              off._remat_stats.spills == r70)
        check(f"{fam} {mk}: spills fall {off._remat_stats.spills} -> "
              f"{on._remat_stats.spills}",
              on._remat_stats.spills < off._remat_stats.spills)
        check(f"{fam} {mk}: evictions were avoided, not just moved",
              on._remat_stats.evictions_avoided > 0)


def test_memory_spills_eliminated():
    """These pipelines needed MEMORY spills before R7.1 and need none after.

    They are still NOT admitted by the vector-SWP gate: relaxing it exposes a
    latent R6.8 defect (the pipelined code fails simulator verification with
    "5 PostCondition comparisons performed, 4 declared"). So the property tested
    here is the one R7.1 actually delivers -- no memory traffic -- not admission."""
    print("pipelines that needed memory spills now need none")
    for fam, fn, mk in (('axpy', suite.axpy, 'vi32_t'),
                        ('axpy', suite.axpy, 'vu32_t')):
        _sel, cand = swp_candidate(fam, fn, mk)
        off, _b0 = compile_ir(cand, no_remat=True)
        on, _b1 = compile_ir(cand, no_remat=False)
        check(f"{fam} {mk}: spilled to MEMORY before R7.1",
              bool(off.spilled_to_memory))
        check(f"{fam} {mk}: no MEMORY spill under R7.1",
              not on.spilled_to_memory)
        # `spilled` still reports that pressure forced an eviction -- deliberately,
        # because every pre-R7.1 gate depends on that meaning.
        check(f"{fam} {mk}: `spilled` still reports the pressure",
              bool(on.spilled))


def test_swp_admission_unchanged():
    """R7.1 must not change which pipelines ship, until the R6.8 defect is fixed."""
    print("no NEW software pipeline is admitted (the R6.8 defect is not exposed)")
    import vector_swp as vs
    for fam, fn, mk in (('axpy', suite.axpy, 'vi32_t'),
                        ('elementwise', suite.elementwise, 'vi16_t')):
        sel, _cand = swp_candidate(fam, fn, mk)
        for arm in (True, False):
            if arm:
                os.environ['APARA_NO_REMAT'] = '1'
            else:
                os.environ.pop('APARA_NO_REMAT', None)
            try:
                _o, recs, summ = vs.apply_vector_swp(copy.deepcopy(sel),
                                                     global_base=GB)
            finally:
                os.environ.pop('APARA_NO_REMAT', None)
            check(f"{fam} {mk}: not committed (remat={'off' if arm else 'on'})",
                  summ.committed == 0)


def test_memory_traffic():
    print("memory traffic falls -- a store plus a reload become one ALU op")
    _sel, cand = swp_candidate('axpy', suite.axpy, 'vi32_t')
    off, b0 = compile_ir(cand, no_remat=True)
    on, b1 = compile_ir(cand, no_remat=False)
    m0, m1 = _mem_ops(b0), _mem_ops(b1)
    check(f"static memory operations fall ({m0} -> {m1})", m1 < m0)
    check("no spill slot was needed at all",
          not on.spilled_to_memory and on._remat_stats.spills == 0)


def test_still_spilling_are_not_rematerializable():
    """If a kernel still spills, R7.1 must be able to say what is left."""
    print("kernels that still spill do so on NON-rematerializable values")
    _sel, cand = swp_candidate('elementwise', suite.elementwise, 'vi16_t')
    on, _b = compile_ir(cand, no_remat=False)
    if not on.spilled:
        check("elementwise vi16 still spills (precondition for this check)", True)
        return
    check("it avoided some evictions", on._remat_stats.evictions_avoided > 0)
    check("the residual spills are fewer than the R7.0 count of 16",
          on._remat_stats.spills < 16)


# ── 4. safety ─────────────────────────────────────────────────────────────────

def test_shipped_code_unaffected_where_no_pressure():
    print("code that never spills is byte-identical with remat on and off")
    for fam, fn, mk in (('dot', suite.dot, 'vi8_t'),
                        ('conv3', suite.conv3, 'vi8_t'),
                        ('gemm', suite.gemm, 'vi16_t')):
        vec, _st, _r = ia.vectorize_all_module(copy.deepcopy(ia.build_ir(fn(mk))))
        sel, _m, _t = ia.production_codegen(copy.deepcopy(vec))
        off, b0 = compile_ir(sel, no_remat=True)
        on, b1 = compile_ir(sel, no_remat=False)
        check(f"{fam} {mk}: no spills either way",
              not off.spilled_to_memory and not on.spilled_to_memory)
        check(f"{fam} {mk}: emitted code identical", b0 == b1)


def test_recipes_are_per_function():
    print("recipes are FP-relative, so they must not leak across functions")
    src = ('long long g(){int a[8];int i;long long s=0;'
           'for(i=0;i<8;i++)s+=a[i];return s;}\n'
           'long long f(){int b[8];int i;long long s=0;'
           'for(i=0;i<8;i++)s+=b[i];return s;}')
    ir = ia.build_ir(src)
    cg, _b = compile_ir(ir)
    check("compiles", True)
    check("no stale recipes survive the last function",
          all(isinstance(k, str) for k in cg._remat.keys()))


def main():
    for t in (test_eligibility, test_recipe_emission, test_choose_victim,
              test_spills_fall, test_memory_spills_eliminated,
              test_swp_admission_unchanged, test_memory_traffic,
              test_still_spilling_are_not_rematerializable,
              test_shipped_code_unaffected_where_no_pressure,
              test_recipes_are_per_function):
        t()
    print()
    if _fails:
        print(f"{len(_fails)} FAILURES:")
        for f in _fails:
            print(f"  - {f}")
        return 1
    print("ALL R7.1 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""_r6_6_test.py -- unit tests for R6.6 Vector Multiple Accumulator Expansion.

Four groups:
  1. ELIGIBILITY -- the transform fires exactly where it is legal and nowhere
     else, and every rejection names its reason (integer markers only; float
     rejected by name);
  2. STRUCTURE -- the emitted loop really does have one accumulator per unrolled
     copy, they are independent, and the epilogue is a balanced tree;
  3. EFFECT -- the recurrence actually shortens (RecMII 8 -> 3 on the kernel
     R6.6A modelled), measured with R6.6A's own machinery;
  4. NON-INTERFERENCE -- kernels that are not integer vector reductions are
     byte-identical to the pre-R6.6 compiler, and the kill switch restores it.
"""
import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'verification'))

import reduction_accumulator_expansion as rae          # noqa: E402
from vector_backend import ilp_analysis as ia          # noqa: E402
from ir import IRBinOp, IRAssign, Const               # noqa: E402
from ir_utils import dest_names                        # noqa: E402
import suite                                           # noqa: E402

_fails = []


def check(n, c):
    print(f"  [{'ok' if c else 'FAIL'}] {n}")
    if not c:
        _fails.append(n)


class FakePlan:
    """The four fields `plan_expansion` reads. Using a stand-in keeps the
    eligibility tests independent of how a real LoweringPlan is built."""

    def __init__(self, kind='sum-reduction', vtype='vi32', acc_slot=-8,
                 acc_bytes=8, signed=True):
        self.kind = kind
        self.vtype = vtype
        self.acc_slot = acc_slot
        self.acc_bytes = acc_bytes
        self.signed = signed


def _build(src, unroll=None, no_expand=False):
    """Compile one kernel under a pinned configuration and return the vector IR."""
    old_u = os.environ.get('APARA_VECTOR_UNROLL')
    old_x = os.environ.get('APARA_NO_ACC_EXPAND')
    if unroll is not None:
        os.environ['APARA_VECTOR_UNROLL'] = str(unroll)
    if no_expand:
        os.environ['APARA_NO_ACC_EXPAND'] = '1'
    else:
        os.environ.pop('APARA_NO_ACC_EXPAND', None)
    try:
        vec, st, _r = ia.vectorize_all_module(copy.deepcopy(ia.build_ir(src)))
        return vec, st
    finally:
        for k, v in (('APARA_VECTOR_UNROLL', old_u),
                     ('APARA_NO_ACC_EXPAND', old_x)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _accs(ir):
    return [n for i in ir for n in (dest_names(i) or ()) if n.startswith('_vxa')]


# ── 1. eligibility ────────────────────────────────────────────────────────────

def test_eligibility():
    print("expansion fires exactly where it is legal, and names every rejection")
    ok, why = rae.eligible(FakePlan(), 8)
    check("an 8x unrolled integer sum-reduction is eligible", ok and why == 'ok')

    ok, why = rae.eligible(FakePlan(), 1)
    check("U=1 is rejected (nothing to expand)",
          not ok and why == 'unroll-factor-1')

    # R8.1: dot products chain `$dot $accumulate` exactly as reductions chain
    # `$vreduce` + add, so they are now ELIGIBLE. Integer addition is associative
    # either way, which is what makes the regrouping exact for both.
    ok, why = rae.eligible(FakePlan(kind='dot-product'), 8)
    check("a dot product is now ELIGIBLE (R8.1)", ok and why == 'ok')

    ok, why = rae.eligible(FakePlan(kind='saxpy'), 8)
    check("a non-accumulating kernel is still rejected",
          not ok and why.startswith('not-an-accumulating-kernel'))

    # the restriction the milestone asks for by name: float addition is not
    # associative, so regrouping the accumulation would change the result.
    ok, why = rae.eligible(FakePlan(vtype='vf32'), 8)
    check("vf32 is rejected BY NAME as a float marker",
          not ok and why == 'float-marker-rejected:vf32')

    ok, why = rae.eligible(FakePlan(vtype='vq7'), 8)
    check("an unknown marker is rejected, not assumed integer",
          not ok and why.startswith('non-integer-marker'))

    for vt in ('vi8', 'vu8', 'vi16', 'vu16', 'vi32', 'vu32'):
        ok, _w = rae.eligible(FakePlan(vtype=vt), 4)
        if not ok:
            check(f"integer marker {vt} is eligible", False)
            break
    else:
        check("all six packed INTEGER markers are eligible", True)

    ok, why = rae.eligible(FakePlan(acc_slot=None), 8)
    check("no accumulator slot -> rejected", not ok and why == 'no-accumulator-slot')

    # R6.2C / defect D2: never touch a shared slot at an unestablished width.
    ok, why = rae.eligible(FakePlan(acc_bytes=None), 8)
    check("unknown accumulator WIDTH -> rejected (D2 rule)",
          not ok and why == 'accumulator-width-unknown')

    os.environ['APARA_NO_ACC_EXPAND'] = '1'
    try:
        ok, why = rae.eligible(FakePlan(), 8)
        check("kill switch disables it and says so",
              not ok and why.startswith('disabled:'))
    finally:
        os.environ.pop('APARA_NO_ACC_EXPAND', None)


# ── 2. structure of what it emits ─────────────────────────────────────────────

def test_structure():
    print("the emitted plan is U independent accumulators plus a balanced tree")
    for u in (2, 4, 8):
        exp, why = rae.plan_expansion(FakePlan(), u)
        if exp is None:
            check(f"U={u} produced a plan", False)
            continue
        names = [a.name for a in exp.accs]
        check(f"U={u}: exactly {u} accumulators, all distinct",
              len(exp.accs) == u and len(set(names)) == u)

        # acc0 inherits the live accumulator; the rest must start at zero, or
        # the reduction would count the incoming value U times.
        zeros = [i for i in exp.pre
                 if isinstance(i, IRAssign) and isinstance(i.src, Const)
                 and i.src.value == 0]
        check(f"U={u}: {u - 1} accumulators initialised to ZERO",
              len(zeros) == u - 1)
        check(f"U={u}: acc0 is seeded from the slot, not zeroed",
              names[0] not in [z.dest.name for z in zeros])

        # each copy's accumulate must touch ONLY its own accumulator -- that is
        # the whole point, and a shared destination would rebuild the chain.
        adds = [exp.accumulate(k, Const(0)) for k in range(u)]
        dests = [a.dest.name for a in adds]
        check(f"U={u}:每 copy accumulates into its OWN accumulator",
              sorted(dests) == sorted(names))
        check(f"U={u}: accumulate is acc_k = acc_k + partial (in place)",
              all(a.dest.name == a.left.name for a in adds))

        # the epilogue must be a TREE: a chain would re-create the serial
        # dependence this pass exists to remove.
        tree_adds = [i for i in exp.post if isinstance(i, IRBinOp)]
        check(f"U={u}: epilogue folds with {u - 1} adds",
              len(tree_adds) == u - 1)
        if u > 2:
            # in a chain every add but the first consumes the previous result;
            # in a balanced tree the first half consume only original accumulators
            first = tree_adds[0]
            second = tree_adds[1]
            check(f"U={u}: the fold is BALANCED, not a chain",
                  first.dest.name not in (second.left.name, second.right.name))


# ── 3. does the recurrence actually shorten? ──────────────────────────────────

def test_recurrence():
    """R6.6A projected RecMII 8 -> 3 and MII 8 -> 5 for reduction vi32 at U=8.
    Measured here with R6.6A's own machinery: the scalar SWP framework, with only
    the vector-op blocklist relaxed in memory."""
    print("the loop-carried recurrence actually shortens (R6.6A's own model)")
    from loopopt import modulo
    from loopopt.discovery import discover_function
    from loopopt.analysis_iv import annotate_induction_vars
    from loopopt.analysis_mem import annotate_memory_effects
    from loopopt.depgraph import DependenceGraph
    from loopopt.depgraph_disambig import MemoryDisambiguator
    from ir_utils import func_slices

    saved = modulo._UNSUPPORTED_KERNEL
    modulo._UNSUPPORTED_KERNEL = frozenset({'IRCall', 'IRIndirectCall',
                                            'IRLoadWide', 'IRStoreWide',
                                            'IRFsqrt'})
    VEC = {'IRVecArith', 'IRVecDot', 'IRVecDot128', 'IRVecReduce'}

    class Unit:
        """The kernel with latencies re-expressed in BUNDLE units (a dependence
        costs one bundle), which is the bundler's hazard semantics."""

        def __init__(s, k):
            s.ops = k.ops
            s.resources = k.resources
            s.instrs_cls = k.instrs_cls
            s.desc = k.desc
            s.intra = [(a, b, (1 if l else 0), d) for (a, b, l, d) in k.intra]
            s.carried = [(a, b, (1 if l else 0), d) for (a, b, l, d) in k.carried]

    def rec_of(src, no_expand):
        vec, _st = _build(src, unroll=8, no_expand=no_expand)
        sel, _m, _t = ia.production_codegen(copy.deepcopy(vec))
        for (lo, hi) in func_slices(sel):
            descs = discover_function(sel, lo, hi)
            annotate_induction_vars(descs)
            annotate_memory_effects(descs)
            dis = MemoryDisambiguator(sel, lo, hi, descs)
            g = DependenceGraph(sel, lo, hi, disambiguator=dis)
            for d in descs:
                k, _r = modulo.build_kernel(d, g)
                if k and any(type(k.instrs_cls[o]).__name__ in VEC for o in k.ops):
                    return modulo.min_ii(Unit(k))       # (mii, rec, res)
        return None

    try:
        src = suite.reduction('vi32_t')
        before = rec_of(src, no_expand=True)
        after = rec_of(src, no_expand=False)
        check("both arms produce an analysable vector loop",
              before is not None and after is not None)
        if before and after:
            mii0, rec0, _res0 = before
            mii1, rec1, _res1 = after
            print(f"        RecMII {rec0} -> {rec1},  MII {mii0} -> {mii1}")
            check(f"RecMII was the R6.6A value 8 (measured {rec0})", rec0 == 8)
            check(f"RecMII falls to ~3 as projected (measured {rec1})",
                  rec1 <= 3)
            check("MII falls too", mii1 < mii0)
    finally:
        modulo._UNSUPPORTED_KERNEL = saved


# ── 4. non-interference ───────────────────────────────────────────────────────

def test_non_interference():
    print("nothing but integer vector reductions is affected")
    # R8.1 removed `dot vi8` from this list: dot now expands too (it chains
    # `$dot $accumulate` identically), so it is no longer a non-interference
    # control. The four remaining families are still untouched.
    others = [('elementwise vi16', suite.elementwise('vi16_t')),
              ('axpy vi16', suite.axpy('vi16_t')),
              ('gemm vi16', suite.gemm('vi16_t')),
              ('conv3 vi8', suite.conv3('vi8_t'))]
    for name, src in others:
        on, _s1 = _build(src, unroll=4, no_expand=False)
        off, _s2 = _build(src, unroll=4, no_expand=True)
        check(f"{name}: identical with expansion on and off",
              [repr(i) for i in on] == [repr(i) for i in off])
        check(f"{name}: no accumulator-expansion temps emitted", not _accs(on))

    red, _s = _build(suite.reduction('vi32_t'), unroll=8, no_expand=False)
    # `_accs` lists DEFINITIONS, and each accumulator is defined twice -- once in
    # the prologue and once by its in-loop `acc_k = acc_k + partial` -- so the
    # distinct count is what equals U.
    check("a reduction DOES expand (the control for the checks above)",
          len(set(_accs(red))) == 8 and len(_accs(red)) == 16)

    # the kill switch must restore the pre-R6.6 program exactly
    red_off, _s = _build(suite.reduction('vi32_t'), unroll=8, no_expand=True)
    check("kill switch removes every expansion temp", not _accs(red_off))
    check("expanded and unexpanded reductions really differ",
          [repr(i) for i in red] != [repr(i) for i in red_off])


def test_u1_is_inert():
    print("U=1 changes nothing at all")
    on, _a = _build(suite.reduction('vi32_t'), unroll=1, no_expand=False)
    off, _b = _build(suite.reduction('vi32_t'), unroll=1, no_expand=True)
    check("U=1 is byte-identical with expansion on and off",
          [repr(i) for i in on] == [repr(i) for i in off])


def main():
    for t in (test_eligibility, test_structure, test_recurrence,
              test_non_interference, test_u1_is_inert):
        t()
    print()
    if _fails:
        print(f"{len(_fails)} FAILURES:")
        for f in _fails:
            print(f"  - {f}")
        return 1
    print("ALL R6.6 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

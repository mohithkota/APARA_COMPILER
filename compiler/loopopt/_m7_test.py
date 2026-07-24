"""
_m7_test.py -- unit tests for the M7 legality framework.

The framework performs NO transformation, so these tests only assert that each
predicate reports the right FACT on hand-built loops (positive and negative
cases), that LegalityFact truthiness / evaluate() short-circuiting behave, and
that LoopRotation's refactored legal() -- now a composition of shared predicates
-- makes the same decisions as the loops it is supposed to accept/reject. The
corpus cross-check (identical 73 rotations / 7 skips) is the end-to-end proof
that the refactor preserved behaviour; these are the focused unit checks.

Run:  python3 compiler/loopopt/_m7_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRStore, IRLoad, IRLoadAddr, IRCall, Temp, Const)
from loopopt import discover, LoopCanonicalizer
from loopopt import legality as L
from loopopt.rotate import LoopRotation
from loopopt._m5_test import sum_loop
from loopopt._m6_test import (do_while_sum, header_side_effect, multi_exit_sum,
                              short_circuit_sum, nested_sum)

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _loop(factory, hlbl='head', canon=True):
    Temp.reset(); ir = factory()
    if canon:
        LoopCanonicalizer().canonicalize(ir)
    descs = discover(ir)
    return next(d for d in descs if d.cfg.blocks[d.header].label == hlbl)


# a loop with two latches (NOT canonicalized -> stays multi-latch)
def _two_latch():
    return [IRFuncBegin('f', [], {}, 0),
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Const(5), 'body', 'exit'),
            IRLabel('body'),
            IRBinOp(Temp('iv2'), '+', Temp('iv'), Const(1)),
            IRLoadAddr(Temp('pi'), -8), IRStore(Temp('pi'), Const(0), Temp('iv2'), 8),
            IRCondJump(Temp('iv2'), '<', Const(3), 'more', 'head'),
            IRLabel('more'), IRJump('head'),
            IRLabel('exit'), IRReturn(Const(0)), IRFuncEnd('f')]


# a loop whose body contains a call (opaque memory effects)
def _call_loop():
    return [IRFuncBegin('f', [], {}, 0),
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Const(5), 'body', 'exit'),
            IRLabel('body'),
            IRCall(Temp('r'), 'g', []),
            IRBinOp(Temp('iv2'), '+', Temp('iv'), Const(1)),
            IRLoadAddr(Temp('pi'), -8), IRStore(Temp('pi'), Const(0), Temp('iv2'), 8),
            IRJump('head'),
            IRLabel('exit'), IRReturn(Const(0)), IRFuncEnd('f')]


# ── structural predicate facts ────────────────────────────────────────────────

def test_shape_predicates():
    print("shape predicates:")
    top = _loop(lambda: sum_loop(5))
    # a genuine bottom-tested loop: rotate a while loop and read the result
    # (a single-block self-loop do-while is classified top-tested by _classify_shape,
    #  since its header both tests and loops -- so we use a real rotated loop here).
    from loopopt.rotate import rotate_module
    Temp.reset(); rir = sum_loop(5); LoopCanonicalizer().canonicalize(rir)
    rotate_module(rir)
    bot = next(d for d in discover(rir) if d.cfg.blocks[d.header].label == 'head')
    check("is_top_tested true on while", L.is_top_tested(top).ok)
    check("is_top_tested false on rotated (bottom-tested) loop", not L.is_top_tested(bot).ok)
    check("is_bottom_tested true on rotated loop", L.is_bottom_tested(bot).ok)
    check("is_bottom_tested false on while", not L.is_bottom_tested(top).ok)
    check("has_labeled_header true", L.has_labeled_header(top).ok)


def test_structure_predicates():
    print("preheader / latch / back-edge predicates:")
    top = _loop(lambda: sum_loop(5))
    two = _loop(_two_latch, canon=False)      # keep two latches
    check("has_unique_preheader true (canonical)", L.has_unique_preheader(top).ok)
    check("has_single_latch true (canonical)", L.has_single_latch(top).ok)
    check("has_single_latch false (two latches)", not L.has_single_latch(two).ok)
    check("has_explicit_backedge true", L.has_explicit_backedge(top).ok)
    check("has_explicit_backedge false when multi-latch", not L.has_explicit_backedge(two).ok)


def test_header_predicates():
    print("header predicates:")
    top = _loop(lambda: sum_loop(5))
    se = _loop(header_side_effect)
    check("side-effect-free header true on clean guard", L.has_side_effect_free_header(top).ok)
    check("side-effect-free header false when header stores", not L.has_side_effect_free_header(se).ok)
    check("header_has_single_exit_test true", L.header_has_single_exit_test(top).ok)
    check("guard_inputs_loop_independent true", L.guard_inputs_loop_independent(top).ok)


def test_dedicated_exits():
    print("dedicated-exit predicate (reuses M4 helper):")
    top = _loop(lambda: sum_loop(5))
    multi = _loop(multi_exit_sum)
    check("has_dedicated_exits true on simple loop", L.has_dedicated_exits(top).ok)
    check("has_dedicated_exits reports a fact on multi-exit loop",
          isinstance(L.has_dedicated_exits(multi).ok, bool))


def test_analysis_backed_predicates():
    print("analysis-backed predicates (annotate on demand):")
    top = _loop(lambda: sum_loop(5))
    check("has_clean_iv finds the primary IV", L.has_clean_iv(top).ok)
    check("IV analysis was annotated on demand", top.primary_iv is not None)
    check("memory_safe true (no calls)", L.memory_safe(top).ok)
    callloop = _loop(_call_loop)
    check("memory_safe false when a call is present", not L.memory_safe(callloop).ok)
    check("call-loop memory analysis reports the call", len(callloop.calls) >= 1)
    pf = L.profile_suitable(top)
    check("profile_suitable reports available profile facts", pf.ok and top.profile_analyzed)


def test_fact_and_evaluate():
    print("LegalityFact truthiness + evaluate() short-circuit:")
    top = _loop(lambda: sum_loop(5))
    f = L.is_top_tested(top)
    check("LegalityFact is truthy when ok", bool(f) is True)
    check("failing fact is falsy", bool(L.is_bottom_tested(top)) is False)
    # evaluate returns the FIRST failing predicate's fact
    res = L.evaluate(top, [L.has_labeled_header, L.is_bottom_tested, L.has_single_latch])
    check("evaluate returns first failing fact", (not res.ok) and res.name == 'is_bottom_tested')
    allok = L.evaluate(top, [L.has_labeled_header, L.is_top_tested, L.has_single_latch])
    check("evaluate passes when all hold", allok.ok and allok.name == 'all')


def test_rotation_decisions_match():
    print("LoopRotation legality (composed) accepts/rejects the right loops:")
    rot = LoopRotation()
    cases = [
        ('simple while',        lambda: sum_loop(5), 'head',  True),
        ('do-while',            do_while_sum,        'body',  False),
        ('header side effect',  header_side_effect,  'head',  False),
        ('two latches',         _two_latch,          'head',  False),
        ('short-circuit while', short_circuit_sum,   'head',  True),
        ('multiple exits',      multi_exit_sum,      'head',  True),
    ]
    for label, fac, hlbl, expect in cases:
        canon = (label != 'two latches')     # keep the two-latch loop uncanonicalized
        d = _loop(fac, hlbl=hlbl, canon=canon)
        ok, reason = rot.legal(d)
        check(f"{label}: legal == {expect}" + ('' if ok == expect else f' (got reason={reason!r})'),
              ok == expect)


def main():
    tests = [test_shape_predicates, test_structure_predicates, test_header_predicates,
             test_dedicated_exits, test_analysis_backed_predicates,
             test_fact_and_evaluate, test_rotation_decisions_match]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"M7 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("M7 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

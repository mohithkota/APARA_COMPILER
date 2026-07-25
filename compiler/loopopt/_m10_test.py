"""
_m10_test.py -- unit tests for M10 (production pipeline integration).

Checks that the framework loop passes are the canonical pipeline path and are
behaviourally identical to the legacy pipeline: the drop-in adapter contracts,
full-tier IR + generated-code equivalence on representative programs, the
Rotation+LICM+IVSR framework transforms composing cleanly through the framework
(verification + rollback intact), and that compiler.py is actually wired to the
adapters. The end-to-end proof is pipeline_crosscheck.py (124/124 per-tier IR +
code + selected-tier, 0 verifier failures / rollbacks, LICM gate both on and off);
these are the focused unit checks.

Run:  python3 compiler/loopopt/_m10_test.py
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pycparser
from compiler import _FAKE_TYPEDEFS
from ir import Temp
from ir_gen import IRGenerator
import ivsr
import licm2
from loopopt import pipeline
from loopopt.loop_licm import licm_module
from loopopt.loop_ivsr import ivsr_module
from loopopt.rotate import rotate_module
from loopopt.canonicalize import LoopCanonicalizer
from loopopt.pipeline_crosscheck import (_build_tiers, _reset_pass_counters,
                                         _legacy_licm, _fw_ivsr, _fw_licm, _select,
                                         _codegen)

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


def _reprs(xs):
    return [repr(x) for x in xs]


_SIMPLE = 'int f(int*a,int n){int s=0;for(int i=0;i<n;i++)s+=a[i];return s;}'
_NESTED = 'int f(int*a,int n){int s=0;for(int i=0;i<n;i++)for(int j=0;j<n;j++)s+=a[j];return s;}'
_MULTI = 'int f(int*a,int*b,int n){int s=0;for(int i=0;i<n;i++)s+=a[i]*b[i];return s;}'


# ── tests ─────────────────────────────────────────────────────────────────────

def test_ivsr_adapter_dropin():
    print("pipeline.induction_strength_reduce == legacy ivsr (byte-identical, non-mutating):")
    ir = _compile(_SIMPLE)
    orig = list(ir)
    ivsr._iv_n[0] = 0
    legacy = ivsr.induction_strength_reduce(copy.deepcopy(ir))
    ivsr._iv_n[0] = 0
    fw = pipeline.induction_strength_reduce(ir)
    check("adapter IR identical to legacy ivsr", _reprs(legacy) == _reprs(fw))
    check("adapter did not mutate caller's list", ir == orig)


def test_licm_adapter_gate():
    print("pipeline.loop_invariant_code_motion preserves licm2's opt-in gate:")
    ir = _compile(_SIMPLE)
    os.environ.pop('APARA_LICM', None); os.environ.pop('APARA_NO_LICM', None)
    check("default (gate off) is a no-op returning the same object",
          pipeline.loop_invariant_code_motion(ir) is ir)
    os.environ['APARA_LICM'] = '1'
    a = licm2.loop_invariant_code_motion(copy.deepcopy(ir))
    b = pipeline.loop_invariant_code_motion(ir)
    check("gate on: adapter IR identical to licm2", _reprs(a) == _reprs(b))
    os.environ.pop('APARA_LICM', None)


def test_complete_pipeline_equivalence():
    print("full tier pipeline: framework IR + generated code identical to legacy:")
    for name, code in (('simple', _SIMPLE), ('nested', _NESTED), ('multi', _MULTI)):
        ir = _compile(code)
        _reset_pass_counters()
        legacy = _build_tiers(ir, ivsr.induction_strength_reduce, _legacy_licm)
        _reset_pass_counters()
        fw = _build_tiers(ir, _fw_ivsr, _fw_licm)
        ir_ok = all(_reprs(l) == _reprs(f) for (_ln, l), (_fn, f) in zip(legacy, fw))
        code_ok = all(_codegen(l)[0] == _codegen(f)[0] and _codegen(l)[1] == _codegen(f)[1]
                      for (_ln, l), (_fn, f) in zip(legacy, fw))
        lsel, _ = _select(legacy)
        fsel, _ = _select(fw)
        check(f"{name}: all tiers IR-identical", ir_ok)
        check(f"{name}: all tiers generate identical code", code_ok)
        check(f"{name}: same selected tier", lsel == fsel)


def test_nested_loops_equivalence():
    print("nested loops through the integrated IVSR adapter == legacy:")
    ir = _compile(_NESTED)
    ivsr._iv_n[0] = 0
    a = ivsr.induction_strength_reduce(copy.deepcopy(ir))
    ivsr._iv_n[0] = 0
    b = pipeline.induction_strength_reduce(ir)
    check("nested-loop IR identical", _reprs(a) == _reprs(b))


def test_rotation_licm_ivsr_interaction():
    print("Rotation + LICM + IVSR compose cleanly through the framework:")
    # all three framework transforms run in sequence on one program; each must
    # verify clean and never roll back (verification + rollback machinery intact).
    ir = _compile(_SIMPLE)
    LoopCanonicalizer().canonicalize(ir)
    rstats, _ = rotate_module(ir)
    os.environ['APARA_LICM'] = '1'
    lstats, _ = licm_module(ir)
    os.environ.pop('APARA_LICM', None)
    _res, istats, _ = ivsr_module(ir)
    total_vf = rstats.verifier_failures + lstats.verifier_failures + istats.verifier_failures
    total_rb = rstats.rollbacks + lstats.rollbacks + istats.rollbacks
    check("no verifier failures across Rotation+LICM+IVSR", total_vf == 0)
    check("no rollbacks across Rotation+LICM+IVSR", total_rb == 0)
    check("composition produced a non-empty IR", len(_res) > 0)


def test_verifier_and_rollback_integration():
    print("framework verification enabled; declines are clean no-ops:")
    # a loop with an opaque call is DECLINED by IVSR -> zero commits, and because a
    # decline is a no-op (not a verify failure) there are zero rollbacks.
    ir = _compile('int g(int);int f(int*a,int n){int s=0;for(int i=0;i<n;i++)s+=g(a[i]);return s;}')
    _res, stats, _ = ivsr_module(ir)
    check("declined loop: 0 commits", stats.commits == 0)
    check("verification enabled, 0 failures", stats.verifier_failures == 0)
    check("clean decline: 0 rollbacks", stats.rollbacks == 0)


def test_compiler_wired_to_framework():
    print("compiler.py imports the framework adapters (integration in place):")
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'compiler.py')).read()
    check("compiler imports induction_strength_reduce from loopopt.pipeline",
          'from loopopt.pipeline import induction_strength_reduce' in src)
    check("compiler imports loop_invariant_code_motion from loopopt.pipeline",
          'from loopopt.pipeline import loop_invariant_code_motion' in src)
    check("compiler no longer imports these from the legacy modules",
          'from ivsr import induction_strength_reduce' not in src
          and 'from licm2 import loop_invariant_code_motion' not in src)


def test_regression_batch():
    print("batch full-pipeline equivalence across all fixtures:")
    all_ok = True
    for code in (_SIMPLE, _NESTED, _MULTI):
        ir = _compile(code)
        _reset_pass_counters()
        legacy = _build_tiers(ir, ivsr.induction_strength_reduce, _legacy_licm)
        _reset_pass_counters()
        fw = _build_tiers(ir, _fw_ivsr, _fw_licm)
        all_ok = all_ok and all(_reprs(l) == _reprs(f)
                                for (_ln, l), (_fn, f) in zip(legacy, fw))
    check("every fixture's every tier is IR-identical", all_ok)


def main():
    tests = [test_ivsr_adapter_dropin, test_licm_adapter_gate,
             test_complete_pipeline_equivalence, test_nested_loops_equivalence,
             test_rotation_licm_ivsr_interaction,
             test_verifier_and_rollback_integration,
             test_compiler_wired_to_framework, test_regression_batch]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"M10 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("M10 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

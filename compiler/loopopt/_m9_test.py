"""
_m9_test.py -- unit tests for M9 LoopIVSR (IVSR migrated onto the framework).

Focused checks that the framework migration reproduces ivsr.py on representative
loops (simple / nested / multiple induction variables), correctly DECLINES loops
ivsr rejects, and that the new MutationTransaction.replace_span() primitive +
framework verification / rollback behave. The end-to-end proof of behavioural
equivalence is ivsr_crosscheck.py (124/124 programs identical, 0 verifier
failures / rollbacks); these are the small, targeted unit checks.

Run:  python3 compiler/loopopt/_m9_test.py
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
from loopopt import ivsr_module, LoopIVSR
from loopopt.transform import MutationTransaction

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


def _spec(instrs):
    ivsr._iv_n[0] = 0
    return ivsr.induction_strength_reduce(copy.deepcopy(instrs))


def _framework(instrs):
    ivsr._iv_n[0] = 0
    out, stats, _rep = ivsr_module(copy.deepcopy(instrs))
    return out, stats


def _identical(a, b):
    return [repr(x) for x in a] == [repr(x) for x in b]


# representative loop nests
_SIMPLE = 'int f(int*a,int n){int s=0;for(int i=0;i<n;i++)s+=a[i];return s;}'
_NESTED = 'int f(int*a,int n){int s=0;for(int i=0;i<n;i++)for(int j=0;j<n;j++)s+=a[j];return s;}'
_MULTI = 'int f(int*a,int*b,int n){int s=0;for(int i=0;i<n;i++)s+=a[i]+b[i];return s;}'
_CALL = 'int g(int);int f(int*a,int n){int s=0;for(int i=0;i<n;i++)s+=g(a[i]);return s;}'


# ── tests ─────────────────────────────────────────────────────────────────────

def test_replace_span_primitive():
    print("MutationTransaction.replace_span() rewrites a span + rolls back:")
    instrs = [f'i{k}' for k in range(6)]
    txn = MutationTransaction(instrs, 0)
    txn.replace_span(2, 3, ['x', 'y', 'z'])           # replace [i2,i3] with 3 items
    check("span replaced (grew by one)",
          instrs == ['i0', 'i1', 'x', 'y', 'z', 'i4', 'i5'])
    check("inserted counter tracks net growth", txn.inserted == 1)
    txn.rollback()
    check("rollback restores original list", instrs == [f'i{k}' for k in range(6)])


def test_simple_iv_equivalence():
    print("simple induction variable: reduced and IR identical to ivsr:")
    ir = _compile(_SIMPLE)
    a = _spec(ir)
    b, stats = _framework(ir)
    check("one loop strength-reduced", stats.commits == 1)
    check("framework IR identical to spec", _identical(a, b))


def test_nested_loop_equivalence():
    print("nested loops: IR identical to ivsr:")
    ir = _compile(_NESTED)
    a = _spec(ir)
    b, stats = _framework(ir)
    check("at least one loop reduced", stats.commits >= 1)
    check("framework IR identical to spec", _identical(a, b))


def test_multiple_iv_equivalence():
    print("multiple induction variables (two arrays): IR identical to ivsr:")
    ir = _compile(_MULTI)
    a = _spec(ir)
    b, stats = _framework(ir)
    check("framework IR identical to spec", _identical(a, b))


def test_legality_rejection():
    print("a loop ivsr rejects (opaque call) is declined identically:")
    ir = _compile(_CALL)
    a = _spec(ir)
    b, stats = _framework(ir)
    check("no reduction applied (loop clobbers memory via call)", stats.commits == 0)
    check("framework IR identical to spec (unchanged loop)", _identical(a, b))


def test_rollback_correctness():
    print("declined attempts roll back cleanly (IR only DCE-changed, no partial edit):")
    # The call loop is attempted but declined; the only change to its IR must be
    # the final dead_temp_elim (same as spec) -- never a half-applied rewrite.
    ir = _compile(_CALL)
    b, stats = _framework(ir)
    # every attempt on this program was a no-op or illegal, never a rollback of a
    # verify failure, and nothing committed.
    check("zero commits", stats.commits == 0)
    check("zero verifier failures", stats.verifier_failures == 0)
    check("zero rollbacks (declines are no-ops, not verify failures)", stats.rollbacks == 0)


def test_verifier_integration():
    print("reductions pass framework verification (enabled, zero failures):")
    for name, code in (('simple', _SIMPLE), ('nested', _NESTED), ('multi', _MULTI)):
        ir = _compile(code)
        _b, stats = _framework(ir)
        check(f"{name}: 0 verifier failures", stats.verifier_failures == 0)
        check(f"{name}: 0 rollbacks", stats.rollbacks == 0)


def test_regression_batch():
    print("batch equivalence + total reductions match across all fixtures:")
    total_fw = 0
    all_ident = True
    for code in (_SIMPLE, _NESTED, _MULTI, _CALL):
        ir = _compile(code)
        a = _spec(ir)
        b, stats = _framework(ir)
        total_fw += stats.commits
        all_ident = all_ident and _identical(a, b)
    check("all fixtures IR-identical to spec", all_ident)
    check("total loops reduced across fixtures == 3", total_fw == 3)


def test_transform_is_a_loop_transform():
    print("LoopIVSR is a framework LoopTransform (runs through the driver):")
    from loopopt.transform import LoopTransform
    check("LoopIVSR subclasses LoopTransform", issubclass(LoopIVSR, LoopTransform))
    check("legal() is trivially true (legality lives in the planner)",
          LoopIVSR().legal(None) == (True, ''))


def main():
    tests = [test_replace_span_primitive, test_simple_iv_equivalence,
             test_nested_loop_equivalence, test_multiple_iv_equivalence,
             test_legality_rejection, test_rollback_correctness,
             test_verifier_integration, test_regression_batch,
             test_transform_is_a_loop_transform]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"M9 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("M9 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

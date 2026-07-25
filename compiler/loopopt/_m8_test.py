"""
_m8_test.py -- unit tests for M8 LoopLICM (LICM migrated onto the framework).

Focused checks on hand-built loops that the transform's decisions match licm2's
whitelist and legality, plus the new MutationTransaction.move() primitive and the
innermost-first fixpoint (outward migration through nested preheaders). The
end-to-end proof of behavioural equivalence is licm_crosscheck.py (124/124
programs identical, 843 hoists each side, 0 verifier failures / rollbacks); these
are the small, targeted unit checks.

Run:  python3 compiler/loopopt/_m8_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRStore, IRLoad, IRLoadAddr, IRCall, Temp, Const)
from loopopt import discover, licm_module, LoopLICM
from loopopt.transform import MutationTransaction, LoopTransformDriver, TransformStats

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


# ── loop factories ────────────────────────────────────────────────────────────

def invariant_loop():
    """One clearly loop-invariant pure computation (inv = 3 + 4) in the body, plus
    a variant user (use = inv * iv). licm2 (and M8) hoist the invariant and the
    constant-offset IRLoadAddr addresses; iv (a memory load) and use (reads iv)
    stay put."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Const(10), 'body', 'exit'),
            IRLabel('body'),
            IRBinOp(Temp('inv'), '+', Const(3), Const(4)),        # invariant, hoistable
            IRBinOp(Temp('use'), '*', Temp('inv'), Temp('iv')),   # variant (reads iv)
            IRLoadAddr(Temp('ps'), -16), IRStore(Temp('ps'), Const(0), Temp('use'), 8),
            IRBinOp(Temp('iv2'), '+', Temp('iv'), Const(1)),
            IRStore(Temp('pc'), Const(0), Temp('iv2'), 8),
            IRJump('head'),
            IRLabel('exit'), IRReturn(Const(0)), IRFuncEnd('f')]


def used_after_loop():
    """The invariant's destination is also used AFTER the loop -> licm2 refuses to
    hoist (its conservative dominance rule requires every use to be in the loop)."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Const(10), 'body', 'exit'),
            IRLabel('body'),
            IRBinOp(Temp('inv'), '+', Const(3), Const(4)),        # used in body AND exit
            IRBinOp(Temp('use'), '*', Temp('inv'), Temp('iv')),
            IRLoadAddr(Temp('ps'), -16), IRStore(Temp('ps'), Const(0), Temp('use'), 8),
            IRBinOp(Temp('iv2'), '+', Temp('iv'), Const(1)),
            IRStore(Temp('pc'), Const(0), Temp('iv2'), 8),
            IRJump('head'),
            IRLabel('exit'),
            IRBinOp(Temp('after'), '+', Temp('inv'), Const(1)),   # use OUTSIDE the loop
            IRReturn(Temp('after')), IRFuncEnd('f')]


def float_loop():
    """A float invariant is EXCLUDED (NaN / trap uncertainty) -- must not hoist."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLabel('pre'), IRJump('head'),
            IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
            IRCondJump(Temp('iv'), '<', Const(10), 'body', 'exit'),
            IRLabel('body'),
            IRBinOp(Temp('finv'), '+', Const(3), Const(4), ftype='$f32'),  # float: excluded
            IRBinOp(Temp('iv2'), '+', Temp('iv'), Const(1)),
            IRStore(Temp('pc'), Const(0), Temp('iv2'), 8),
            IRJump('head'),
            IRLabel('exit'), IRReturn(Const(0)), IRFuncEnd('f')]


def nested_loop():
    """An invariant defined in the INNER loop but invariant w.r.t. the OUTER loop
    too. licm2's innermost-first fixpoint first hoists it to the inner preheader
    (inside the outer body), then a later round migrates it to the outer preheader.
    We assert it ends up above the OUTER header."""
    return [IRFuncBegin('f', [], {}, 0),
            IRLabel('opre'), IRJump('ohead'),
            IRLabel('ohead'),
            IRLoadAddr(Temp('poi'), -8), IRLoad(Temp('oi'), Temp('poi'), Const(0), 8),
            IRCondJump(Temp('oi'), '<', Const(10), 'obody', 'oexit'),
            IRLabel('obody'),
            IRJump('ihead'),                                       # inner preheader = obody tail
            IRLabel('ihead'),
            IRLoadAddr(Temp('pii'), -16), IRLoad(Temp('ii'), Temp('pii'), Const(0), 8),
            IRCondJump(Temp('ii'), '<', Const(10), 'ibody', 'iexit'),
            IRLabel('ibody'),
            IRBinOp(Temp('inv'), '+', Const(3), Const(4)),        # invariant in BOTH loops
            IRLoadAddr(Temp('ps'), -24), IRStore(Temp('ps'), Const(0), Temp('inv'), 8),
            IRBinOp(Temp('ii2'), '+', Temp('ii'), Const(1)),
            IRStore(Temp('pii'), Const(0), Temp('ii2'), 8),
            IRJump('ihead'),
            IRLabel('iexit'),
            IRBinOp(Temp('oi2'), '+', Temp('oi'), Const(1)),
            IRStore(Temp('poi'), Const(0), Temp('oi2'), 8),
            IRJump('ohead'),
            IRLabel('oexit'), IRReturn(Const(0)), IRFuncEnd('f')]


# ── helpers ───────────────────────────────────────────────────────────────────

def _idx_of_def(ir, name):
    for k, ins in enumerate(ir):
        if name in [d.name for d in [getattr(ins, 'dest', None)] if d is not None
                    and hasattr(d, 'name')]:
            return k
    return None


def _idx_of_label(ir, name):
    for k, ins in enumerate(ir):
        if type(ins).__name__ == 'IRLabel' and ins.name == name:
            return k
    return None


# ── tests ─────────────────────────────────────────────────────────────────────

def test_move_primitive():
    print("MutationTransaction.move() reorders + rolls back exactly:")
    instrs = [f'i{k}' for k in range(6)]
    txn = MutationTransaction(instrs, 0)
    txn.move(4, 1)                        # pull i4 up to index 1 (del 4; insert 1)
    check("move relocated the object", instrs == ['i0', 'i4', 'i1', 'i2', 'i3', 'i5'])
    check("membership unchanged (no create/delete)", sorted(instrs) == ['i0','i1','i2','i3','i4','i5'])
    txn.rollback()
    check("rollback restores original order", instrs == [f'i{k}' for k in range(6)])


def test_basic_hoist():
    print("basic invariant is hoisted into the preheader:")
    Temp.reset(); ir = invariant_loop()
    licm_module(ir)
    inv_i = _idx_of_def(ir, 'inv')
    head_i = _idx_of_label(ir, 'head')
    use_i = _idx_of_def(ir, 'use')
    check("inv defined BEFORE the header (in the preheader)", inv_i is not None and inv_i < head_i)
    check("variant 'use' stays inside the body (after header)", use_i is not None and use_i > head_i)


def test_hoist_count_matches_licm2():
    print("hoist count equals licm2 on the same loop:")
    import licm2
    os.environ['APARA_LICM'] = '1'; os.environ.pop('APARA_NO_LICM', None)
    Temp.reset(); ir = invariant_loop()
    import copy
    a = licm2.loop_invariant_code_motion(copy.deepcopy(ir))
    b = copy.deepcopy(ir); stats, _ = licm_module(b)
    check("M8 IR identical to licm2 IR", [repr(x) for x in a] == [repr(x) for x in b])
    check("at least one instruction hoisted", stats.commits >= 1)


def test_used_after_loop_not_hoisted():
    print("value used after the loop is NOT hoisted:")
    Temp.reset(); ir = used_after_loop()
    licm_module(ir)
    inv_i = _idx_of_def(ir, 'inv')
    head_i = _idx_of_label(ir, 'head')
    check("inv stays inside the loop body (use after loop blocks hoist)", inv_i > head_i)


def test_float_not_hoisted():
    print("float invariant is excluded:")
    Temp.reset(); ir = float_loop()
    licm_module(ir)
    finv_i = _idx_of_def(ir, 'finv')
    head_i = _idx_of_label(ir, 'head')
    check("float invariant stays in the body", finv_i > head_i)


def test_nested_migration():
    print("innermost-first fixpoint migrates an invariant to the OUTER preheader:")
    Temp.reset(); ir = nested_loop()
    licm_module(ir)
    inv_i = _idx_of_def(ir, 'inv')
    ohead_i = _idx_of_label(ir, 'ohead')
    check("inv migrated above the OUTER header", inv_i is not None and inv_i < ohead_i)


def test_legal_requires_preheader():
    print("legal() rejects a loop without a unique dominating preheader:")
    # two external entries into the header -> no unique preheader
    ir = [IRFuncBegin('f', [], {}, 0),
          IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('c'), Temp('pc'), Const(0), 8),
          IRCondJump(Temp('c'), '<', Const(1), 'head', 'alt'),
          IRLabel('alt'), IRJump('head'),                 # second entry into head
          IRLabel('head'),
          IRLoad(Temp('iv'), Temp('pc'), Const(0), 8),
          IRCondJump(Temp('iv'), '<', Const(10), 'body', 'exit'),
          IRLabel('body'),
          IRBinOp(Temp('iv2'), '+', Temp('iv'), Const(1)),
          IRStore(Temp('pc'), Const(0), Temp('iv2'), 8),
          IRJump('head'),
          IRLabel('exit'), IRReturn(Const(0)), IRFuncEnd('f')]
    Temp.reset()
    descs = discover(ir)
    head = next(d for d in descs if d.cfg.blocks[d.header].label == 'head')
    ok, reason = LoopLICM().legal(head)
    check("legal() is False without a unique preheader", not ok)


def main():
    tests = [test_move_primitive, test_basic_hoist, test_hoist_count_matches_licm2,
             test_used_after_loop_not_hoisted, test_float_not_hoisted,
             test_nested_migration, test_legal_requires_preheader]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"M8 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("M8 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

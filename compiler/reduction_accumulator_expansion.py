"""
reduction_accumulator_expansion.py -- R6.6 Vector Multiple Accumulator Expansion.

R6.4 unrolls the compact vector loop U times. For a sum-reduction every copy then
accumulates into the SAME location, so the U copies -- which are otherwise
completely independent -- are chained into one serial dependence:

    acc += partial0
    acc += partial1        <- waits for partial0's add
    acc += partial2        <- waits for partial1's add
    ...

R6.6A measured the consequence on `reduction vi32` at U=8: a recurrence of length
8, `RecMII = 8` against `ResMII = 5`, so the recurrence -- not the machine --
sets the loop's cost. That recurrence did not exist before unrolling; at U=1 it
is a single add. Unrolling created it.

This pass gives every unrolled copy its own accumulator, so the U adds become
independent, and folds them back together once the loop is finished:

    acc0 += partial0                      acc0 = <the original accumulator>
    acc1 += partial1                      acc1..accU-1 = 0
    ...                                   (all independent -- no chain)
    ---- after the loop ----
    acc_slot = ((acc0+acc1) + (acc2+acc3)) + ...       balanced tree

Correctness rests on ONE property: integer addition is associative, including
under two's-complement wrap-around, so regrouping the partial sums cannot change
the result. That is why this is restricted to integer element types and why
`vf32_t` is rejected -- floating-point addition is NOT associative and regrouping
would change the answer.

Narrow accumulators are safe for the same reason. The unexpanded loop truncates
to `acc_bytes` on every iteration and this version truncates once at the end, but
(a+b) mod M + c == (a+b+c) mod M, and a sign-extending reload of a truncated
value is congruent mod M, so the stored result is identical bit for bit.

WHAT THIS PASS DOES NOT DO: no scheduling change, no bundler change, no legality
change, no new IR node, no ISA change, and no new lowering -- the accumulate is
still `$vreduce` + an integer add, emitted by the existing reduction lowering in
`vector_lowering.build_compact_body`. This module only decides whether expansion
applies and supplies the prologue, the per-copy accumulate and the epilogue.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import Const, IRAssign, IRBinOp                              # noqa: E402
import vector_capability_db as _vdb                                  # noqa: E402
import vector_compact_loop as _vcl                                   # noqa: E402
from vector_lowering import _fresh                                   # noqa: E402


# Kill switch, mirroring APARA_NO_VECTORIZE / APARA_NO_SWP / APARA_NO_SUPERBLOCK.
def _disabled():
    return os.environ.get('APARA_NO_ACC_EXPAND', '') not in ('', '0')


class ExpansionPlan:
    """The three pieces the lowering needs: what to emit before the loop, which
    accumulator each unrolled copy owns, and what to emit after it."""
    __slots__ = ('u', 'accs', 'pre', 'post')

    def __init__(self, u, accs, pre, post):
        self.u = u
        self.accs = accs
        self.pre = pre
        self.post = post

    def accumulate(self, copy_index, partial):
        """The accumulate for one unrolled copy: `acc_k = acc_k + partial`.

        Destination and left operand are the SAME temp, which is exactly the
        form R2.6's loop register promotion already produces for scalar loops
        (`_lr103 = _lr103 + _vred10`), so codegen and register allocation need no
        change to keep it live across the back edge."""
        acc = self.accs[copy_index]
        return IRBinOp(acc, '+', acc, partial)

    def __repr__(self):
        return f'<ExpansionPlan u={self.u} accs={len(self.accs)}>'


def eligible(plan, u):
    """(ok, reason) -- may this reduction's accumulator be expanded?

    Reported in the same fact-style as `vector_legality`: a single reason string
    naming the property that failed, never a bare False."""
    if _disabled():
        return False, 'disabled:APARA_NO_ACC_EXPAND'
    # R8.1: dot products chain `$dot $accumulate` exactly as reductions chain
    # `$vreduce` + add, and integer addition is associative either way, so the
    # same regrouping is exact for both kinds.
    if plan.kind not in ('sum-reduction', 'dot-product'):
        return False, f'not-an-accumulating-kernel:{plan.kind}'
    if u < 2:
        return False, 'unroll-factor-1'

    vt = plan.vtype or ''
    # Integer markers only. Floating point is rejected FIRST and by name: the
    # whole transform is a regrouping of the accumulation, and float addition is
    # not associative, so regrouping would change the computed result. This is
    # defence in depth -- `ELEMENT_TYPES` currently has no float entry, so a
    # float reduction cannot reach here today -- but the restriction is a
    # property of the transform, not of the current table, so it is checked here
    # rather than assumed.
    if vt.startswith('vf'):
        return False, f'float-marker-rejected:{vt}'
    e = _vdb.ELEMENT_TYPES.get(vt)
    if e is None:
        return False, f'non-integer-marker:{vt or "?"}'

    if plan.acc_slot is None:
        return False, 'no-accumulator-slot'
    # R6.2C / defect D2: the accumulator slot is shared with the scalar code, so
    # it must be accessed at exactly the width that code uses. If that width
    # could not be established, do not touch the slot.
    if not isinstance(plan.acc_bytes, int):
        return False, 'accumulator-width-unknown'
    return True, 'ok'


def best_accumulator_count(chunks, cap=8):
    """How many partial accumulators to use for a straight-line chain of
    `chunks` accumulating operations.

    With K accumulators the dependence chain is ceil(chunks/K) accumulates plus
    a ceil(log2(K))-deep fold, so the depth is minimised somewhere in the middle
    and grows again as K rises. Pick the K that minimises it, preferring the
    SMALLEST such K because every extra accumulator is another simultaneously
    live register -- and R7.0 established register pressure as this compiler's
    binding constraint."""
    best, best_k = None, 1
    k = 1
    while k <= min(chunks, cap):
        depth = -(-chunks // k) + (k - 1).bit_length()
        if best is None or depth < best:
            best, best_k = depth, k
        k *= 2
    return best_k


def plan_expansion(plan, u, load_fn=None, store_fn=None):
    """(ExpansionPlan or None, reason).

    `load_fn`/`store_fn` default to the compact loop's slot accessors. The
    fully-unrolled lowering passes its own (signature-compatible) pair so that
    ONE implementation of the transform serves both realisations."""
    ok, reason = eligible(plan, u)
    if not ok:
        return None, reason
    load_fn = load_fn or _vcl.slot_load
    store_fn = store_fn or _vcl.slot_store

    # ── prologue: acc0 takes the live accumulator, the rest start at zero ──
    pre, val = load_fn(plan.acc_slot, plan.signed,
                       elem_bytes=plan.acc_bytes)
    pre = list(pre)
    accs = []
    a0 = _fresh('_vxa')
    pre.append(IRAssign(a0, val))
    accs.append(a0)
    for _k in range(1, u):
        ak = _fresh('_vxa')
        pre.append(IRAssign(ak, Const(0)))
        accs.append(ak)

    # ── epilogue: fold the accumulators with a BALANCED tree, then store ──
    # A tree rather than a chain: this runs once, but a chain of U-1 adds would
    # re-create in the epilogue exactly the serial dependence the pass exists to
    # remove, and the tree is log2(U) deep for the same instruction count.
    post = []
    level = list(accs)
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level) - 1, 2):
            t = _fresh('_vxt')
            post.append(IRBinOp(t, '+', level[i], level[i + 1]))
            nxt.append(t)
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    post += store_fn(plan.acc_slot, level[0], elem_bytes=plan.acc_bytes)

    return ExpansionPlan(u, accs, pre, post), 'ok'

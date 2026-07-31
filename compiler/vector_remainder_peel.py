"""
vector_remainder_peel.py -- Remainder Peeling for Vector Kernels (R4.2.7).

R4.1-R4.2.6 handle `trip % lanes` leftover elements by KEEPING the original
scalar loop, with its induction variable started at `chunks*lanes`. That loop
runs at most `lanes-1` times but still costs a full loop skeleton -- a compare, a
branch, an IV load/add/store and the label boundaries the bundler cannot pack
across. It is the documented weak case: `vector add`, N=20 at 8 lanes, is 2 chunks
plus a 4-iteration tail and finishes at 30 bundles against a 23-bundle SCALAR
baseline. Vectorizing made it bigger, and compaction could not help because with
2 chunks there is nothing to compact.

PEELING removes that loop entirely: the remainder is known at compile time and is
bounded by `lanes-1 <= 7`, so the tail iterations are emitted as straight-line
code at CONSTANT offsets, and the scalar loop is deleted. The bundler can then
pack the tail against the vector body instead of being blocked by a loop
boundary.

WHY THIS IS SAFE -- the tail is NOT re-derived from the source. Re-synthesising
`c[i] = a[i] + b[i]` from scratch would risk getting integer promotion or
sub-word truncation subtly wrong (exactly the class of bug that made R4.1's
narrow-accumulator dot diverge). Instead each planner records a `PeelTemplate`
holding the ORIGINAL instructions' own `elem_bytes` and `unsigned` flags and the
original arithmetic opcode, and peeling replays those attributes at constant
offsets. The differential oracle then validates the result like any other
lowering, and a mismatch rolls the whole kernel back to scalar.

The IV slot is set to `trip` explicitly after the peeled tail, because the
deleted loop is what used to leave it there and the vectorized function must
leave memory identical to the scalar one.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import Const, IRLoad, IRStore, IRLoadAddr, IRBinOp
from vector_lowering import _fresh


class PeelTemplate:
    """Everything needed to replay ONE scalar element operation at a constant
    index, taken from the original loop body rather than re-derived.

        loads     [(slot, elem_bytes, unsigned)] in operand order
        value     (op, unsigned) combining the loads, or None for a single load
        acc_slot  reduction accumulator slot (dot/reduction), else None
        acc_op    how the value joins the accumulator (always '+')
        store     (slot, elem_bytes) for elementwise, else None
    """
    __slots__ = ('loads', 'value', 'acc_slot', 'acc_op', 'store')

    def __init__(self, loads, value=None, acc_slot=None, acc_op='+', store=None):
        self.loads = loads
        self.value = value
        self.acc_slot = acc_slot
        self.acc_op = acc_op
        self.store = store


def _elem_load(slot, byte_off, elem_bytes, unsigned):
    base = _fresh('_vrpb')
    dest = _fresh('_vrpv')
    return [IRLoadAddr(base, slot),
            IRLoad(dest, base, Const(byte_off), elem_bytes=elem_bytes,
                   unsigned=unsigned)], dest


def _elem_store(slot, byte_off, value, elem_bytes):
    base = _fresh('_vrps')
    return [IRLoadAddr(base, slot), IRStore(base, Const(byte_off), value,
                                            elem_bytes)]


def build_peeled_tail(plan):
    """Straight-line code for the `remainder` leftover elements, followed by the
    IV fix-up. Returns (instrs, n_ops) or (None, reason)."""
    tmpl = getattr(plan, 'peel', None)
    if tmpl is None:
        return None, 'no-peel-template'
    if plan.remainder <= 0:
        return None, 'no-remainder'

    out = []
    for r in range(plan.remainder):
        idx = plan.chunks * plan.lanes + r
        byte = idx * plan.eb
        vals = []
        for (slot, eb, uns) in tmpl.loads:
            ins, d = _elem_load(slot, byte, eb, uns)
            out += ins
            vals.append(d)
        if tmpl.value is None:
            val = vals[0]
        else:
            op, uns = tmpl.value
            val = _fresh('_vrpr')
            out.append(IRBinOp(val, op, vals[0], vals[1], unsigned=uns))
        if tmpl.acc_slot is not None:            # dot / reduction
            abase = _fresh('_vrpa')
            acur = _fresh('_vrpc')
            out.append(IRLoadAddr(abase, tmpl.acc_slot))
            out.append(IRLoad(acur, abase, Const(0), elem_bytes=8,
                              unsigned=False))
            nxt = _fresh('_vrpn')
            out.append(IRBinOp(nxt, tmpl.acc_op, acur, val))
            out += _elem_store(tmpl.acc_slot, 0, nxt, 8)
        else:                                    # elementwise
            slot, eb = tmpl.store
            out += _elem_store(slot, byte, val, eb)

    # The deleted scalar loop is what used to leave the IV at `trip`; restore it
    # so the vectorized function leaves memory identical to the scalar one.
    out += _elem_store(plan.iv_slot, 0, Const(plan.trip), 8)
    return out, len(out)


def splice_peeled(instrs, plan, vec_body, iv_init_value):
    """Vector body + peeled tail, with the scalar loop DELETED.

    `iv_init_value` is what the IV init store should hold on entry to the vector
    body: `chunks*lanes` for the unrolled realisation (which does not count) or
    0 for the compact realisation (whose loop counts up itself)."""
    tail, n = build_peeled_tail(plan)
    if tail is None:
        return None
    plan.peel_len = n
    new = list(instrs)
    iv_store = new[plan.iv_init_site]
    new[plan.iv_init_site] = IRStore(iv_store.base, iv_store.offset,
                                     Const(iv_init_value), iv_store.elem_bytes)
    return (new[:plan.region_lo] + vec_body + tail
            + new[plan.region_hi + 1:])

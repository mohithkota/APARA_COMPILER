"""
vector_remainder_peel.py -- Generalized Vector Remainder Peeling (R4.2.7, R4.4.5).

When `trip % lanes != 0` a vectorized kernel has leftover elements. The default is
to keep the original scalar loop for them, but that loop costs a full skeleton --
a compare, a branch, an IV load/add/store, and label boundaries the bundler cannot
pack across. PEELING deletes it and emits the (at most `lanes-1`) tail iterations
as straight-line code at constant indices.

R4.2.7 could only express `dest = f(loads)` where every load sat at the SAME
element index, which covered dot, reduction and elementwise but NOT

        Y[i] += a * X[i]

because `a` is an invariant scalar (not indexed) and `Y[i]` is read AND written.
AXPY and GEMM therefore kept scalar tails, and that was the sole cause of the
bundle-count growth reported in R4.3 and R4.4.

R4.4.5 GENERALIZES THE TEMPLATE so one framework covers every client. There is no
`AxpyPeeler` or `GemmPeeler`: clients describe their update declaratively and this
module is the only thing that emits scalar tail code.

    Y[i] = X[i]            operands=[array X]                dest=array Y
    Y[i] = X[i] + Z[i]     operands=[array X, array Z] op=+  dest=array Y
    Y[i] += X[i]           operands=[array X]                dest=array Y, dest_op=+
    Y[i] += a * X[i]       operands=[scalar a, array X] op=* dest=array Y, dest_op=+
    s    += A[i]*B[i]      operands=[array A, array B]  op=* dest=slot s,  dest_op=+

WHY DECLARATIVE AND NOT A PER-CLIENT EMITTER CALLBACK: the tail must reproduce the
ORIGINAL loop's integer promotion and sub-word truncation exactly. Clients record
the original instructions' own `elem_bytes`, `unsigned` flags and opcodes; this
module replays them. Letting each client emit its own tail would re-open exactly
the class of bug that made R4.1's narrow-accumulator dot diverge.

The one genuinely per-client concern -- how an array element's ADDRESS is formed --
is a callback (`PeelArray.offset_at`), because GEMM addresses a row
(`C[i*N + j]`) while the others address from the array base. The default covers
every client except GEMM.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import Const, Temp, IRAssign, IRLoad, IRStore, IRLoadAddr, IRBinOp, emit_array_base
from vector_lowering import _fresh


# ── descriptors are the shared expression-tree nodes (R4.5) ────────────────────
# R4.4.5 defined its own operand descriptors; R4.5 replaces them with the
# kernel-independent nodes from `expression_tree`, so ONE representation serves
# recognition, vector lowering and the remainder tail. The old names remain as
# aliases -- clients and tests written against R4.4.5 keep working.

import expression_tree as et
from expression_tree import ArrayRef, ScalarRef, Const as ExprConst, BinOp
from expression_lowering import lower_scalar

PeelArray = ArrayRef
PeelScalar = ScalarRef
PeelConst = ExprConst


class PeelTemplate:
    """One scalar element update, described declaratively.

        expr     an expression_tree node -- the value to compute
        dest     ArrayRef (indexed store) or ScalarRef (accumulator slot)
        dest_op  None -> dest = value ; (op, uns) -> dest = dest <op> value

    R4.5: `expr` may now be an arbitrary small tree (`a+b+c`, `a*b+c`, ...). The
    R4.4.5 form `PeelTemplate([operands], dest, op=..., dest_op=...)` is still
    accepted and is converted to a tree, so existing clients are unaffected."""
    __slots__ = ('expr', 'dest', 'dest_op')

    def __init__(self, expr=None, dest=None, op=None, dest_op=None,
                 operands=None):
        if operands is not None:                     # legacy keyword form
            expr = list(operands)
        if isinstance(expr, (list, tuple)):          # legacy operands + op form
            ops = list(expr)
            expr = ops[0] if op is None else BinOp(op[0], ops[0], ops[1],
                                                   bool(op[1]))
        self.expr = expr
        self.dest = dest
        self.dest_op = dest_op


def _load_at(slot, offset, elem_bytes, unsigned):
    base = _fresh('_vrpb')
    dest = _fresh('_vrpv')
    return [emit_array_base(base, slot),
            IRLoad(dest, base, offset, elem_bytes=elem_bytes,
                   unsigned=unsigned)], dest


def _store_at(slot, offset, value, elem_bytes):
    base = _fresh('_vrps')
    return [emit_array_base(base, slot), IRStore(base, offset, value, elem_bytes)]


def build_peeled_tail(plan):
    """Straight-line code for the `remainder` leftover elements, then the IV
    fix-up. The value is emitted by the SHARED recursive scalar lowering, so a
    client never writes tail code. Returns (instrs, n_ops) or (None, reason)."""
    tmpl = getattr(plan, 'peel', None)
    if tmpl is None:
        return None, 'no-peel-template'
    if plan.remainder <= 0:
        return None, 'no-remainder'

    out = []
    for r in range(plan.remainder):
        idx = plan.chunks * plan.lanes + r
        try:
            ins, val = lower_scalar(tmpl.expr, idx)
        except Exception as e:
            return None, f'peel-lowering:{e}'
        out += ins

        d = tmpl.dest
        if isinstance(d, ScalarRef):
            pre, d_off, d_eb = [], Const(0), d.elem_bytes
        else:
            pre, d_off = d.address(idx)
            d_eb = d.elem_bytes
        out += list(pre)
        if tmpl.dest_op is not None:            # read-modify-write
            lins, cur = _load_at(d.slot, d_off, d_eb,
                                 bool(getattr(d, 'unsigned', False)))
            out += lins
            op, uns = tmpl.dest_op
            nxt = _fresh('_vrpn')
            out.append(IRBinOp(nxt, op, cur, val, unsigned=uns))
            val = nxt
        out += _store_at(d.slot, d_off, val, d_eb)

    # The deleted scalar loop is what used to leave the IV at `trip`; restore it
    # so the vectorized function leaves memory identical to the scalar one.
    out += _store_at(plan.iv_slot, Const(0), Const(plan.trip), 8)
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

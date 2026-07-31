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

from ir import Const, Temp, IRAssign, IRLoad, IRStore, IRLoadAddr, IRBinOp
from vector_lowering import _fresh


# ── operand / destination descriptors ───────────────────────────────────────────

class PeelArray:
    """An element of a packed array at the peeled index.

    `offset_at(idx)` -> (instrs, offset_value) overrides address formation; the
    default is the constant byte offset `idx * elem_bytes` from the array base,
    which is what every client except GEMM needs."""
    __slots__ = ('slot', 'elem_bytes', 'unsigned', 'offset_at')

    def __init__(self, slot, elem_bytes, unsigned=False, offset_at=None):
        self.slot = slot
        self.elem_bytes = elem_bytes
        self.unsigned = unsigned
        self.offset_at = offset_at

    def address(self, idx):
        if self.offset_at is not None:
            return self.offset_at(idx)
        return [], Const(idx * self.elem_bytes)


class PeelScalar:
    """A loop-invariant scalar read from its own slot (AXPY's coefficient)."""
    __slots__ = ('slot', 'elem_bytes', 'unsigned')

    def __init__(self, slot, elem_bytes=8, unsigned=False):
        self.slot = slot
        self.elem_bytes = elem_bytes
        self.unsigned = unsigned


class PeelConst:
    """A literal coefficient (`Y[i] += 3*X[i]`)."""
    __slots__ = ('value',)

    def __init__(self, value):
        self.value = value


class PeelTemplate:
    """One scalar element update, described declaratively.

        operands  [PeelArray | PeelScalar | PeelConst], in the ORIGINAL order
        op        (opcode, unsigned) combining operands[0] and operands[1],
                  or None when there is a single operand
        dest      PeelArray (indexed store) or PeelScalar (accumulator slot)
        dest_op   None      -> dest = value            (plain assignment)
                  (op, uns) -> dest = dest <op> value  (read-modify-write)
    """
    __slots__ = ('operands', 'op', 'dest', 'dest_op')

    def __init__(self, operands, dest, op=None, dest_op=None):
        self.operands = list(operands)
        self.dest = dest
        self.op = op
        self.dest_op = dest_op


# ── the single emitter ──────────────────────────────────────────────────────────

def _load_at(slot, offset, elem_bytes, unsigned):
    base = _fresh('_vrpb')
    dest = _fresh('_vrpv')
    return [IRLoadAddr(base, slot),
            IRLoad(dest, base, offset, elem_bytes=elem_bytes,
                   unsigned=unsigned)], dest


def _store_at(slot, offset, value, elem_bytes):
    base = _fresh('_vrps')
    return [IRLoadAddr(base, slot), IRStore(base, offset, value, elem_bytes)]


def _emit_operand(o, idx):
    """(instrs, value_temp_or_const) for one operand at element index `idx`."""
    if isinstance(o, PeelConst):
        t = _fresh('_vrpk')
        return [IRAssign(t, Const(o.value))], t
    if isinstance(o, PeelScalar):
        ins, t = _load_at(o.slot, Const(0), o.elem_bytes, o.unsigned)
        return ins, t
    pre, off = o.address(idx)
    ins, t = _load_at(o.slot, off, o.elem_bytes, o.unsigned)
    return list(pre) + ins, t


def build_peeled_tail(plan):
    """Straight-line code for the `remainder` leftover elements, then the IV
    fix-up. Returns (instrs, n_ops) or (None, reason)."""
    tmpl = getattr(plan, 'peel', None)
    if tmpl is None:
        return None, 'no-peel-template'
    if plan.remainder <= 0:
        return None, 'no-remainder'
    if not tmpl.operands or (tmpl.op is not None and len(tmpl.operands) != 2):
        return None, 'malformed-peel-template'

    out = []
    for r in range(plan.remainder):
        idx = plan.chunks * plan.lanes + r
        vals = []
        for o in tmpl.operands:
            ins, v = _emit_operand(o, idx)
            out += ins
            vals.append(v)
        if tmpl.op is None:
            val = vals[0]
        else:
            op, uns = tmpl.op
            val = _fresh('_vrpr')
            out.append(IRBinOp(val, op, vals[0], vals[1], unsigned=uns))

        d = tmpl.dest
        if isinstance(d, PeelScalar):
            d_off, d_eb = Const(0), d.elem_bytes
            pre = []
        else:
            pre, d_off = d.address(idx)
            d_eb = d.elem_bytes
            out += list(pre)
        if tmpl.dest_op is not None:            # read-modify-write
            ins, cur = _load_at(d.slot, d_off, d_eb,
                                bool(getattr(d, 'unsigned', False)))
            out += ins
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

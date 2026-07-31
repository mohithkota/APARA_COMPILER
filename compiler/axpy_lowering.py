"""
axpy_lowering.py -- AXPY Pattern Matching & Lowering (R4.3).

Recognises and lowers

        for (i)  Y[i] += a * X[i];

over PACKED 1-D arrays, where `a` is a loop-invariant scalar. This is the first
production client of `vector_affine` (R4.2.8): every access decision here --
contiguous, invariant, strided -- is delegated to that analysis, and none of the
old ad-hoc `iv_terms` matching is used.

    Y[i] += a*X[i]      ->      packed load X
                                $v * with $replicate(a)
                                packed load Y
                                $v +
                                packed store Y

`$replicate` broadcasts src2 across every lane (see codegen `_gen_IRVecArith` and
the R4.0 capability database), so the scalar is passed as src2 of the multiply.
No new vector instruction is introduced.

The same packed-layout constraint as R4.1/R4.2 applies: only 1-D arrays declared
with the packed typedef markers are stored contiguously, so only those can be
loaded and stored a lane-group at a time.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import Const, Temp, IRLoad, IRStore, IRLoadAddr, IRVecArith
from analysis import DefUse
from vector_capability import VectorCapability
from vector_capability_db import ELEMENT_TYPES
from vector_lowering import _fresh, _packed_load
from vector_elementwise_lowering import _packed_store
import vector_compact_loop as _vcl
from vector_affine import (LoopAffineContext, classify_access, resolve_offset,
                           CONTIGUOUS, INVARIANT)

_cap = VectorCapability()


def _cname(x):
    return type(x).__name__


def _region(desc):
    idx = []
    for b in desc.body_blocks:
        blk = desc.cfg.blocks[b]
        idx.extend(range(blk.lo, blk.hi + 1))
    return sorted(idx)


class AxpyPlan:
    __slots__ = ('ok', 'reason', 'vtype', 'lanes', 'eb', 'signed', 'trip',
                 'chunks', 'remainder', 'y_slot', 'x_slot', 'a_slot',
                 'a_eb', 'a_unsigned', 'a_const', 'iv_slot', 'iv_init_site', 'iv_bytes',
                 'region_lo', 'region_hi', 'realisation', 'compact_per_iter',
                 'y_off', 'x_off', 'row_based',
                 'unrolled_len', 'peel', 'peel_len')

    def __init__(self):
        self.ok = False
        self.reason = None
        self.realisation = None
        self.compact_per_iter = 0
        self.unrolled_len = 0
        self.a_const = None              # literal coefficient, if any
        self.y_off = self.x_off = None   # R4.4: offset temps (row-based GEMM)
        self.row_based = False           # R4.4: set by plan_gemm
        self.peel = None                 # R4.3 offers no peeled variant (see below)
        self.peel_len = 0

    def __repr__(self):
        if not self.ok:
            return f"Axpy(reject: {self.reason})"
        return (f"Axpy({self.vtype} x{self.lanes} chunks={self.chunks} "
                f"rem={self.remainder})")


def plan_axpy(desc, instrs, kernel, legality):
    """Match `Y[i] += a*X[i]` and extract what lowering needs, or reject."""
    p = AxpyPlan()
    p.vtype = kernel.vtype
    p.eb = kernel.elem_bytes
    p.lanes = legality.lanes
    e = ELEMENT_TYPES.get(kernel.vtype)
    p.signed = e['signed'] if e else True
    p.trip = kernel.trip
    if p.trip is None:
        p.reason = 'trip-unknown'
        return p
    p.chunks = p.trip // p.lanes
    p.remainder = p.trip % p.lanes
    if p.chunks < 1:
        p.reason = 'trip-smaller-than-lanes'
        return p
    if kernel.reduction_slot is not None:
        p.reason = 'has-scalar-reduction'         # not an AXPY
        return p

    lo, hi = desc.func_slice
    def_map = DefUse(instrs, lo, hi).single_defs()
    addr_slot = {n: instrs[i].fp_offset for n, i in def_map.items()
                 if _cname(instrs[i]) == 'IRLoadAddr'}
    ctx = LoopAffineContext(instrs, desc)        # THE affine analysis (R4.2.8)
    region = _region(desc)

    def slot_of(ins):
        base = getattr(ins, 'base', None)
        return addr_slot.get(base.name) if isinstance(base, Temp) else None

    # ── the single contiguous array store defines Y ───────────────────────────
    stores = [i for i in region if _cname(instrs[i]) == 'IRStore'
              and classify_access(instrs[i], ctx).kind == CONTIGUOUS]
    if len(stores) != 1:
        p.reason = f'expect-one-contiguous-store(got {len(stores)})'
        return p
    st = instrs[stores[0]]
    p.y_slot = slot_of(st)
    if p.y_slot is None or st.elem_bytes != p.eb:
        p.reason = 'store-not-a-local-packed-array'
        return p

    # ── value must be  load(Y[i]) + ( a * load(X[i]) )  (either order) ────────
    if not isinstance(st.src, Temp):
        p.reason = 'stored-value-not-a-temp'
        return p
    d = def_map.get(st.src.name)
    if d is None or _cname(instrs[d]) != 'IRBinOp' or instrs[d].op != '+':
        p.reason = 'value-is-not-an-accumulate'
        return p
    add = instrs[d]

    def is_y_load(t):
        if not isinstance(t, Temp):
            return False
        i = def_map.get(t.name)
        if i is None or _cname(instrs[i]) != 'IRLoad':
            return False
        a = classify_access(instrs[i], ctx)
        return (a.kind == CONTIGUOUS and slot_of(instrs[i]) == p.y_slot
                and instrs[i].elem_bytes == p.eb)

    if is_y_load(add.left):
        mul_t = add.right
    elif is_y_load(add.right):
        mul_t = add.left
    else:
        p.reason = 'no-contiguous-Y-reload'
        return p

    if not isinstance(mul_t, Temp):
        p.reason = 'product-not-a-temp'
        return p
    mi = def_map.get(mul_t.name)
    if mi is None or _cname(instrs[mi]) != 'IRBinOp' or instrs[mi].op != '*':
        p.reason = 'not-a-scalar-times-vector-product'
        return p
    mul = instrs[mi]

    # one factor CONTIGUOUS (X), the other INVARIANT (a) -- both decided by
    # vector_affine, never by inspecting index expressions here
    def kind_of(t):
        if isinstance(t, Const):
            return INVARIANT, 'const'               # Y[i] += 3*X[i]
        if not isinstance(t, Temp):
            return None, None
        i = def_map.get(t.name)
        if i is None or _cname(instrs[i]) != 'IRLoad':
            # not a load: invariant iff its VALUE cannot change in this loop
            return (INVARIANT, None) if not ctx.varies(t.name) else (None, None)
        k = classify_access(instrs[i], ctx).kind
        if k == CONTIGUOUS:
            return CONTIGUOUS, i
        # A scalar coefficient must be invariant BY VALUE. classify_access only
        # describes the ADDRESS pattern: a load of the IV's own slot sits at a
        # constant offset and so looks address-invariant, while its value changes
        # every iteration. `Y[i] += i*X[i]` is caught here rather than left to the
        # differential.
        return (INVARIANT, i) if not ctx.varies(t.name) else (None, None)

    lk, li = kind_of(mul.left)
    rk, ri = kind_of(mul.right)
    if lk == CONTIGUOUS and rk == INVARIANT:
        x_i, a_i = li, ri
    elif rk == CONTIGUOUS and lk == INVARIANT:
        x_i, a_i = ri, li
    else:
        p.reason = f'product-operands-not-(contiguous,invariant):({lk},{rk})'
        return p

    xl = instrs[x_i]
    p.x_slot = slot_of(xl)
    if p.x_slot is None or xl.elem_bytes != p.eb:
        p.reason = 'X-not-a-local-packed-array'
        return p
    if a_i == 'const':
        p.a_const = (mul.left if isinstance(mul.left, Const) else mul.right).value
        p.a_slot = p.a_eb = None
        p.a_unsigned = False
        a_i = None
    if a_i is None and p.a_const is None:
        p.reason = 'scalar-coefficient-not-a-slot-load'
        return p
    al = instrs[a_i] if a_i is not None else None
    if al is not None:
        p.a_slot = slot_of(al)
        if p.a_slot is None:
            p.reason = 'scalar-coefficient-not-a-local-slot'
            return p
        p.a_eb = al.elem_bytes
        p.a_unsigned = bool(getattr(al, 'unsigned', False))

    # ── the ISA must support $v * and $v + for this element type ──────────────
    for op in ('mul', 'add'):
        cap = _cap.can(op, p.vtype)
        if not cap.ok:
            p.reason = f'isa-unsupported:{op}/{p.vtype}:{cap.reason}'
            return p
        if cap.lanes != p.lanes:
            p.reason = 'lane-count-disagreement'
            return p

    # ── IV init site (same requirement and reasoning as R4.1/R4.2) ────────────
    p.iv_slot = desc.primary_iv
    # R6.2C / defect D2: the compact chunk loop REUSES this slot, so it must
    # access it at exactly the width the scalar code does. A width mismatch on a
    # 64-bit DMEM word reads the wrong half (and yields 0), which is what broke
    # packed GEMM wherever the compact realisation was selected.
    p.iv_bytes = _vcl.slot_width(instrs, lo, hi, p.iv_slot)
    hblk = desc.cfg.blocks[desc.header]
    p.iv_init_site = None
    for k in range(hblk.lo - 1, lo - 1, -1):
        ins = instrs[k]
        if (_cname(ins) == 'IRStore' and isinstance(ins.base, Temp)
                and addr_slot.get(ins.base.name) == p.iv_slot
                and isinstance(ins.offset, Const) and ins.offset.value == 0):
            p.iv_init_site = k
            break
    if p.iv_init_site is None:
        p.reason = 'iv-init-not-found'
        return p

    p.region_lo = hblk.lo
    p.region_hi = desc.cfg.blocks[desc.latches[0]].hi

    # R4.4.5: `Y[i] += a*X[i]` is now expressible by the shared remainder
    # framework -- an invariant scalar operand and a read-modify-write array
    # destination. Operands are recorded in the ORIGINAL order so a
    # non-commutative opcode would still replay faithfully.
    from vector_remainder_peel import (PeelTemplate, PeelArray, PeelScalar,
                                       PeelConst)
    coeff = (PeelConst(p.a_const) if p.a_const is not None
             else PeelScalar(p.a_slot, p.a_eb, p.a_unsigned))
    xop = PeelArray(p.x_slot, p.eb, not p.signed)
    ops = ([coeff, xop] if isinstance(mul.left, Const)
           or (isinstance(mul.left, Temp) and a_i is not None
               and def_map.get(mul.left.name) == a_i)
           else [xop, coeff])
    p.peel = PeelTemplate(
        operands=ops,
        op=('*', bool(getattr(mul, 'unsigned', False))),
        dest=PeelArray(p.y_slot, p.eb, not p.signed),
        dest_op=('+', bool(getattr(add, 'unsigned', False))))
    p.ok = True
    return p


# ── the packed AXPY body ────────────────────────────────────────────────────────

def _load_scalar(plan):
    """Materialise the invariant coefficient ONCE, ahead of the vector body."""
    val = _fresh('_vav')
    if plan.a_const is not None:
        from ir import IRAssign
        return [IRAssign(val, Const(plan.a_const))], val
    base = _fresh('_vab')
    return [IRLoadAddr(base, plan.a_slot),
            IRLoad(val, base, Const(0), elem_bytes=plan.a_eb,
                   unsigned=plan.a_unsigned)], val


def _chunk(plan, a_val, load_x, load_y, store_y):
    """One chunk: X*replicate(a) + Y -> Y. The three access emitters are passed
    in so the same body serves the unrolled (constant offset) and compact
    (register offset) realisations."""
    body = []
    xT, yT = _fresh('_vax'), _fresh('_vay')
    body += load_x(xT)
    body += load_y(yT)
    prod = _fresh('_vap')
    # $replicate broadcasts src2 -> the scalar must be src2 of the multiply
    body.append(IRVecArith(prod, '*', xT, a_val, '$' + plan.vtype,
                           replicate=True))
    acc = _fresh('_vaa')
    body.append(IRVecArith(acc, '+', yT, prod, '$' + plan.vtype))
    body += store_y(acc)
    return body


def build_unrolled(plan):
    pre, a_val = _load_scalar(plan)
    out = list(pre)
    for c in range(plan.chunks):
        out += _chunk(
            plan, a_val,
            lambda t, c=c: _packed_load(t, plan.x_slot, c, plan.lanes, plan.eb,
                                        plan.signed),
            lambda t, c=c: _packed_load(t, plan.y_slot, c, plan.lanes, plan.eb,
                                        plan.signed),
            lambda v, c=c: _packed_store(plan.y_slot, c, v, plan.lanes, plan.eb))
    plan.unrolled_len = len(out)
    return out


def build_compact(plan):
    pre, a_val = _load_scalar(plan)

    def emit(off):
        return _chunk(
            plan, a_val,
            lambda t: _vcl.packed_load_at(t, plan.x_slot, off, plan.lanes,
                                          plan.eb, plan.signed),
            lambda t: _vcl.packed_load_at(t, plan.y_slot, off, plan.lanes,
                                          plan.eb, plan.signed),
            lambda v: _vcl.packed_store_at(plan.y_slot, off, v, plan.lanes,
                                           plan.eb))

    loop, per_iter = _vcl.build_compact_chunk_loop(plan.iv_slot, plan.eb,
                                                   plan.lanes, plan.chunks, emit,
                                                   iv_bytes=plan.iv_bytes)
    plan.compact_per_iter = per_iter
    return pre + loop


def _splice(instrs, plan, body, iv_init_value):
    new = list(instrs)
    iv = new[plan.iv_init_site]
    new[plan.iv_init_site] = IRStore(iv.base, iv.offset, Const(iv_init_value),
                                     iv.elem_bytes)
    if plan.remainder == 0:
        return new[:plan.region_lo] + body + new[plan.region_hi + 1:]
    return new[:plan.region_lo] + body + new[plan.region_lo:]


def lower_axpy(instrs, lo, hi, plan, global_base=0x400):
    """Vectorized function slice, or (None, reason).

    Offers the unrolled and compact realisations, plus their peeled variants when
    there is a remainder (R4.4.5 generalized the shared peel framework to express
    `Y[i] += a*X[i]`), and keeps whichever the R4.2.6 post-optimizer probe
    measures smaller."""
    if not plan.ok:
        return None, plan.reason
    from vector_remainder_peel import splice_peeled
    ub, cb = build_unrolled(plan), build_compact(plan)
    unrolled = _splice(instrs, plan, ub, plan.chunks * plan.lanes)
    compact = _splice(instrs, plan, cb, 0)
    cands = [('unrolled', unrolled), ('compact', compact)]
    if plan.remainder > 0:                      # R4.4.5: peel the scalar tail
        cands.append(('unrolled+peeled',
                      splice_peeled(instrs, plan, ub, plan.chunks * plan.lanes)))
        cands.append(('compact+peeled', splice_peeled(instrs, plan, cb, 0)))
    best, name, _s = _vcl.choose_smaller(cands, global_base)
    if best is None:
        return None, 'no-realisation-compiles'
    plan.realisation = name
    return best, f'ok:{name}'

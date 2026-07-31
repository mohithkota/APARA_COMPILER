"""
gemm_lowering.py -- Packed GEMM Recognition & Lowering (R4.4).

Supported shape -- the i-k-j ordering, over 1-D PACKED arrays:

    for (i)
      for (k) {
        s = A[i*N+k];
        for (j)  C[i*N+j] += s * B[k*N+j];
      }

GEMM IS AXPY OVER A ROW. The innermost j loop is exactly `Y[j] += a*X[j]` with
Y = C's i-th row and X = B's k-th row, so this module implements NO second vector
lowering. It reuses R4.3 wholesale:

    plan_axpy      the structural match (one contiguous store, value is
                   load(Y)+ (invariant * load(X)), ISA capability, IV init site)
    _load_scalar   materialising the invariant coefficient once
    _chunk         the body: X * $replicate(a) + Y -> Y

`_chunk` was already parameterised by three access emitters so the same body could
serve the unrolled and compact realisations; GEMM simply passes row-aware emitters.
That is the whole difference.

THE ONE THING GEMM ADDS: a row base. R4.3 addresses a chunk as
`slot + chunk*lanes*elem_bytes`, which assumes the invariant part of the offset is
ZERO. For `C[i*N+j]` the offset is `i*N + j`, so the row base `i*N` must be
included or the kernel reads and writes the wrong row. Rather than reconstruct the
base arithmetically, this module CLONES the loop's own offset computation with the
induction variable substituted -- the compiler already computes exactly the right
address, so the clone is correct by construction for any affine index the R4.2.8
analysis accepts, not just `i*N + j`.

Every access classification is `vector_affine`'s; this module introduces no
address recognizer of its own.

WHY OTHER LOOP ORDERINGS ARE REJECTED WITHOUT A SPECIAL CHECK: in i-j-k the
innermost loop is k, where `B[k*N+j]` steps by a whole row. `vector_affine` reports
that as STRIDED (coeff = N*elem_bytes, not elem_bytes), so the kernel is never
recognised as contiguous and is declined. The ordering requirement falls out of the
affine analysis rather than being pattern-matched separately.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import Const, Temp, IRLoad, IRStore, IRLoadAddr
from ir_utils import src_names
from analysis import DefUse
import vector_compact_loop as _vcl
from vector_affine import LoopAffineContext, classify_access, CONTIGUOUS
from axpy_lowering import plan_axpy, _load_scalar, _chunk, _fresh


def _cname(x):
    return type(x).__name__


# ── cloning the loop's own address computation ──────────────────────────────────

def clone_offset(instrs, def_map, region, offset, iv_slot, iv_value):
    """Re-emit the computation of `offset` with the induction variable replaced.

    `iv_value` is a Const (the unrolled realisation, which needs the address of a
    specific chunk) or None (the compact realisation, which re-loads the IV slot so
    the address tracks the loop). Returns (instrs, temp) or (None, reason).

    Only instructions INSIDE the loop body are cloned; anything defined outside is
    already available and is referenced as-is."""
    emitted, mapping = [], {}

    def walk(t, depth=0):
        if depth > 16:
            return None, 'offset-expression-too-deep'
        if isinstance(t, Const):
            return t, None
        if not isinstance(t, Temp):
            return None, 'offset-operand-not-a-value'
        if t.name in mapping:
            return mapping[t.name], None
        d = def_map.get(t.name)
        if d is None or d not in region:
            return t, None                      # computed outside the loop
        ins = instrs[d]
        c = _cname(ins)
        if c == 'IRLoadAddr':
            n = _fresh('_vgb')
            emitted.append(IRLoadAddr(n, ins.fp_offset))
            mapping[t.name] = n
            return n, None
        if c == 'IRLoad':
            base = getattr(ins, 'base', None)
            slot = None
            if isinstance(base, Temp):
                bd = def_map.get(base.name)
                if bd is not None and _cname(instrs[bd]) == 'IRLoadAddr':
                    slot = instrs[bd].fp_offset
            if slot == iv_slot and isinstance(getattr(ins, 'offset', None), Const) \
                    and ins.offset.value == 0:
                if iv_value is not None:        # unrolled: substitute the index
                    mapping[t.name] = iv_value
                    return iv_value, None
            nb, err = walk(base, depth + 1)     # otherwise re-emit the load
            if err:
                return None, err
            no, err = walk(getattr(ins, 'offset', Const(0)), depth + 1)
            if err:
                return None, err
            n = _fresh('_vgl')
            emitted.append(IRLoad(n, nb, no, elem_bytes=ins.elem_bytes,
                                  unsigned=bool(getattr(ins, 'unsigned', False))))
            mapping[t.name] = n
            return n, None
        if c == 'IRBinOp':
            l, err = walk(ins.left, depth + 1)
            if err:
                return None, err
            r, err = walk(ins.right, depth + 1)
            if err:
                return None, err
            n = _fresh('_vgo')
            from ir import IRBinOp
            emitted.append(IRBinOp(n, ins.op, l, r,
                                   unsigned=bool(getattr(ins, 'unsigned', False))))
            mapping[t.name] = n
            return n, None
        if c == 'IRAssign':
            return walk(ins.src, depth + 1)
        return None, f'offset-uses-{c}'

    res, err = walk(offset)
    if err:
        return None, err
    return emitted, res


# ── recognition ─────────────────────────────────────────────────────────────────

def plan_gemm(desc, instrs, kernel, legality):
    """A GEMM inner loop is an AXPY whose row base is non-zero. Reuses plan_axpy
    for the entire structural match and then records the offset expressions."""
    p = plan_axpy(desc, instrs, kernel, legality)
    if not p.ok:
        return p
    lo, hi = desc.func_slice
    def_map = DefUse(instrs, lo, hi).single_defs()
    ctx = LoopAffineContext(instrs, desc)
    region = set()
    for b in desc.body_blocks:
        blk = desc.cfg.blocks[b]
        region.update(range(blk.lo, blk.hi + 1))

    def offset_of(slot, want_store):
        for i in sorted(region):
            ins = instrs[i]
            if _cname(ins) != ('IRStore' if want_store else 'IRLoad'):
                continue
            base = getattr(ins, 'base', None)
            if not isinstance(base, Temp):
                continue
            bd = def_map.get(base.name)
            if bd is None or _cname(instrs[bd]) != 'IRLoadAddr':
                continue
            if instrs[bd].fp_offset != slot:
                continue
            if classify_access(ins, ctx).kind == CONTIGUOUS:
                return getattr(ins, 'offset', None)
        return None

    p.y_off = offset_of(p.y_slot, True)
    p.x_off = offset_of(p.x_slot, False)
    if p.y_off is None or p.x_off is None:
        p.ok = False
        p.reason = 'row-offset-not-recoverable'
        return p
    # A plain AXPY's offset is the bare induction term (`j` or `j*eb`); a GEMM row
    # access is a compound expression. Only the latter needs the row-aware
    # lowering, so R4.3 keeps its exact output. Decided structurally, without
    # consulting the pre-R4.2.8 `iv_terms` map.
    def _is_bare_iv(off):
        if not isinstance(off, Temp):
            return False
        d = def_map.get(off.name)
        if d is None:
            return False
        ins = instrs[d]
        if _cname(ins) == 'IRLoad':
            return _loads_iv(ins)
        if _cname(ins) == 'IRBinOp' and ins.op == '*':
            for c_, o_ in ((ins.right, ins.left), (ins.left, ins.right)):
                if isinstance(c_, Const) and isinstance(o_, Temp):
                    dd = def_map.get(o_.name)
                    if dd is not None and _cname(instrs[dd]) == 'IRLoad' \
                            and _loads_iv(instrs[dd]):
                        return True
        return False

    def _loads_iv(ins):
        base = getattr(ins, 'base', None)
        if not isinstance(base, Temp):
            return False
        bd = def_map.get(base.name)
        return (bd is not None and _cname(instrs[bd]) == 'IRLoadAddr'
                and instrs[bd].fp_offset == desc.primary_iv
                and isinstance(getattr(ins, 'offset', None), Const)
                and ins.offset.value == 0)

    p.row_based = not (_is_bare_iv(p.y_off) and _is_bare_iv(p.x_off))
    if not p.row_based:
        p.ok = False
        p.reason = 'not-row-based(plain-axpy)'
    p.iv_slot = desc.primary_iv
    return p


# ── lowering (reuses the AXPY body verbatim) ────────────────────────────────────

def _row_body(plan, instrs, def_map, region, a_val, iv_value):
    """One chunk at a row-aware address. `iv_value` is a Const for the unrolled
    realisation, or None so the compact realisation re-loads the IV slot."""
    pre_y, off_y = clone_offset(instrs, def_map, region, plan.y_off,
                                plan.iv_slot, iv_value)
    if pre_y is None:
        return None, off_y
    pre_x, off_x = clone_offset(instrs, def_map, region, plan.x_off,
                                plan.iv_slot, iv_value)
    if pre_x is None:
        return None, off_x
    body = list(pre_y) + list(pre_x)
    body += _chunk(
        plan, a_val,
        lambda t: _vcl.packed_load_at(t, plan.x_slot, off_x, plan.lanes,
                                      plan.eb, plan.signed),
        lambda t: _vcl.packed_load_at(t, plan.y_slot, off_y, plan.lanes,
                                      plan.eb, plan.signed),
        lambda v: _vcl.packed_store_at(plan.y_slot, off_y, v, plan.lanes,
                                       plan.eb))
    return body, None


def build_unrolled(plan, instrs, def_map, region):
    pre, a_val = _load_scalar(plan)
    out = list(pre)
    for c in range(plan.chunks):
        body, err = _row_body(plan, instrs, def_map, region, a_val,
                              Const(c * plan.lanes))
        if body is None:
            return None, err
        out += body
    plan.unrolled_len = len(out)
    return out, None


def build_compact(plan, instrs, def_map, region):
    pre, a_val = _load_scalar(plan)
    err_box = [None]

    def emit(_off):
        # the address comes from the cloned expression, which re-loads the IV
        # slot, so `build_compact_chunk_loop`'s own scaled offset is unused here
        body, err = _row_body(plan, instrs, def_map, region, a_val, None)
        if body is None:
            err_box[0] = err
            return []
        return body

    loop, per_iter = _vcl.build_compact_chunk_loop(plan.iv_slot, plan.eb,
                                                   plan.lanes, plan.chunks, emit)
    if err_box[0]:
        return None, err_box[0]
    plan.compact_per_iter = per_iter
    return pre + loop, None


def _splice(instrs, plan, body, iv_init_value):
    new = list(instrs)
    iv = new[plan.iv_init_site]
    new[plan.iv_init_site] = IRStore(iv.base, iv.offset, Const(iv_init_value),
                                     iv.elem_bytes)
    if plan.remainder == 0:
        return new[:plan.region_lo] + body + new[plan.region_hi + 1:]
    return new[:plan.region_lo] + body + new[plan.region_lo:]


def lower_gemm(instrs, lo, hi, plan, global_base=0x400):
    """Vectorized function slice, or (None, reason)."""
    if not plan.ok:
        return None, plan.reason
    def_map = DefUse(instrs, lo, hi).single_defs()
    region = set()
    # the region is recoverable from the spliced bounds recorded by plan_axpy
    region.update(range(plan.region_lo, plan.region_hi + 1))
    cands = []
    ub, err_u = build_unrolled(plan, instrs, def_map, region)
    if ub is not None:
        cands.append(('unrolled', _splice(instrs, plan, ub,
                                          plan.chunks * plan.lanes)))
    cb, err_c = build_compact(plan, instrs, def_map, region)
    if cb is not None:
        cands.append(('compact', _splice(instrs, plan, cb, 0)))
    if not cands:
        return None, err_u or err_c or 'no-realisation'
    best, name, _s = _vcl.choose_smaller(cands, global_base)
    if best is None:
        return None, 'no-realisation-compiles'
    plan.realisation = name
    return best, f'ok:{name}'

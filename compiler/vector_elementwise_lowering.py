"""
vector_elementwise_lowering.py -- Elementwise Vector Lowering (R4.2 Phase 2-3).

Pattern matching and lowering for the FOUR supported elementwise shapes:

    A[i] = B[i];            (copy)
    A[i] = B[i] + C[i];
    A[i] = B[i] - C[i];
    A[i] = B[i] * C[i];

Everything else is rejected with a specific reason. The lowered form is

    packed load  ->  $v arithmetic  ->  packed store        (+ scalar remainder)

which is the elementwise analogue of R4.1's `packed load -> $dot/$vreduce`. The
ISA question -- is `$v op` available and reliable for this element type? -- is
never answered here; it is delegated to the R4.0 capability layer.

THE SAME MEMORY REALITY AS R4.1 APPLIES, and it is the binding constraint:
ordinary C arrays are stored one element per 8-byte DMEM word (stride 8), so
consecutive elements are NOT contiguous. Only arrays declared with the packed
typedef markers (vu8_t/vi8_t/vu16_t/vi16_t/vu32_t/vi32_t) are tightly packed, and
only those can be gathered/scattered by a single 64-bit access. Elementwise
vectorization is therefore restricted to packed arrays, exactly as R4.1 was.

WHAT ELEMENTWISE ADDS OVER R4.1: a packed STORE. R4.1 read packed data and
reduced it into a scalar; here `lanes` results must be written back contiguously.
The store is an ordinary 64-bit IRStore (on hardware that writes the `lanes`
contiguous packed bytes); the `_vec_pack` marker exists only so the differential
oracle models the scatter (see vector_lowering.PackedVectorInterp).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import Const, Temp, IRLoad, IRStore, IRLoadAddr, IRVecArith
from analysis import DefUse
from vector_capability import VectorCapability
from vector_capability_db import ELEMENT_TYPES
from vector_lowering import _fresh, _packed_load
from vector_affine import classify_access, CONTIGUOUS
from expression_lowering import lower_vector

_cap = VectorCapability()

# the only value shapes this milestone supports
_BINOPS = {'+': 'add', '-': 'sub', '*': 'mul'}


def _cname(x):
    return type(x).__name__


def _region(desc):
    idx = []
    for b in desc.body_blocks:
        blk = desc.cfg.blocks[b]
        idx.extend(range(blk.lo, blk.hi + 1))
    return sorted(idx)


class ElementwisePlan:
    """The extracted shape of one elementwise loop, or a rejection reason."""
    __slots__ = ('ok', 'reason', 'op', 'vtype', 'lanes', 'eb', 'signed', 'trip',
                 'chunks', 'remainder', 'dst_slot', 'src_slots', 'acc_unused',
                 'iv_slot', 'iv_bytes', 'iv_init_site', 'region_lo', 'region_hi', 'body_len',
                 'realisation', 'compact_per_iter', 'peel', 'peel_len', 'expr',
                 'dst_off', 'shifted', '_instrs', '_defmap', '_region')

    def __init__(self):
        self.ok = False
        self.reason = None
        self.op = None                  # None = copy, else '+' / '-' / '*'
        self.body_len = 0
        self.realisation = None         # 'compact' | 'unrolled', set by lowering
        self.compact_per_iter = 0
        self.peel = None                # R4.2.7 PeelTemplate, or None
        self.expr = None                # R4.5 expression tree
        self.dst_off = None             # R4.6 store offset expression
        self.shifted = False            # R4.6 any non-bare offset?
        self._instrs = self._defmap = self._region = None
        self.peel_len = 0

    def __repr__(self):
        if not self.ok:
            return f"Elementwise(reject: {self.reason})"
        shape = 'copy' if self.op is None else f"'{self.op}'"
        return (f"Elementwise({shape} {self.vtype} x{self.lanes} "
                f"chunks={self.chunks} rem={self.remainder})")


def plan_elementwise(desc, instrs, kernel, legality):
    """Pattern-match ONE innermost loop against the four supported elementwise
    shapes and extract everything lowering needs. Rejects everything else."""
    p = ElementwisePlan()
    p.vtype = kernel.vtype
    p.eb = kernel.elem_bytes
    p.lanes = legality.lanes
    e = ELEMENT_TYPES.get(kernel.vtype)
    p.signed = e['signed'] if e else True

    # ── trip / chunking ───────────────────────────────────────────────────────
    p.trip = kernel.trip
    if p.trip is None:
        p.reason = 'trip-unknown'
        return p
    p.chunks = p.trip // p.lanes
    p.remainder = p.trip % p.lanes
    if p.chunks < 1:
        p.reason = 'trip-smaller-than-lanes'
        return p

    # a reduction is not an elementwise kernel (R4.1 owns those)
    if kernel.reduction_slot is not None:
        p.reason = 'has-reduction-accumulator'
        return p

    lo, hi = desc.func_slice
    du = DefUse(instrs, lo, hi)
    def_map = du.single_defs()
    addr_off = {n: instrs[i].fp_offset for n, i in def_map.items()
                if _cname(instrs[i]) == 'IRLoadAddr'}
    iv_terms = dict(desc.iv_terms)          # temp name -> (iv_slot, scale)
    region = _region(desc)

    def _packed_array_access(ins):
        """(slot, ok) for a load/store that is a CONTIGUOUS PACKED affine access:
        base is a local array slot and the offset is the IV scaled by exactly
        elem_bytes (stride == elem_bytes is what 'packed' means)."""
        base = getattr(ins, 'base', None)
        off = getattr(ins, 'offset', None)
        if not (isinstance(off, Temp) and off.name in iv_terms):
            return None, 'non-affine-access'
        if not (isinstance(base, Temp) and base.name in addr_off):
            return None, 'array-base-not-local-slot'
        _slot, scale = iv_terms[off.name]
        if scale != p.eb:
            return None, 'unpacked-array-stride'
        return addr_off[base.name], None

    # ── the single contiguous store defines the shape ─────────────────────────
    # R4.6.1: decided by vector_affine, not by the pre-R4.2.8 `iv_terms` map. A
    # 2-D-indexed store like `out[i*N+j]` has a SUM offset, which `iv_terms`
    # cannot represent -- so a 2-D stencil was counted as ZERO stores and declined
    # with 'expect-exactly-one-array-store(got 0)'. This is the last place in the
    # elementwise path that still used the old mechanism.
    import expression_tree as et
    ectx = et.ExprContext(instrs, desc, elem_bytes=p.eb)
    stores = [i for i in region if _cname(instrs[i]) == 'IRStore'
              and classify_access(instrs[i], ectx.affine).kind == CONTIGUOUS]
    if len(stores) != 1:
        p.reason = f'expect-exactly-one-contiguous-store(got {len(stores)})'
        return p
    st = instrs[stores[0]]
    p.dst_slot = ectx.slot_of(st)
    if p.dst_slot is None:
        p.reason = 'store-not-a-local-packed-array'
        return p
    if st.elem_bytes != p.eb:
        p.reason = 'store-width-mismatch'
        return p

    # ── the stored VALUE must be one of the four supported shapes ─────────────
    if not isinstance(st.src, Temp):
        p.reason = 'stored-value-not-a-temp'
        return p
    d = def_map.get(st.src.name)
    if d is None:
        p.reason = 'stored-value-has-no-single-def'
        return p
    vins = instrs[d]
    vcls = _cname(vins)

    # ── R4.5: the stored value is recognised as an EXPRESSION TREE ───────────
    # This replaces the old 1-or-2-operand matcher, so `a+b+c`, `a*b+c` and
    # `(a+b)*c` are accepted by the same code that always handled `a+b`.
    from expression_lowering import vector_feasible
    tree, why = et.build_expression(st.src, ectx)
    if tree is None:
        p.reason = f'value-shape:{why}'
        return p
    if isinstance(tree, (et.Const, et.ScalarRef)):
        p.reason = 'value-is-loop-invariant'
        return p
    ok, why = vector_feasible(tree)
    if not ok:
        p.reason = f'not-vectorizable:{why}'
        return p
    p.expr = tree
    p.op = tree.op if isinstance(tree, et.BinOp) else None
    refs = et.arrays(tree)
    p.src_slots = [a.slot for a in refs]

    # every ISA operation the tree needs must be supported for this element type
    for n in et.walk(tree):
        if isinstance(n, et.BinOp):
            cap = _cap.can(_BINOPS[n.op], p.vtype)
            if not cap.ok:
                p.reason = f'isa-unsupported:{_BINOPS[n.op]}/{p.vtype}:{cap.reason}'
                return p
            if cap.lanes != p.lanes:
                p.reason = 'lane-count-disagreement'
                return p

    # no OTHER array traffic may hide in the body: every affine load must be one
    # the tree consumes, or a lane could read data the vector form never gathers.
    affine_loads = [i for i in region if _cname(instrs[i]) == 'IRLoad'
                    and classify_access(instrs[i], ectx.affine).kind == CONTIGUOUS]
    if len(affine_loads) != len(refs):
        p.reason = (f'extra-array-traffic({len(affine_loads)} contiguous loads, '
                    f'{len(refs)} consumed)')
        return p

    # ── R4.6: honour a SHIFTED access (`in[i+1]`, `in[i+r]`) ─────────────────
    # A convolution tap is contiguous but its address is not `base + idx*eb`.
    # Rather than reconstruct it, re-emit the loop's OWN address computation with
    # the induction variable substituted -- the same `clone_offset` mechanism R4.4
    # introduced for GEMM row bases. Bare `IV`/`IV*const` offsets keep the
    # base-relative form, so R4.2/R4.5 output is unchanged.
    from gemm_lowering import clone_offset

    def _bare(off):
        if not isinstance(off, Temp):
            return False
        d0 = def_map.get(off.name)
        if d0 is None:
            return False
        ii = instrs[d0]
        if _cname(ii) == 'IRLoad':
            return _loads_iv(ii)
        if _cname(ii) == 'IRBinOp' and ii.op == '*':
            for cc, oo in ((ii.right, ii.left), (ii.left, ii.right)):
                if isinstance(cc, Const) and isinstance(oo, Temp):
                    dd = def_map.get(oo.name)
                    if dd is not None and _cname(instrs[dd]) == 'IRLoad' \
                            and _loads_iv(instrs[dd]):
                        return True
        return False

    def _loads_iv(ii):
        b = getattr(ii, 'base', None)
        if not isinstance(b, Temp):
            return False
        bd = def_map.get(b.name)
        return (bd is not None and _cname(instrs[bd]) == 'IRLoadAddr'
                and instrs[bd].fp_offset == desc.primary_iv
                and isinstance(getattr(ii, 'offset', None), Const)
                and ii.offset.value == 0)

    p._instrs, p._defmap, p._region = instrs, def_map, region
    p.dst_off = getattr(st, 'offset', None)
    offs = [a.offset_expr for a in refs] + [p.dst_off]
    p.shifted = any(o is not None and not _bare(o) for o in offs)

    def _at(off_expr):
        def at(idx):
            ins2, t2 = clone_offset(instrs, def_map, region, off_expr,
                                    desc.primary_iv, Const(idx))
            if ins2 is None:
                raise ValueError(t2)
            return ins2, t2
        return at

    if p.shifted:
        for o in offs:
            if o is None or clone_offset(instrs, def_map, region, o,
                                         desc.primary_iv, Const(0))[0] is None:
                p.reason = 'shifted-offset-not-clonable'
                return p
        from vector_affine import LoopAffineContext, resolve_offset
        import vector_capability_db as _vdb
        _WB = _vdb.WORD_BITS // 8
        _ctx = LoopAffineContext(instrs, desc)

        def _word_shift(oe):
            if oe is None: return 0
            acc = resolve_offset(oe, _ctx)
            if not acc.ok or acc.const_off is None: return None
            if acc.sym_div and acc.sym_div % _WB: return None
            return acc.const_off % _WB

        for _o in offs:
            if _word_shift(_o) is None:
                p.reason = 'shift-not-compile-time-constant'; return p
        if _word_shift(p.dst_off) != 0:
            p.reason = 'unaligned-vector-store'; return p
        tree = et.map_arrays(tree, lambda a: et.ArrayRef(
            a.slot, a.elem_bytes, a.unsigned, offset_at=_at(a.offset_expr),
            offset_expr=a.offset_expr,
            word_shift=(_word_shift(a.offset_expr) or 0)))
        p.expr = tree

    # R4.5 peel template: the SAME tree drives the scalar tail
    from vector_remainder_peel import PeelTemplate
    p.peel = PeelTemplate(expr=tree,
                          dest=et.ArrayRef(p.dst_slot, st.elem_bytes,
                                           bool(getattr(st, 'unsigned', False)),
                                           offset_at=(_at(p.dst_off)
                                                      if p.shifted else None)))

    # ── the IV init site (same requirement and reasoning as R4.1) ─────────────
    p.iv_slot = desc.primary_iv
    # R6.2C / defect D2, completed here. The compact chunk loop REUSES the
    # scalar loop's induction-variable slot, so it must access it at the width
    # the scalar code uses -- an 8-byte write and a 4-byte read of one 64-bit
    # DMEM word do not see the same bits. R6.2C fixed the dot/reduction, AXPY
    # and GEMM clients; this fourth client was missed. It stayed latent because
    # an elementwise body addresses chunks with the loop's OWN offset temp and
    # never re-reads the slot -- only a client that re-reads it through
    # `clone_offset` (a shifted convolution window) can observe the mismatch.
    import vector_compact_loop as _vcl_w      # local: _vcl imports this module
    p.iv_bytes = _vcl_w.slot_width(instrs, lo, hi, p.iv_slot)
    hblk = desc.cfg.blocks[desc.header]
    p.iv_init_site = None
    for k in range(hblk.lo - 1, lo - 1, -1):
        ins = instrs[k]
        if (_cname(ins) == 'IRStore' and isinstance(ins.base, Temp)
                and addr_off.get(ins.base.name) == p.iv_slot
                and isinstance(ins.offset, Const) and ins.offset.value == 0):
            p.iv_init_site = k
            break
    if p.iv_init_site is None:
        p.reason = 'iv-init-not-found'
        return p
    # R4.6.1: chunk addressing indexes elements as 0, lanes, 2*lanes, ... so the
    # induction variable must START AT 0. A loop like `for (j = 1; ...)` would be
    # lowered one element off; the differential caught it, but declining at match
    # time is cheaper and states the limit instead of burning a rollback.
    _iv0 = instrs[p.iv_init_site]
    if not (isinstance(getattr(_iv0, 'src', None), Const) and _iv0.src.value == 0):
        p.reason = 'iv-does-not-start-at-zero'
        return p

    # (the R4.5 peel template is built from the expression tree above)
    p.region_lo = hblk.lo
    p.region_hi = desc.cfg.blocks[desc.latches[0]].hi
    p.ok = True
    return p


# ── the packed elementwise body ─────────────────────────────────────────────────

def _packed_store(slot, chunk, value, lanes, eb):
    """Store one packed 64-bit result back into `lanes` contiguous elements."""
    base = _fresh('_vbs')
    la = IRLoadAddr(base, slot)
    st = IRStore(base, Const(chunk * lanes * eb), value, 8)
    st._vec_pack = (lanes, eb)
    return [la, st]


def build_elementwise_body(plan):
    """Straight-line packed chunks. The value is emitted by the SHARED recursive
    vector lowering, so no expression walking happens here."""
    body = []
    for c in range(plan.chunks):
        if plan.shifted:
            import vector_compact_loop as _v
            from gemm_lowering import clone_offset as _co

            def _ld(a, t, c=c):
                pre, off = a.offset_at(c * plan.lanes)
                return list(pre) + _v.packed_window_load_at(
                    t, a.slot, off, getattr(a, 'word_shift', 0), plan.lanes,
                    a.elem_bytes, not a.unsigned)
            ins, val, _sc = lower_vector(plan.expr, plan.vtype, _ld)
            if ins is None:
                raise ValueError(val)
            body += ins
            pre, doff = plan.peel.dest.offset_at(c * plan.lanes)
            body += list(pre) + _v.packed_store_at(plan.dst_slot, doff, val,
                                                   plan.lanes, plan.eb)
        else:
            ins, val, _sc = lower_vector(
                plan.expr, plan.vtype,
                lambda a, t, c=c: _packed_load(t, a.slot, c, plan.lanes,
                                               a.elem_bytes, not a.unsigned))
            if ins is None:
                raise ValueError(val)
            body += ins
            body += _packed_store(plan.dst_slot, c, val, plan.lanes, plan.eb)
    plan.body_len = len(body)
    return body


def build_compact_elementwise_body(plan):
    """R4.2.5: the body of ONE chunk of a compact vector loop, addressed by a
    register offset instead of a constant. Elementwise has no loop-carried value
    at all (each chunk is independent), so the compact form needs nothing beyond
    the offset change."""
    import vector_compact_loop as _vcl

    def emit(off):
        import vector_compact_loop as _v
        if plan.shifted:
            # R4.6: re-emit each access's own address computation, re-loading the
            # IV slot (iv_value=None) so it tracks the compact loop.
            from gemm_lowering import clone_offset as _co

            def _ld(a, t):
                pre, o = _co(plan._instrs, plan._defmap, plan._region,
                             a.offset_expr, plan.iv_slot, None)
                if pre is None:
                    raise ValueError(o)
                return list(pre) + _v.packed_window_load_at(
                    t, a.slot, o, getattr(a, 'word_shift', 0), plan.lanes,
                                                     a.elem_bytes, not a.unsigned)
            ins, val, _sc = lower_vector(plan.expr, plan.vtype, _ld)
            if ins is None:
                raise ValueError(val)
            pre, doff = _co(plan._instrs, plan._defmap, plan._region,
                            plan.dst_off, plan.iv_slot, None)
            if pre is None:
                raise ValueError(doff)
            return list(ins) + list(pre) + _v.packed_store_at(
                plan.dst_slot, doff, val, plan.lanes, plan.eb)
        ins, val, _sc = lower_vector(
            plan.expr, plan.vtype,
            lambda a, t: _v.packed_window_load_at(
                t, a.slot, off, getattr(a, 'word_shift', 0), plan.lanes,
                                           a.elem_bytes, not a.unsigned))
        if ins is None:
            raise ValueError(val)
        return list(ins) + _v.packed_store_at(plan.dst_slot, off, val,
                                              plan.lanes, plan.eb)

    loop, per_iter = _vcl.build_compact_chunk_loop(plan.iv_slot, plan.eb,
                                                   plan.lanes, plan.chunks, emit,
                                                   iv_bytes=plan.iv_bytes)
    plan.compact_per_iter = per_iter
    return loop


def _splice_unrolled(instrs, plan, vec_body):
    """The R4.2 realisation: straight-line chunks, IV init rewritten to
    chunks*lanes (where the scalar remainder resumes)."""
    new = list(instrs)
    iv_store = new[plan.iv_init_site]
    new[plan.iv_init_site] = IRStore(iv_store.base, iv_store.offset,
                                     Const(plan.chunks * plan.lanes),
                                     iv_store.elem_bytes)
    if plan.remainder == 0:
        return new[:plan.region_lo] + vec_body + new[plan.region_hi + 1:]
    return new[:plan.region_lo] + vec_body + new[plan.region_lo:]


def _splice_compact(instrs, plan, vec_loop):
    """The R4.2.5 realisation: a compact loop over the kernel's OWN IV slot, which
    counts to chunks*lanes itself -- so the IV init store stays at 0 and the
    scalar remainder resumes with no fix-up."""
    new = list(instrs)
    if plan.remainder == 0:
        return new[:plan.region_lo] + vec_loop + new[plan.region_hi + 1:]
    return new[:plan.region_lo] + vec_loop + new[plan.region_lo:]


def lower_elementwise(instrs, lo, hi, desc, kernel, legality, plan,
                      global_base=0x400):
    """Produce the vectorized function slice (list) or (None, reason). Proving
    correctness is the pipeline's job (the differential oracle).

    R4.2.5: builds BOTH realisations and keeps whichever compiles to fewer
    bundles (ties -> unrolled: at equal size the loop is strictly slower, so
    compact must earn the switch)."""
    if not plan.ok:
        return None, plan.reason

    import vector_compact_loop as _vcl
    from vector_remainder_peel import splice_peeled
    compact_body = build_compact_elementwise_body(plan)
    unrolled_body = build_elementwise_body(plan)
    plan.body_len = len(unrolled_body)
    compact = _splice_compact(instrs, plan, compact_body)
    unrolled = _splice_unrolled(instrs, plan, unrolled_body)
    # R4.2.7: with a remainder, the scalar tail loop can be PEELED away instead of
    # kept. Both realisations get a peeled variant; the selector decides. The
    # unrolled body starts at chunks*lanes (it does not count), the compact loop
    # counts up from 0 itself.
    cands = [('unrolled', unrolled), ('compact', compact)]
    if plan.remainder > 0:
        cands.append(('unrolled+peeled',
                      splice_peeled(instrs, plan, unrolled_body,
                                    plan.chunks * plan.lanes)))
        cands.append(('compact+peeled',
                      splice_peeled(instrs, plan, compact_body, 0)))
    best, name, _scores = _vcl.choose_smaller(cands, global_base)
    if best is None:
        return None, 'no-realisation-compiles'
    plan.realisation = name
    return best, f'ok:{name}'

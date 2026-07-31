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
                 'iv_slot', 'iv_init_site', 'region_lo', 'region_hi', 'body_len',
                 'realisation', 'compact_per_iter', 'peel', 'peel_len')

    def __init__(self):
        self.ok = False
        self.reason = None
        self.op = None                  # None = copy, else '+' / '-' / '*'
        self.body_len = 0
        self.realisation = None         # 'compact' | 'unrolled', set by lowering
        self.compact_per_iter = 0
        self.peel = None                # R4.2.7 PeelTemplate, or None
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

    # ── the single affine store defines the shape ─────────────────────────────
    stores = [i for i in region if _cname(instrs[i]) == 'IRStore'
              and isinstance(getattr(instrs[i], 'offset', None), Temp)
              and instrs[i].offset.name in iv_terms]
    if len(stores) != 1:
        p.reason = f'expect-exactly-one-array-store(got {len(stores)})'
        return p
    st = instrs[stores[0]]
    p.dst_slot, why = _packed_array_access(st)
    if p.dst_slot is None:
        p.reason = f'store:{why}'
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

    operand_temps = []
    if vcls == 'IRLoad':
        p.op = None                                     # A[i] = B[i]
        operand_temps = [st.src]
    elif vcls == 'IRBinOp' and vins.op in _BINOPS:
        p.op = vins.op                                  # A[i] = B[i] op C[i]
        if not (isinstance(vins.left, Temp) and isinstance(vins.right, Temp)):
            p.reason = f'operand-not-a-temp(op {vins.op})'
            return p
        operand_temps = [vins.left, vins.right]
    else:
        detail = vins.op if vcls == 'IRBinOp' else vcls
        p.reason = f'unsupported-value-shape:{detail}'
        return p

    # every operand must itself be a packed affine load of the SAME width
    p.src_slots = []
    src_info = []                       # (slot, elem_bytes, unsigned) per operand
    for t in operand_temps:
        di = def_map.get(t.name)
        if di is None or _cname(instrs[di]) != 'IRLoad':
            p.reason = 'operand-not-an-array-load'
            return p
        ld = instrs[di]
        slot, why = _packed_array_access(ld)
        if slot is None:
            p.reason = f'operand:{why}'
            return p
        if ld.elem_bytes != p.eb:
            p.reason = 'operand-width-mismatch'
            return p
        p.src_slots.append(slot)
        src_info.append((slot, ld.elem_bytes,
                         bool(getattr(ld, 'unsigned', False))))

    # no OTHER array traffic may hide in the body: every affine load must be one
    # we consume, or a lane could read data the vector form never gathers.
    affine_loads = [i for i in region if _cname(instrs[i]) == 'IRLoad'
                    and isinstance(getattr(instrs[i], 'offset', None), Temp)
                    and instrs[i].offset.name in iv_terms]
    if len(affine_loads) != len(operand_temps):
        p.reason = (f'extra-array-traffic({len(affine_loads)} affine loads, '
                    f'{len(operand_temps)} consumed)')
        return p

    # ── the ISA must actually support this op for this element type ───────────
    if p.op is not None:
        cap = _cap.can(_BINOPS[p.op], p.vtype)
        if not cap.ok:
            p.reason = f'isa-unsupported:{_BINOPS[p.op]}/{p.vtype}:{cap.reason}'
            return p
        if cap.lanes != p.lanes:
            p.reason = 'lane-count-disagreement'
            return p

    # ── the IV init site (same requirement and reasoning as R4.1) ─────────────
    p.iv_slot = desc.primary_iv
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

    # R4.2.7 peel template: replay the ORIGINAL loads/binop/store attributes at a
    # constant index rather than re-deriving the tail.
    from vector_remainder_peel import PeelTemplate, PeelArray
    p.peel = PeelTemplate(
        operands=[PeelArray(sl, eb_, un_) for (sl, eb_, un_) in src_info],
        op=(None if p.op is None
            else (p.op, bool(getattr(vins, 'unsigned', False)))),
        dest=PeelArray(p.dst_slot, st.elem_bytes))

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
    """The straight-line packed vector instructions processing the first
    `chunks*lanes` elements. The kept scalar loop (IV re-initialised to
    chunks*lanes) handles the remainder, exactly as in R4.1."""
    body = []
    for c in range(plan.chunks):
        loaded = []
        for slot in plan.src_slots:
            t = _fresh('_vea')
            body += _packed_load(t, slot, c, plan.lanes, plan.eb, plan.signed)
            loaded.append(t)
        if plan.op is None:
            result = loaded[0]                          # copy: store what we read
        else:
            result = _fresh('_ver')
            body.append(IRVecArith(result, plan.op, loaded[0], loaded[1],
                                   '$' + plan.vtype))
        body += _packed_store(plan.dst_slot, c, result, plan.lanes, plan.eb)
    plan.body_len = len(body)
    return body


def build_compact_elementwise_body(plan):
    """R4.2.5: the body of ONE chunk of a compact vector loop, addressed by a
    register offset instead of a constant. Elementwise has no loop-carried value
    at all (each chunk is independent), so the compact form needs nothing beyond
    the offset change."""
    import vector_compact_loop as _vcl

    def emit(off):
        body = []
        loaded = []
        for slot in plan.src_slots:
            t = _fresh('_vea')
            body += _vcl.packed_load_at(t, slot, off, plan.lanes, plan.eb,
                                        plan.signed)
            loaded.append(t)
        if plan.op is None:
            result = loaded[0]                      # copy: store what we read
        else:
            result = _fresh('_ver')
            body.append(IRVecArith(result, plan.op, loaded[0], loaded[1],
                                   '$' + plan.vtype))
        body += _vcl.packed_store_at(plan.dst_slot, off, result, plan.lanes,
                                     plan.eb)
        return body

    loop, per_iter = _vcl.build_compact_chunk_loop(plan.iv_slot, plan.eb,
                                                   plan.lanes, plan.chunks, emit)
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

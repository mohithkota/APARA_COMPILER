"""
vector_lowering.py -- Vector Lowering for Dot Product & Sum Reduction (R4.1).

Lowers a detected + legal + profitable dot-product or sum-reduction loop over a
PACKED array to APARA vector instructions, keeping a scalar remainder loop for the
tail iterations. Emits ONLY the two supported forms; no general vectorization.

APARA MEMORY REALITY (discovered in R4.0/R4.1 from the backend): ordinary C arrays
are stored one element per 8-byte DMEM word (stride 8), so consecutive elements are
NOT contiguous and cannot be gathered by a single load. Only arrays declared with
the packed typedef markers (vu8_t/vi8_t/vu16_t/vi16_t/vu32_t/vi32_t) are stored
tightly packed (stride = element size), so `lanes = 8/elem_bytes` consecutive
elements fill one 64-bit word -- exactly what `$dot`/`$vreduce` consume. This pass
therefore vectorizes ONLY packed arrays.

  dot:        acc += A[i]*B[i]   ->   packed loads of A,B chunks + $dot $accumulate
  reduction:  acc += A[i]        ->   packed load of A chunk + $vreduce (+ scalar add)

A packed 64-bit load is a normal IRLoad (elem_bytes=8) -- on hardware it reads the
contiguous packed bytes; for the differential oracle we model the gather (the
frozen VectorInterp is EXTENDED here by subclass, not modified).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import (Const, Temp, IRLoad, IRStore, IRLoadAddr, IRVecDot, IRVecReduce)
from ir_utils import func_slices
from analysis import DefUse
from loopopt.analysis_iv import annotate_induction_vars, TripCount
from loopopt.analysis_mem import annotate_memory_effects
from loopopt.discovery import discover_function
from vector_capability import VectorCapability
from vector_capability_db import ELEMENT_TYPES
import vector_validation as _vv

_cap = VectorCapability()
_vec_n = [0]


def _fresh(prefix='_vk'):
    _vec_n[0] += 1
    return Temp(f"{prefix}{_vec_n[0]}")


# ── packed-array-aware interpreter (extends the frozen VectorInterp) ─────────────

class PackedVectorInterp(_vv.VectorInterp):
    """VectorInterp + the packed 64-bit load AND store: an access marked
    `_vec_pack=(lanes, elem_bytes)` gathers/scatters `lanes` contiguous packed
    elements through one register, modelling the hardware's contiguous packed DMEM
    (the interpreter stores each element at its own byte address).

    The load is R4.1's (dot/reduction only ever READ packed data). R4.2's
    elementwise kernels also WRITE, so the symmetric scatter lives here too --
    additive, and inert for R4.1 because dot/reduction never mark a store."""

    def _exec_data(self, ins, c, mem, regs):
        pk = getattr(ins, '_vec_pack', None)
        if c == 'IRLoad' and pk is not None:
            lanes, eb = pk
            base = self._val(mem, regs, ins.base) + self._val(mem, regs, ins.offset)
            emask = (1 << (8 * eb)) - 1
            packed = 0
            for i in range(lanes):
                e = mem.get(base + i * eb, 0) & emask
                packed |= e << (i * eb * 8)
            regs[ins.dest.name] = _vv.ir_interp._to_signed(packed)
        elif c == 'IRStore' and pk is not None:
            # Scatter: unpack `lanes` fields out of the 64-bit value and write each
            # to its own element address. Each lane is truncated EXACTLY as the
            # scalar store would be (`_trunc(v, eb, unsigned=False)`), so the
            # vector and scalar forms leave byte-identical memory -- which is what
            # the differential then checks.
            lanes, eb = pk
            base = self._val(mem, regs, ins.base) + self._val(mem, regs, ins.offset)
            packed = self._val(mem, regs, ins.src) & _vv._MASK64
            bits = 8 * eb
            emask = (1 << bits) - 1
            for i in range(lanes):
                mem[base + i * eb] = _vv.ir_interp._trunc(
                    (packed >> (i * bits)) & emask, eb, unsigned=False)
        else:
            super()._exec_data(ins, c, mem, regs)


def run_slice_packed(instrs, lo, hi, init_mem=None, step_limit=2_000_000):
    mem = dict(init_mem) if init_mem else {}
    ret = PackedVectorInterp(instrs, step_limit).run_function(lo, hi, mem)
    return ret, mem


def differential_packed(scalar_instrs, vector_instrs, lo, hi, seeds=6):
    """differential_vector, but using the packed-aware interpreter for the vector
    side. Returns ('match'|'mismatch'|'unsupported', detail)."""
    import random
    fname = getattr(scalar_instrs[lo], 'name', None)
    a1 = next((a for a, b in func_slices(vector_instrs)
               if getattr(vector_instrs[a], 'name', None) == fname), None)
    if a1 is None:
        return 'mismatch', 'vector slice missing'
    b1 = next(b for a, b in func_slices(vector_instrs) if a == a1)
    rng = random.Random(0x4EC1)
    ran = 0
    for s in range(seeds):
        seed = dict(_vv.ir_interp._preload_globals(scalar_instrs))
        if s:
            # full byte/half-word range so sign/zero-extension differences surface
            hi_val = [127, 255, 200, 100, 32000][s % 5]
            for addr in range(-8192, 8192, 1):
                seed.setdefault(addr, rng.randint(-hi_val, hi_val))
        try:
            r0, m0 = _vv.ir_interp.run_slice(scalar_instrs, lo, hi, init_mem=dict(seed))
            r1, m1 = run_slice_packed(vector_instrs, a1, b1, init_mem=dict(seed))
        except (_vv.ir_interp.Unsupported, _vv.ir_interp.StepLimit):
            continue
        ran += 1
        if r0 != r1:
            return 'mismatch', f'return {r0} != {r1} (seed {s})'
        if m0 != m1:
            diff = {k: (m0.get(k), m1.get(k)) for k in set(m0) | set(m1)
                    if m0.get(k) != m1.get(k)}
            return 'mismatch', f'memory differs {sorted(diff)[:4]} (seed {s})'
    return ('match' if ran else 'unsupported', f'{ran} seeds')


# ── extraction of the packed kernel's operands from the scalar loop ─────────────

def _cname(x):
    return type(x).__name__


def _region(desc):
    idx = []
    for b in desc.body_blocks:
        blk = desc.cfg.blocks[b]
        idx.extend(range(blk.lo, blk.hi + 1))
    return sorted(idx)


class LoweringPlan:
    __slots__ = ('ok', 'reason', 'kind', 'vtype', 'lanes', 'eb', 'signed',
                 'trip', 'chunks', 'remainder', 'array_slots', 'acc_slot',
                 'iv_slot', 'iv_init_site', 'region_lo', 'region_hi',
                 'realisation', 'compact_per_iter', 'unrolled_len')

    def __init__(self):
        self.ok = False
        self.reason = None
        self.realisation = None         # 'compact' | 'unrolled', set by lowering
        self.compact_per_iter = 0
        self.unrolled_len = 0


def plan_lowering(desc, instrs, kernel, legality):
    """Extract everything needed to lower one packed dot/reduction loop, or
    (ok=False, reason). Requires local packed arrays addressed by IRLoadAddr."""
    p = LoweringPlan()
    p.kind = kernel.kind
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

    lo, hi = desc.func_slice
    du = DefUse(instrs, lo, hi)
    addr_off = {n: instrs[i].fp_offset for n, i in du.single_defs().items()
                if _cname(instrs[i]) == 'IRLoadAddr'}
    iv_terms = set(desc.iv_terms.keys())

    # affine array loads: base must be a local IRLoadAddr slot; stride == elem_bytes
    array_slots = []
    for i in _region(desc):
        ins = instrs[i]
        if _cname(ins) != 'IRLoad':
            continue
        base = getattr(ins, 'base', None)
        off = getattr(ins, 'offset', None)
        is_affine = (isinstance(off, Temp) and off.name in iv_terms)
        if not is_affine or not isinstance(base, Temp) or base.name not in addr_off:
            continue
        # packed check: the index advances by elem_bytes per iteration, i.e. the
        # offset temp is the IV scaled by elem_bytes (stride == elem_bytes, packed)
        slot, scale = desc.iv_terms.get(off.name, (None, None))
        if scale != kernel.elem_bytes:
            p.reason = 'unpacked-array-stride'
            return p
        array_slots.append(addr_off[base.name])

    need = 2 if kernel.kind in ('dot-product',) else 1
    if len(array_slots) < need:
        p.reason = 'array-bases-not-extracted'
        return p
    p.array_slots = array_slots[:need]
    p.acc_slot = kernel.reduction_slot
    p.iv_slot = desc.primary_iv

    # locate the IV-init store (constant store to the IV slot before the header)
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

    p.region_lo = hblk.lo
    p.region_hi = desc.cfg.blocks[desc.latches[0]].hi
    p.ok = True
    return p


# ── the packed vector body ──────────────────────────────────────────────────────

def _packed_load(dest, slot, chunk, lanes, eb, signed):
    """A packed 64-bit load of `lanes` contiguous elements of chunk `chunk`."""
    base = _fresh('_vba')
    la = IRLoadAddr(base, slot)
    byte_off = chunk * lanes * eb
    ld = IRLoad(dest, base, Const(byte_off), elem_bytes=8, unsigned=(not signed))
    ld._vec_pack = (lanes, eb)
    return [la, ld]


def _acc_addr_load(slot, signed):
    """Load the current accumulator value from its slot -> (instrs, value_temp)."""
    base = _fresh('_vaa')
    val = _fresh('_vac')
    return [IRLoadAddr(base, slot),
            IRLoad(val, base, Const(0), elem_bytes=8, unsigned=(not signed))], val


def _acc_store(slot, value_temp):
    base = _fresh('_vas')
    return [IRLoadAddr(base, slot), IRStore(base, Const(0), value_temp, 8)]


def build_vector_body(plan):
    """The straight-line packed vector instructions that process the first
    `chunks*lanes` elements and store the partial accumulator. The kept scalar
    loop (with IV re-initialised to chunks*lanes) handles the remainder."""
    body = []
    init, acc = _acc_addr_load(plan.acc_slot, plan.signed)
    body += init
    for c in range(plan.chunks):
        if plan.kind == 'dot-product':
            aT, bT = _fresh('_vpa'), _fresh('_vpb')
            body += _packed_load(aT, plan.array_slots[0], c, plan.lanes, plan.eb, plan.signed)
            body += _packed_load(bT, plan.array_slots[1], c, plan.lanes, plan.eb, plan.signed)
            nxt = _fresh('_vacc')
            body.append(IRVecDot(nxt, aT, bT, '$' + plan.vtype,
                                 accumulate=True, accum=acc))
            acc = nxt
        else:  # sum-reduction
            aT = _fresh('_vpa')
            body += _packed_load(aT, plan.array_slots[0], c, plan.lanes, plan.eb, plan.signed)
            partial = _fresh('_vred')
            body.append(IRVecReduce(partial, aT, '$' + plan.vtype, '+'))
            nxt = _fresh('_vacc')
            from ir import IRBinOp
            body.append(IRBinOp(nxt, '+', acc, partial))
            acc = nxt
    body += _acc_store(plan.acc_slot, acc)
    return body


def build_compact_body(plan):
    """R4.2.5: the body of ONE chunk of a compact vector loop, addressed by a
    register offset instead of a constant. The accumulator lives in its memory
    slot across the back edge (loaded at the top, stored at the bottom) exactly as
    the scalar loop does -- so no loop-carried REGISTER is introduced and the
    R2.8-class live-range hazard cannot arise."""
    import vector_compact_loop as _vcl

    def emit(off):
        body = []
        init, acc = _vcl.slot_load(plan.acc_slot, plan.signed)
        body += init
        if plan.kind == 'dot-product':
            aT, bT = _fresh('_vpa'), _fresh('_vpb')
            body += _vcl.packed_load_at(aT, plan.array_slots[0], off,
                                        plan.lanes, plan.eb, plan.signed)
            body += _vcl.packed_load_at(bT, plan.array_slots[1], off,
                                        plan.lanes, plan.eb, plan.signed)
            nxt = _fresh('_vacc')
            body.append(IRVecDot(nxt, aT, bT, '$' + plan.vtype,
                                 accumulate=True, accum=acc))
        else:                                       # sum-reduction
            aT = _fresh('_vpa')
            body += _vcl.packed_load_at(aT, plan.array_slots[0], off,
                                        plan.lanes, plan.eb, plan.signed)
            partial = _fresh('_vred')
            body.append(IRVecReduce(partial, aT, '$' + plan.vtype, '+'))
            nxt = _fresh('_vacc')
            from ir import IRBinOp
            body.append(IRBinOp(nxt, '+', acc, partial))
        body += _vcl.slot_store(plan.acc_slot, nxt)
        return body

    loop, per_iter = _vcl.build_compact_chunk_loop(plan.iv_slot, plan.eb,
                                                   plan.lanes, plan.chunks, emit)
    plan.compact_per_iter = per_iter
    return loop


def _splice_unrolled(instrs, plan, vec_body):
    """The R4.1 realisation: straight-line chunks. The IV init store is REWRITTEN
    to chunks*lanes -- where the scalar loop resumes, and the value the IV slot
    must hold on exit so the vectorized function leaves memory identical."""
    new = list(instrs)
    from ir import IRStore as _S
    iv_store = new[plan.iv_init_site]
    new[plan.iv_init_site] = _S(iv_store.base, iv_store.offset,
                                Const(plan.chunks * plan.lanes),
                                iv_store.elem_bytes)
    if plan.remainder == 0:
        return new[:plan.region_lo] + vec_body + new[plan.region_hi + 1:]
    return new[:plan.region_lo] + vec_body + new[plan.region_lo:]


def _splice_compact(instrs, plan, vec_loop):
    """The R4.2.5 realisation: a compact loop over the kernel's OWN IV slot. The
    IV init store is left at 0 -- the loop counts up to chunks*lanes itself, so
    the scalar remainder resumes with no fix-up."""
    new = list(instrs)
    if plan.remainder == 0:
        return new[:plan.region_lo] + vec_loop + new[plan.region_hi + 1:]
    return new[:plan.region_lo] + vec_loop + new[plan.region_lo:]


def lower_kernel(instrs, lo, hi, plan, global_base=0x400):
    """Produce the vectorized function slice (list) or (None, reason). Analysis of
    correctness is the caller's job (differential_packed).

    Takes an already-matched LoweringPlan (the client builds it in match()).

    R4.2.5: builds BOTH the compact-loop and the fully-unrolled realisation and
    keeps whichever compiles to fewer bundles (ties -> unrolled: at equal size the
    loop is strictly slower, so compact must earn the switch). Neither form is
    assumed better: with few chunks the bundler packs the independent unrolled
    chunks into wide bundles and beats a loop's per-chunk compare/branch/update."""
    if not plan.ok:
        return None, plan.reason

    import vector_compact_loop as _vcl
    compact = _splice_compact(instrs, plan, build_compact_body(plan))
    _unrolled_body = build_vector_body(plan)
    plan.unrolled_len = len(_unrolled_body)
    unrolled = _splice_unrolled(instrs, plan, _unrolled_body)
    best, name, _scores = _vcl.choose_smaller(
        [('unrolled', unrolled), ('compact', compact)], global_base)
    if best is None:
        return None, 'no-realisation-compiles'
    plan.realisation = name
    return best, f'ok:{name}'

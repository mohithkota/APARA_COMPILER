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
                # MSB-first (R6.2D, established on the simulator): the element
                # at byte offset i*eb occupies bits [63-8*i*eb : ...].
                packed |= e << (64 - (i + 1) * eb * 8)
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
                    (packed >> (64 - (i + 1) * bits)) & emask, eb,
                    unsigned=False)
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
                 'trip', 'chunks', 'remainder', 'array_slots', 'array_offs',
                 'acc_slot',
                 'iv_slot', 'iv_init_site', 'region_lo', 'region_hi', 'iv_bytes',
                 'acc_bytes', 'acc_expand', 'acc_expand_reason',
                 'realisation', 'compact_per_iter', 'unrolled_len', 'peel',
                 'peel_len', 'array_addr', 'array_base_pre')

    def __init__(self):
        self.ok = False
        self.reason = None
        self.acc_expand = False         # R6.6, set by build_compact_body
        self.acc_expand_reason = None
        self.realisation = None         # 'compact' | 'unrolled', set by lowering
        self.compact_per_iter = 0
        self.unrolled_len = 0
        self.peel = None                # R4.2.7 PeelTemplate, or None
        self.peel_len = 0
        # R13.0: per-array INVARIANT byte-offset expression, positionally
        # aligned with `array_slots`. None means "no invariant part", i.e. the
        # pre-R13 form `offset == IV*elem_bytes`, and the lowering then takes
        # the byte-for-byte identical path it always did. A non-None entry is
        # the `invariant_base + IV*elem_bytes` form a matmul row/column needs.
        self.array_offs = []
        # R13.0: materialised `array base + invariant row offset` address temps,
        # positionally aligned with `array_slots`; None where `array_offs` is
        # None. Built once in the plan so the emission path stays a pure
        # function of the plan (it has no access to instrs/def_map).
        self.array_addr = []
        self.array_base_pre = []


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

    # Affine array loads: base must be a local IRLoadAddr slot, and the access
    # must be CONTIGUOUS in the loop IV (stride == elem_bytes).
    #
    # R13.0 generalisation. The pre-R13 test was `off.name in desc.iv_terms`,
    # i.e. the offset temp had to BE the IV scaled by elem_bytes. That admits
    #     offset = IV*eb
    # but rejects the form every matmul row/column produces,
    #     offset = invariant_base + IV*eb
    # which is why matmul died with 'array-bases-not-extracted'.
    #
    # The generalised test asks `vector_affine.classify_access` for CONTIGUOUS,
    # which is a strict SUPERSET: a bare IV term classifies CONTIGUOUS with an
    # empty invariant part (const_off == 0 and sym_div == 0), so every kernel
    # that matched before still matches, and still records `None` as its base --
    # taking the identical emission path. Only accesses with a real invariant
    # part take the new path.
    import vector_affine as _va_r13
    _actx = _va_r13.LoopAffineContext(instrs, desc)
    array_slots = []
    array_offs = []                     # invariant byte-offset expr, or None
    array_info = []                     # (slot, elem_bytes, unsigned) per load
    load_temps = {}                     # temp name -> slot, for finding the mul
    for i in _region(desc):
        ins = instrs[i]
        if _cname(ins) != 'IRLoad':
            continue
        base = getattr(ins, 'base', None)
        off = getattr(ins, 'offset', None)
        if not isinstance(off, Temp) or not isinstance(base, Temp):
            continue
        if base.name not in addr_off:
            continue
        if addr_off[base.name] in (desc.primary_iv, kernel.reduction_slot):
            continue                    # the IV's own slot / the accumulator
        acc = _va_r13.classify_access(ins, _actx)
        if not acc.ok or acc.kind == _va_r13.INVARIANT:
            continue                    # not an array walk (scalar operand)
        if acc.kind != _va_r13.CONTIGUOUS or acc.coeff != kernel.elem_bytes:
            p.reason = 'unpacked-array-stride'
            return p
        bare_iv = (acc.const_off == 0 and (acc.sym_div or 0) == 0)
        array_slots.append(addr_off[base.name])
        array_offs.append(None if bare_iv else off)
        array_info.append((addr_off[base.name], ins.elem_bytes,
                           bool(getattr(ins, 'unsigned', False))))
        load_temps[ins.dest.name] = addr_off[base.name]

    # How many multiplicand arrays this reduction actually consumes, derived
    # from the reduction VALUE rather than a hard-coded kind list.
    # `reduction_value == 'dot'` means the accumulated value is load*load, so
    # there are two operand arrays; 'load'/'scaled' means one.
    #
    # This is behaviour-preserving for the pre-R13 kinds -- 'dot-product' has
    # reduction_value 'dot' (2, as before) and 'sum-reduction' has 'load' or
    # 'scaled' (1, as before) -- and it is what makes matmul safe: matmul is
    # ALSO a load*load reduction, so it needs 2. The old expression gave it 1,
    # and `array_slots[:1]` would have silently dropped the second multiplicand,
    # which is a wrong-answer bug rather than a missed optimisation.
    need = 2 if kernel.reduction_value == 'dot' else 1
    if len(array_slots) < need:
        p.reason = 'array-bases-not-extracted'
        return p
    p.array_slots = array_slots[:need]
    p.array_offs = array_offs[:need]
    p.acc_slot = kernel.reduction_slot
    p.iv_slot = desc.primary_iv
    # R6.2C / defect D2: the compact chunk loop REUSES this slot, so it must
    # access it at exactly the width the scalar code does. A width mismatch on a
    # 64-bit DMEM word reads the wrong half (and yields 0), which is what broke
    # packed GEMM wherever the compact realisation was selected.
    import vector_compact_loop as _vcl_w     # local: _vcl imports this module
    p.iv_bytes = _vcl_w.slot_width(instrs, lo, hi, p.iv_slot)
    # Same rule for the reduction accumulator: it is ALSO a slot shared with the
    # scalar code (`long long s` is 8 bytes, but `int s` is 4), so the compact
    # loop must read and write it at the width the rest of the program uses.
    p.acc_bytes = (_vcl_w.slot_width(instrs, lo, hi, p.acc_slot)
                   if p.acc_slot is not None else None)

    # ── R13.0: materialise the INVARIANT part of each based access ────────────
    # For `offset = invariant_base + IV*eb`, substituting IV = c*lanes gives
    #     off(c) = off(0) + c*lanes*eb
    # so the invariant part is computed ONCE into an address temp and each chunk
    # becomes `[addr + c*lanes*eb]` -- a compile-time constant displacement.
    # This is exactly R9.3's `_row_base` construction, reused rather than
    # reinvented, and the constant delta is licensed by the contiguity test that
    # already had to pass above.
    p.array_addr = [None] * len(p.array_slots)
    p.array_base_pre = []
    if any(o is not None for o in p.array_offs):
        from gemm_lowering import clone_offset as _clone_offset
        from ir import IRBinOp as _IRBinOp
        _dm = du.single_defs()
        _rg = set(_region(desc))
        if p.remainder:
            # A peeled/scalar tail replays the ORIGINAL loads, whose addressing
            # this plan does not describe; emitting one would address row 0.
            # Reject rather than risk a wrong answer.
            p.reason = 'based-access-with-remainder-unsupported'
            return p
        for _i, _off in enumerate(p.array_offs):
            if _off is None:
                continue
            _pre, _off0 = _clone_offset(instrs, _dm, _rg, _off, p.iv_slot,
                                        Const(0))
            if _pre is None:
                p.reason = f'row-base-not-clonable:{_off0}'
                return p
            _b, _a = _fresh('_vrb'), _fresh('_vra')
            p.array_base_pre += list(_pre) + [
                IRLoadAddr(_b, p.array_slots[_i]),
                _IRBinOp(_a, '+', _b, _off0)]
            p.array_addr[_i] = _a

    # R4.2.7 peel template: replay the ORIGINAL loads/arithmetic at a constant
    # index rather than re-deriving the tail (which would risk getting integer
    # promotion or sub-word truncation subtly wrong).
    _value = None
    if kernel.kind == 'dot-product':
        for i in _region(desc):
            m = instrs[i]
            if (_cname(m) == 'IRBinOp' and m.op == '*'
                    and isinstance(m.left, Temp) and isinstance(m.right, Temp)
                    and m.left.name in load_temps and m.right.name in load_temps):
                _value = ('*', bool(getattr(m, 'unsigned', False)))
                break
        if _value is None:
            _value = ('*', False)
    if p.acc_slot is not None:
        from vector_remainder_peel import (PeelTemplate, PeelArray, PeelScalar)
        p.peel = PeelTemplate(
            operands=[PeelArray(sl, eb_, un_) for (sl, eb_, un_) in array_info[:need]],
            op=_value,
            dest=PeelScalar(p.acc_slot, p.acc_bytes, False),
            dest_op=('+', False))

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

def _packed_load_any(dest, plan, which, chunk):
    """Packed load of chunk `chunk` from multiplicand `which`.

    Dispatches on whether the access has an invariant base. Without one the
    pre-R13 `_packed_load` runs unchanged (byte-for-byte identical output); with
    one, the address temp built by the plan is used with a constant per-chunk
    displacement."""
    addr = plan.array_addr[which] if plan.array_addr else None
    if addr is None:
        return _packed_load(dest, plan.array_slots[which], chunk,
                            plan.lanes, plan.eb, plan.signed)
    import vector_compact_loop as _vcl_i
    return _vcl_i.packed_load_at_imm(dest, addr, chunk * plan.lanes * plan.eb,
                                     plan.lanes, plan.eb, plan.signed)


def _packed_load(dest, slot, chunk, lanes, eb, signed):
    """A packed 64-bit load of `lanes` contiguous elements of chunk `chunk`."""
    base = _fresh('_vba')
    la = IRLoadAddr(base, slot)
    byte_off = chunk * lanes * eb
    ld = IRLoad(dest, base, Const(byte_off), elem_bytes=8, unsigned=(not signed))
    ld._vec_pack = (lanes, eb)
    return [la, ld]


def _acc_addr_load(slot, signed, elem_bytes=8):
    """Load the current accumulator value from its slot -> (instrs, value_temp).

    `elem_bytes` MUST be the width the scalar code uses for this slot (R6.2C /
    defect D2): the accumulator slot is SHARED with the scalar remainder and
    with the code that reads the result, and a 4-byte and an 8-byte access to
    one 64-bit DMEM word do not see the same bits."""
    base = _fresh('_vaa')
    val = _fresh('_vac')
    return [IRLoadAddr(base, slot),
            IRLoad(val, base, Const(0), elem_bytes=elem_bytes,
                   unsigned=(not signed))], val


def _acc_store(slot, value_temp, elem_bytes=8):
    base = _fresh('_vas')
    return [IRLoadAddr(base, slot),
            IRStore(base, Const(0), value_temp, elem_bytes)]


def is_dot_shaped(plan):
    """Two multiplicand arrays feed the accumulator, so this lowers to $dot.

    R13.1 promotes this to the SHARED structural predicate for "dot-shaped"
    reductions. It is deliberately about the reduction STRUCTURE -- an
    accumulator fed by a product of two load-derived operands -- and never
    about a kind string, a kernel name, a datatype or a matrix size. Both
    'dot-product' and 'matmul' satisfy it; 'sum-reduction' (one operand) does
    not, which is exactly the distinction R8.1a requires be preserved.

    R13.0: emission used to branch on `plan.kind == 'dot-product'`, which sent
    'matmul' -- a genuine two-operand dot -- down the ONE-operand $vreduce path
    and produced wrong results (caught by the differential oracle, not shipped).
    Branching on the operand count instead is behaviour-identical for the
    pre-R13 kinds (dot-product has 2, sum-reduction has 1) and correct for any
    future load*load reduction."""
    return len(getattr(plan, 'array_slots', ()) or ()) == 2


def build_vector_body(plan):
    """The straight-line packed vector instructions that process the first
    `chunks*lanes` elements and store the partial accumulator. The kept scalar
    loop (with IV re-initialised to chunks*lanes) handles the remainder.

    R8.1: the fully-unrolled realisation chains ALL `chunks` accumulates through
    one register, which makes the dependence chain exactly `chunks` long. That
    chain -- not the machine -- sets the bundle count: measured on `dot vi8`, 28
    bundles for 72 instructions against a width bound of 9. Accumulator expansion
    (R6.6, previously only wired into the COMPACT realisation and only for
    sum-reduction) is reused here for both kinds."""
    import reduction_accumulator_expansion as _rae
    from ir import IRBinOp

    # One accumulator per chunk is wasteful in registers; `best_accumulator_count`
    # picks the K that minimises ceil(chunks/K) + log2(K), preferring the smaller.
    # R8.1a: restricted to dot products. Applying it to the fully-unrolled
    # SUM-REDUCTION as well perturbed the R4.2.5 size probe enough to change the
    # adaptive search's outcome for `reduction vi16`, which lost the COMPACT
    # expansion R6.6 gave it (626 -> 605 -> 626 ticks). Dot is where the chain
    # gap is extreme (28 bundles against a width bound of 9) and where the
    # measured gain is largest, so the transform is applied there only.
    # R13.1: the expansion is offered to every DOT-SHAPED reduction, not to the
    # 'dot-product' kind alone. R8.1a's restriction is preserved exactly -- it
    # excluded the one-operand SUM-REDUCTION from the fully-unrolled expansion
    # because it perturbed the R4.2.5 size probe, and `is_dot_shaped` still
    # excludes it. matmul is a genuine two-operand dot and now qualifies.
    k = (_rae.best_accumulator_count(plan.chunks)
         if is_dot_shaped(plan) else 1)
    exp, plan.acc_expand_reason = _rae.plan_expansion(
        plan, k, load_fn=_acc_addr_load, store_fn=_acc_store)
    plan.acc_expand = exp is not None

    body = []
    body += list(plan.array_base_pre or [])      # R13.0 row bases, computed once
    if exp is None:
        init, acc = _acc_addr_load(plan.acc_slot, plan.signed,
                                   elem_bytes=plan.acc_bytes)
        body += init
        accs = None
    else:
        body += exp.pre
        accs = exp.accs

    for c in range(plan.chunks):
        acc_in = acc if accs is None else accs[c % len(accs)]
        if is_dot_shaped(plan):
            aT, bT = _fresh('_vpa'), _fresh('_vpb')
            body += _packed_load_any(aT, plan, 0, c)
            body += _packed_load_any(bT, plan, 1, c)
            nxt = _fresh('_vacc') if accs is None else acc_in
            body.append(IRVecDot(nxt, aT, bT, '$' + plan.vtype,
                                 accumulate=True, accum=acc_in))
            if accs is None:
                acc = nxt
        else:  # sum-reduction
            aT = _fresh('_vpa')
            body += _packed_load_any(aT, plan, 0, c)
            partial = _fresh('_vred')
            body.append(IRVecReduce(partial, aT, '$' + plan.vtype, '+'))
            if accs is None:
                nxt = _fresh('_vacc')
                body.append(IRBinOp(nxt, '+', acc, partial))
                acc = nxt
            else:
                body.append(IRBinOp(acc_in, '+', acc_in, partial))

    if exp is None:
        body += _acc_store(plan.acc_slot, acc, elem_bytes=plan.acc_bytes)
    else:
        body += exp.post
    return body


def build_compact_body(plan):
    # R13.0: the compact realisation addresses each chunk with a REGISTER offset
    # derived from the loop's own counter, which carries no invariant row base.
    # Emitting it for a based access would address row 0 on every row -- the
    # exact shape of the R12.1 wrong-answer bug. Decline instead; `lower_kernel`
    # then simply has one fewer candidate and the unrolled realisation is used.
    # Compact support for based accesses is NOT part of R13.0.
    if any(a is not None for a in (plan.array_addr or [])):
        return None

    """R4.2.5: the body of ONE chunk of a compact vector loop, addressed by a
    register offset instead of a constant. The accumulator lives in its memory
    slot across the back edge (loaded at the top, stored at the bottom) exactly as
    the scalar loop does -- so no loop-carried REGISTER is introduced and the
    R2.8-class live-range hazard cannot arise."""
    import vector_compact_loop as _vcl
    import reduction_accumulator_expansion as _rae

    # R6.6: when unrolling gives this loop U independent copies, a sum-reduction
    # can give each copy its OWN accumulator instead of chaining them all through
    # one. `plan_expansion` returns None (with a reason) whenever that does not
    # apply, and then everything below is byte-for-byte the pre-R6.6 lowering.
    _u = _vcl.unroll_factor(plan.chunks)
    exp, plan.acc_expand_reason = _rae.plan_expansion(plan, _u)
    plan.acc_expand = exp is not None
    if exp is not None:
        # `build_compact_chunk_loop` invokes emit_body exactly once per unrolled
        # copy, in order k = 0 .. U-1, so the copy index is tracked here rather
        # than widening the emit_body contract for the three clients that do not
        # need it.
        _copy = [0]

        def emit_expanded(off, iv_index=None):
            k = _copy[0]
            _copy[0] += 1
            body = []
            aT = _fresh('_vpa')
            body += _vcl.packed_load_at(aT, plan.array_slots[0], off,
                                        plan.lanes, plan.eb, plan.signed)
            partial = _fresh('_vred')
            body.append(IRVecReduce(partial, aT, '$' + plan.vtype, '+'))
            body.append(exp.accumulate(k, partial))
            return body

        loop, per_iter = _vcl.build_compact_chunk_loop(
            plan.iv_slot, plan.eb, plan.lanes, plan.chunks, emit_expanded,
            iv_bytes=plan.iv_bytes)
        plan.compact_per_iter = per_iter
        return exp.pre + loop + exp.post

    def emit(off, iv_index=None):
        body = []
        init, acc = _vcl.slot_load(plan.acc_slot, plan.signed,
                                   elem_bytes=plan.acc_bytes)
        body += init
        if is_dot_shaped(plan):
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
        body += _vcl.slot_store(plan.acc_slot, nxt,
                                elem_bytes=plan.acc_bytes)
        return body

    loop, per_iter = _vcl.build_compact_chunk_loop(plan.iv_slot, plan.eb,
                                                   plan.lanes, plan.chunks, emit,
                                                   iv_bytes=plan.iv_bytes)
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
    from vector_remainder_peel import splice_peeled
    compact_body = build_compact_body(plan)
    unrolled_body = build_vector_body(plan)
    plan.unrolled_len = len(unrolled_body)
    compact = (_splice_compact(instrs, plan, compact_body)
               if compact_body is not None else None)
    unrolled = _splice_unrolled(instrs, plan, unrolled_body)
    # R4.2.7: with a remainder, the scalar tail loop can be PEELED away instead of
    # kept. Both realisations get a peeled variant; the selector decides. The
    # unrolled body starts at chunks*lanes (it does not count), the compact loop
    # counts up from 0 itself.
    cands = [('unrolled', unrolled)]
    if compact is not None:
        cands.append(('compact', compact))
    if plan.remainder > 0 and compact_body is not None:
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

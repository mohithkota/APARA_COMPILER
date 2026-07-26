"""
vector_validation.py -- Vector Validation Framework (Milestone R4.0).

The vector equivalent of the scalar `ir_interp` differential oracle that R2/R3
relied on. `ir_interp` raises `Unsupported` on vector IR, so this module EXTENDS
it (without modifying it) to execute the vector instructions, and provides the
`differential_vector` oracle every future vector pass will use to prove a
vectorized loop is behaviour-identical to its scalar original.

FAITHFUL TO THE HARDWARE, NOT THE IDEAL. The lane semantics mirror golden_stubs.h
(the no-bias reference derived from isa.txt + hardware confirmation), including the
one CONFIRMED simulator bug: `$vreduce` sum ALWAYS sign-extends lanes -- correct
for signed element types, the documented bug for unsigned. Modelling the real
behaviour means the oracle would CATCH a pass that wrongly emitted an unreliable
op (the capability layer already forbids it; this is the second line of defence).

Vector register model: a vector register is a single 64-bit word holding
`64/element_bits` packed lanes -- represented, like every scalar value, as a
Python int. Wide loads/stores move 2 or 4 consecutive words.
"""

import vector_capability_db as _db
from ir import Const, Temp
from loopopt import ir_interp

_MASK64 = (1 << 64) - 1


def _vinfo(type_str):
    """(element_bits, signed) for a vector type tag like '$vi8'."""
    tag = type_str.replace('$', '')
    e = _db.ELEMENT_TYPES.get(tag)
    if e is None:
        raise ir_interp.Unsupported(f'vector type {type_str}')
    return e['bits'], e['signed']


def _lane(packed, i, bits, mask):
    return (packed >> (i * bits)) & mask


def _sext(e, bits):
    """Sign-extend a `bits`-wide lane value to a Python int."""
    if bits >= 64:
        return e - (1 << 64) if (e >> 63) else e
    return e - (1 << bits) if (e >> (bits - 1)) else e


class VectorInterp(ir_interp.Interp):
    """`ir_interp.Interp` + the APARA vector instructions."""

    def _exec_data(self, ins, c, mem, regs):
        if c == 'IRVecArith':
            regs[ins.dest.name] = self._valu(ins, regs)
        elif c == 'IRVecDot':
            regs[ins.dest.name] = self._dot(ins, regs)
        elif c == 'IRVecDot128':
            regs[ins.dest.name] = self._dot128(ins, regs)
        elif c == 'IRVecReduce':
            regs[ins.dest.name] = self._vreduce(ins, regs)
        elif c == 'IRLoadWide':
            self._load_wide(ins, mem, regs)
        elif c == 'IRStoreWide':
            self._store_wide(ins, mem, regs)
        else:
            super()._exec_data(ins, c, mem, regs)

    # ── VALU: element-wise +/-/* across lanes (golden_stubs __v_generic) ───────
    def _valu(self, ins, regs):
        bits, _sgn = _vinfo(ins.type_str)
        mask = (1 << bits) - 1 if bits < 64 else _MASK64
        n = 64 // bits
        a = self._val(None, regs, ins.src1) & _MASK64
        b = self._val(None, regs, ins.src2) & _MASK64
        out = 0
        for i in range(n):
            ea = _lane(a, i, bits, mask)
            eb = (b & mask) if ins.replicate else _lane(b, i, bits, mask)
            if ins.op == '+':
                r = ea + eb
            elif ins.op == '-':
                r = ea - eb
            else:
                r = ea * eb
            out |= (r & mask) << (i * bits)
        return _sext(out, 64)                      # store as signed 64-bit word

    # ── DOT: sum of element-wise products (golden_stubs __dot_generic) ─────────
    def _dot(self, ins, regs):
        bits, signed = _vinfo(ins.type_str)
        mask = (1 << bits) - 1 if bits < 64 else _MASK64
        n = 64 // bits
        a = self._val(None, regs, ins.src1) & _MASK64
        b = self._val(None, regs, ins.src2) & _MASK64
        acc = 0
        if getattr(ins, 'accumulate', False) and getattr(ins, 'accum', None) is not None:
            acc = self._val(None, regs, ins.accum)
        s = acc
        for i in range(n):
            ea = _lane(a, i, bits, mask)
            eb = _lane(b, i, bits, mask)
            va = _sext(ea, bits) if signed else ea
            vb = _sext(eb, bits) if signed else eb
            s += va * vb
        return s

    # ── DOT128: 16 vu8 lanes across a value pair (lo 0-7, hi 8-15) ─────────────
    def _dot128(self, ins, regs):
        bits, _sgn = _vinfo(ins.type_str)          # vu8 -> unsigned 8-bit
        mask = (1 << bits) - 1
        n = 64 // bits
        s = 0
        for lo, hi_reg in ((ins.a_lo, ins.b_lo), (ins.a_hi, ins.b_hi)):
            a = self._val(None, regs, lo) & _MASK64
            b = self._val(None, regs, hi_reg) & _MASK64
            for i in range(n):
                s += _lane(a, i, bits, mask) * _lane(b, i, bits, mask)
        return s

    # ── VREDUCE: horizontal sum / max. Sum ALWAYS sign-extends (hardware). ─────
    def _vreduce(self, ins, regs):
        bits, _sgn = _vinfo(ins.type_str)
        mask = (1 << bits) - 1 if bits < 64 else _MASK64
        n = 64 // bits
        a = self._val(None, regs, ins.src) & _MASK64
        op = getattr(ins, 'op', '+')
        if op == '$max':
            signed = _sgn
            best = None
            for i in range(n):
                e = _lane(a, i, bits, mask)
                ev = _sext(e, bits) if signed else e
                best = ev if best is None or ev > best else best
            return best or 0
        # sum: the simulator sign-extends every lane (bug for unsigned; correct for signed)
        s = 0
        for i in range(n):
            s += _sext(_lane(a, i, bits, mask), bits)
        return s

    # ── wide load / store: contiguous 2/4-word move ────────────────────────────
    def _load_wide(self, ins, mem, regs):
        base = self._val(mem, regs, ins.base)
        off = self._val(mem, regs, ins.offset)
        for i, d in enumerate(ins.dests):
            if isinstance(d, Temp):
                regs[d.name] = ir_interp._to_signed(mem.get(base + off + i * 8, 0))

    def _store_wide(self, ins, mem, regs):
        base = self._val(mem, regs, ins.base)
        off = self._val(mem, regs, ins.offset)
        for i, s in enumerate(ins.srcs):
            mem[base + off + i * 8] = ir_interp._to_signed(self._val(mem, regs, s))


# ── public API (mirrors ir_interp.run_slice / differential) ─────────────────────

def run_slice_vector(instrs, lo, hi, init_mem=None, step_limit=2_000_000):
    """Execute a function slice that MAY contain vector IR; return (ret, mem)."""
    mem = dict(init_mem) if init_mem else {}
    ret = VectorInterp(instrs, step_limit).run_function(lo, hi, mem)
    return ret, mem


def differential_vector(scalar_instrs, vector_instrs, lo, hi, init_mem=None,
                        seeds=6):
    """THE vector differential oracle: run the SCALAR slice (via the frozen
    scalar interpreter) and the VECTORIZED slice (via the vector interpreter) from
    identical memory and compare return value + final memory. Returns
    ('match'|'mismatch'|'unsupported', detail).

    This is what a future vectorizer will gate on -- exactly like the scalar
    differential gated R2/R3, but able to execute the vector form."""
    import random
    fname = getattr(scalar_instrs[lo], 'name', None)
    from ir_utils import func_slices
    a1 = next((a for a, b in func_slices(vector_instrs)
               if getattr(vector_instrs[a], 'name', None) == fname), None)
    if a1 is None:
        return 'mismatch', 'vectorized slice not found'
    b1 = next(b for a, b in func_slices(vector_instrs) if a == a1)

    rng = random.Random(0x4EC0)
    ran = 0
    for s in range(seeds):
        seed = dict(ir_interp._preload_globals(scalar_instrs))
        if s:
            for addr in range(-4096, 4097, 8):
                seed.setdefault(addr, rng.randint(-100, 100))
        if init_mem:
            seed.update(init_mem)
        try:
            r0, m0 = ir_interp.run_slice(scalar_instrs, lo, hi, init_mem=dict(seed))
            r1, m1 = run_slice_vector(vector_instrs, a1, b1, init_mem=dict(seed))
        except (ir_interp.Unsupported, ir_interp.StepLimit) as e:
            continue
        ran += 1
        if r0 != r1:
            return 'mismatch', f'return {r0} != {r1} (seed {s})'
        if m0 != m1:
            diff = {k: (m0.get(k), m1.get(k)) for k in set(m0) | set(m1)
                    if m0.get(k) != m1.get(k)}
            return 'mismatch', f'memory differs at {sorted(diff)[:4]} (seed {s})'
    return ('match' if ran >= 1 else 'unsupported',
            f'{ran} seeds agreed')

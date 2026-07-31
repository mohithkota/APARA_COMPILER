"""
vector_affine.py -- Affine Access Recognition for Vectorization (R4.2.8).

ANALYSIS ONLY. Resolves a memory access's offset expression, relative to ONE
innermost loop, into the normal form

        offset  ==  coeff * IV  +  invariant

with `coeff` a compile-time constant. That single answer is what every vector
transformation on the roadmap actually needs:

    coeff == elem_bytes   the access is CONTIGUOUS  -> packed load/store
    coeff == 0            the access is INVARIANT   -> a scalar operand
                                                       (AXPY's replicate value,
                                                        GEMM row-dot's accumulator)
    coeff == anything else  STRIDED  -> rejected, with the stride reported
    unresolvable            UNKNOWN  -> rejected, with a reason

WHY THIS AND NOT `invariant_base + IV*constant`
-----------------------------------------------
That narrower form was the original R4.2.8 proposal. Measuring the IR the front
end actually emits shows it is NOT sufficient:

1. **For elem_bytes >= 2 the scale is applied AFTER the index sum.** A 2-D-indexed
   `C[i*8+j]` on `vi16_t` emits `(i*8 + j) * 2`, i.e. `(invariant + IV) * const`,
   not `invariant + IV*const`. A pattern matcher for the latter matches NONE of
   the vi16/vi32 kernels -- it would appear to work on vi8 (where the x1 scale is
   elided) and silently fail to generalize.
2. **Operand order varies.** A 1-D convolution emits `(invariant + IV*1)` when the
   tap loop is innermost and `(IV*1 + invariant)` when the output loop is.
3. **Expressions nest.** A 2-D convolution's `in[(i+r)*8 + (j+s)]` hides the IV two
   levels down inside `(j+s)`; one level of matching is not enough.
4. **Invariance is a VALUE property, not a syntactic one.** The "invariant"
   subexpressions (`i*8`) are RECOMPUTED INSIDE the innermost body -- LICM has not
   run yet -- so they look local. Deciding invariance by position rejects every
   2-D kernel. It must be decided by asking whether the slot is written in the
   loop, which is the M2 memory-effects question. (Same class of bug as R1.4's
   "the bound must be VALUE-invariant, not merely temp-independent".)
5. **`coeff == 0` must be a first-class answer**, because identifying the
   loop-invariant operand is exactly what recognises an AXPY or a row-dot.

WHAT IS DELIBERATELY *NOT* SUPPORTED (this is not a general dependence analyser)
-------------------------------------------------------------------------------
* **Multiple varying induction variables.** Not needed, by construction:
  vectorization always targets the INNERMOST loop, so every enclosing loop's IV is
  invariant with respect to it. `IV1 + IV2` cannot arise with both varying.
* **Symbolic coefficients.** `B[k*N+j]` with the k-loop innermost and a runtime
  `N` resolves to `coeff = N` (not constant) and is REJECTED -- correctly, since
  that is precisely the column-strided access APARA cannot gather.
* Division, modulo, min/max, data-dependent (gather) indices, pointer arithmetic
  through unknown bases: all rejected with a reason.
* No SCEV, no chains of recurrences, no polyhedral machinery.

The narrow `invariant + IV*const` form is not lost -- it is one of the shapes this
normalizer folds into the same answer.
"""

import os
import sys
from math import gcd as _gcd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import Const, Temp
from ir_utils import src_names
from analysis import DefUse

_MAX_DEPTH = 16                     # bounds the recursion on pathological trees

CONTIGUOUS = 'contiguous'
INVARIANT = 'invariant'
STRIDED = 'strided'
UNKNOWN = 'unknown'


def _cname(x):
    return type(x).__name__


class AffineAccess:
    """`offset == coeff*IV + const + symbolic`, or a rejection reason.

    R6.2C additions (`const_off`, `sym_div`) decompose the INVARIANT part that
    R4.2.8 deliberately left opaque. They are needed for exactly one question,
    which cannot be asked without them: is the address this access lowers to
    8-byte ALIGNED? The lowering substitutes the IV with a multiple of the
    packed word, so alignment depends entirely on the invariant:

        const_off   the compile-time constant part, in BYTES
        sym_div     a positive integer that provably DIVIDES every symbolic
                    (non-constant, non-IV) term; 0 means there is no symbolic
                    part at all, and 1 means "present but nothing proven"

    `coeff` and `kind` are computed exactly as before -- this is additive."""
    __slots__ = ('ok', 'coeff', 'reason', 'kind', 'elem_bytes',
                 'const_off', 'sym_div')

    def __init__(self, ok, coeff=None, reason=None, kind=UNKNOWN, elem_bytes=None,
                 const_off=None, sym_div=None):
        self.ok = ok
        self.coeff = coeff
        self.reason = reason
        self.kind = kind
        self.elem_bytes = elem_bytes
        self.const_off = const_off
        self.sym_div = sym_div

    def __repr__(self):
        if not self.ok:
            return f"Affine(UNKNOWN: {self.reason})"
        if self.kind == INVARIANT:
            return "Affine(INVARIANT)"
        return f"Affine({self.kind} coeff={self.coeff} eb={self.elem_bytes})"


class LoopAffineContext:
    """Everything the resolver needs about ONE innermost loop. Built once per
    loop and reused for every access, so the cost is linear in the body."""

    __slots__ = ('instrs', 'def_map', 'addr_slot', 'iv_slot', 'region',
                 'stored_slots')

    def __init__(self, instrs, desc):
        lo, hi = desc.func_slice
        self.instrs = instrs
        self.def_map = DefUse(instrs, lo, hi).single_defs()
        self.addr_slot = {n: instrs[i].fp_offset
                          for n, i in self.def_map.items()
                          if _cname(instrs[i]) == 'IRLoadAddr'}
        self.iv_slot = desc.primary_iv
        region = set()
        for b in desc.body_blocks:
            blk = desc.cfg.blocks[b]
            region.update(range(blk.lo, blk.hi + 1))
        self.region = region
        # The M2 question: which stack slots does this loop WRITE? A slot that is
        # never written in the loop yields the same value every iteration, however
        # deep inside the body it happens to be re-loaded.
        self.stored_slots = set()
        for i in sorted(region):
            ins = instrs[i]
            if _cname(ins) == 'IRStore' and isinstance(getattr(ins, 'base', None), Temp):
                slot = self.addr_slot.get(ins.base.name)
                if slot is not None:
                    self.stored_slots.add(slot)

    # ── invariance, decided by VALUE not by position ──────────────────────────
    def _slot_of_load(self, ins):
        base = getattr(ins, 'base', None)
        if not isinstance(base, Temp):
            return None
        return self.addr_slot.get(base.name)

    def is_the_iv(self, name):
        """True if `name` is a load of the induction variable's own slot."""
        d = self.def_map.get(name)
        if d is None or _cname(self.instrs[d]) != 'IRLoad':
            return False
        ins = self.instrs[d]
        off = getattr(ins, 'offset', None)
        return (self._slot_of_load(ins) == self.iv_slot
                and isinstance(off, Const) and off.value == 0)

    def varies(self, name, seen=None):
        """True if `name`'s VALUE can differ between iterations of this loop."""
        if seen is None:
            seen = set()
        if name in seen:
            return False
        seen.add(name)
        d = self.def_map.get(name)
        if d is None:
            return False                     # no single def in this function
        if d not in self.region:
            return False                     # computed outside the loop
        ins = self.instrs[d]
        if _cname(ins) == 'IRLoad':
            slot = self._slot_of_load(ins)
            if slot is None:
                return True                  # unknown address: conservative
            if slot == self.iv_slot:
                return True                  # reads the induction variable
            if slot in self.stored_slots:
                return True                  # M2: this loop writes the slot
            # A load from an unwritten slot is still VARYING if the address it
            # reads moves with the IV -- `a[idx[k]]` reads a different element
            # every iteration even though `idx` itself is never written here.
            # Missing this would classify a GATHER as loop-invariant, which is
            # exactly the access APARA cannot perform.
            off = getattr(ins, 'offset', None)
            if isinstance(off, Temp):
                return self.varies(off.name, seen)
            return False
        if _cname(ins) in ('IRCall', 'IRIndirectCall'):
            return True
        return any(self.varies(s, seen) for s in (src_names(ins) or []))


def _const_value(expr, ctx):
    """The compile-time value of `expr`, or None."""
    if isinstance(expr, Const):
        return expr.value
    return None


def _merge_div(a, b):
    """Divisor of a SUM of two symbolic parts. 0 means "no symbolic part", so it
    is the identity; otherwise the sum is divisible only by their gcd."""
    if a == 0:
        return b
    if b == 0:
        return a
    return _gcd(a, b)


def _resolve(expr, ctx, depth=0):
    """(coeff, const, div, ok) for `expr` == coeff*IV + const + symbolic.

    `coeff` is exactly what R4.2.8 computed. `const` and `div` additionally
    decompose the invariant remainder (see AffineAccess) so the alignment of the
    lowered address can be decided; they never influence `coeff` or `ok`."""
    if depth > _MAX_DEPTH:
        return None, None, None, False
    if isinstance(expr, Const):
        return 0, expr.value, 0, True
    if not isinstance(expr, Temp):
        return None, None, None, False
    if ctx.is_the_iv(expr.name):
        return 1, 0, 0, True
    d = ctx.def_map.get(expr.name)
    if d is None or d not in ctx.region:
        return (0, 0, 1, True) if not ctx.varies(expr.name) else (None, None, None, False)
    ins = ctx.instrs[d]
    c = _cname(ins)
    if c == 'IRAssign':
        return _resolve(ins.src, ctx, depth + 1)
    if c in ('IRLoadAddr', 'IRGlobalAddrOf'):
        return 0, 0, 1, True
    if c == 'IRLoad':
        # not the IV (checked above): invariant iff its slot is never written here
        return (0, 0, 1, True) if not ctx.varies(expr.name) else (None, None, None, False)
    if c == 'IRBinOp':
        lc, lk, ld, lok = _resolve(ins.left, ctx, depth + 1)
        rc, rk, rd, rok = _resolve(ins.right, ctx, depth + 1)
        if not (lok and rok):
            return None, None, None, False
        if ins.op == '+':
            return lc + rc, lk + rk, _merge_div(ld, rd), True
        if ins.op == '-':
            return lc - rc, lk - rk, _merge_div(ld, rd), True
        if ins.op == '*':
            # affine x affine is affine only when the IV-bearing side is scaled by
            # a COMPILE-TIME constant. A symbolic scale (a runtime row stride) is
            # exactly the column-strided case we must reject. Scaling multiplies
            # the constant part and the symbolic divisor by the same literal.
            k = _const_value(ins.left, ctx)
            if k is not None:
                return k * rc, k * rk, (k * rd if rd else 0), True
            k = _const_value(ins.right, ctx)
            if k is not None:
                return k * lc, k * lk, (k * ld if ld else 0), True
            if lc == 0 and rc == 0:
                return 0, 0, 1, True         # invariant x invariant: no divisor
            return None, None, None, False
        return None, None, None, False       # '/', '%', shifts, ...: rejected
    return (0, 0, 1, True) if not ctx.varies(expr.name) else (None, None, None, False)


def resolve_offset(offset, ctx):
    """Resolve one access offset. Returns AffineAccess (without an elem_bytes
    judgement -- use `classify_access` for that)."""
    coeff, const, div, ok = _resolve(offset, ctx)
    if not ok:
        return AffineAccess(False, reason='not-affine-in-the-loop-iv')
    kind = INVARIANT if coeff == 0 else STRIDED
    return AffineAccess(True, coeff=coeff, kind=kind,
                        const_off=const, sym_div=div)


def word_aligned(acc, word=8):
    """Is the lowered address of `acc` provably a multiple of `word` bytes?

    The packed lowering substitutes the induction variable with a multiple of
    the packed word, so the IV term is aligned by construction and alignment
    reduces to the INVARIANT part:

        aligned  <=>  const_off % word == 0  AND  (no symbolic part, or its
                      proven divisor is itself a multiple of word)

    Returns False when alignment cannot be PROVEN -- an unproven address is
    exactly the case that must not be lowered to a wide access."""
    if not acc.ok or acc.const_off is None:
        return False
    if acc.const_off % word:
        return False
    return acc.sym_div == 0 or acc.sym_div % word == 0


def classify_access(ins, ctx):
    """Classify a load/store for vectorization.

        CONTIGUOUS  coeff == elem_bytes  -> a packed lane-parallel access
        INVARIANT   coeff == 0           -> a scalar operand
        STRIDED     any other constant   -> APARA cannot gather it
        UNKNOWN     unresolvable
    """
    off = getattr(ins, 'offset', None)
    eb = getattr(ins, 'elem_bytes', None)
    res = resolve_offset(off, ctx)
    if not res.ok:
        return res
    res.elem_bytes = eb
    if res.coeff == 0:
        res.kind = INVARIANT
    elif eb is not None and res.coeff == eb:
        res.kind = CONTIGUOUS
    else:
        res.kind = STRIDED
        res.reason = f'stride-{res.coeff}-not-elem-{eb}'
    return res


def classify_loop(desc, instrs):
    """Classify every load/store in one innermost loop body.
    Returns [(index, instruction, AffineAccess)] in program order."""
    ctx = LoopAffineContext(instrs, desc)
    out = []
    for i in sorted(ctx.region):
        ins = instrs[i]
        if _cname(ins) in ('IRLoad', 'IRStore'):
            out.append((i, ins, classify_access(ins, ctx)))
    return out


def summarize_loop(desc, instrs):
    """{kind: count} over the loop's accesses -- for reporting."""
    counts = {}
    for _i, _ins, a in classify_loop(desc, instrs):
        k = a.kind if a.ok else UNKNOWN
        counts[k] = counts.get(k, 0) + 1
    return counts

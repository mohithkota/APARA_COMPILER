"""
depgraph_disambig.py -- Memory Dependence Disambiguation (Milestone R2.2).

ANALYSIS ONLY. Improves the *precision* of the R2.1 DependenceGraph's MEMORY
edges and nothing else. It never mutates IR, never changes scheduling, never
changes generated assembly, never changes any compiler behaviour. The graph, its
API, `LoopTransform`, the bundler and `LoopUnroll` are all frozen; this module
plugs into the R2.1 graph purely through the optional `disambiguator=` hook the
graph already exposes, and is consumed by nothing in the production pipeline.

It answers ONE question for a memory pair the R2.1 base oracle called "may
alias": can this specific intra-iteration or loop-carried conflict be PROVED
absent? If yes the edge is dropped; if the pair provably MUST alias the kept edge
is tagged `proven`; otherwise it stays a conservative edge. **A dependence is
removed only when provably safe** -- every other case remains conservative.

================================================================================
REUSED ANALYSES (nothing re-derived)
================================================================================
    loopopt.discover                     -- M0 loop descriptors (per function)
    loopopt.annotate_induction_vars      -- M1 IVs: `basic_ivs`, `iv_terms`
                                            (temp -> (iv_slot, scale)) -- the affine
                                            index recognition and stride comparison
    loopopt.annotate_memory_effects      -- M2: `aliasing_summary.clean_slots`
                                            (escape analysis), `written_keys`,
                                            `invariant_insts`
    analysis.DefUse                      -- single-definition map (base-pointer and
                                            offset origin resolution)
    ir_utils.func_slices                 -- per-function scoping
    ir (`Const`, `Temp`)                 -- operand inspection

Disambiguation rules, each provably safe under the compiler's existing memory
model (the same one M2 and the bundler already assume):

  1. CLEAN-SLOT.  A clean local stack slot (its address never escaped -- M2
     `clean_slots`) cannot be aliased by any computed pointer, global access, or
     call. => disjoint.  (This is exactly M2's documented model.)
  2. DISTINCT LOCAL OBJECTS.  Two computed accesses based at the addresses of two
     DIFFERENT local slots (`&a` vs `&b`) address non-overlapping frame regions.
     => disjoint. (Same non-overlapping-slot assumption M2 makes.)
  3. SAME-BASE SIV.  Two accesses through the SAME base value (same stack-object
     address, same global object, or the same loop-invariant pointer slot) with
     offsets that are affine in the SAME induction variable with EQUAL stride:
        intra (same iteration i):  disjoint iff the constant parts differ.
        carried (iterations i1 != i2, stride s != 0):  a cross-iteration solution
          exists iff (c_a - c_b) is a NON-ZERO multiple of s; otherwise disjoint.
          In particular a[i] vs a[i] has NO loop-carried conflict (only intra).
     Constant-offset accesses to the same base are the s == 0 special case
     (iteration-independent): disjoint iff the constants differ, else must-alias.
  Anything not covered stays CONSERVATIVE.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import Const, Temp                                            # noqa: E402
from ir_utils import func_slices                                      # noqa: E402
from analysis import DefUse                                           # noqa: E402
from .discovery import discover, discover_function                    # noqa: E402
from .analysis_iv import annotate_induction_vars                      # noqa: E402
from .analysis_mem import annotate_memory_effects                     # noqa: E402
from .depgraph import DependenceGraph                                 # noqa: E402


def _cname(x):
    return type(x).__name__


def _is_zero(off):
    return isinstance(off, Const) and off.value == 0


# ── verdict ───────────────────────────────────────────────────────────────────

class Verdict:
    __slots__ = ('disjoint', 'proven', 'reason')

    def __init__(self, disjoint, proven, reason):
        self.disjoint = disjoint    # True => provably no conflict; drop the edge
        self.proven = proven        # True => provably a real conflict (MUST-alias)
        self.reason = reason        # tag for measurement / debugging

    def __repr__(self):
        state = 'DISJOINT' if self.disjoint else ('PROVEN' if self.proven
                                                  else 'MAYBE')
        return f"Verdict({state}:{self.reason})"


def _disjoint(reason):
    return Verdict(True, False, reason)


def _alias(reason):
    return Verdict(False, True, reason)


def _maybe(reason):
    return Verdict(False, False, reason)


# ── one memory access, resolved to (base identity, affine offset) ─────────────

class _Access:
    __slots__ = ('index', 'category', 'base_id', 'iv_slot', 'coef', 'const',
                 'is_write')

    def __init__(self, index, category, base_id, iv_slot, coef, const, is_write):
        self.index = index
        self.category = category    # 'stack'|'computed'|'global'|'barrier'
        self.base_id = base_id      # hashable base-value identity, or None
        self.iv_slot = iv_slot      # induction-variable slot of the offset, or None
        self.coef = coef            # affine stride (int); 0 = constant; None = unknown
        self.const = const          # affine constant part (int); None = unknown
        self.is_write = is_write


# ── the disambiguator ─────────────────────────────────────────────────────────

class MemoryDisambiguator:
    """Per-function memory disambiguation, plugged into DependenceGraph via
    `disambiguator=`. `classify(i, j, carried)` returns a Verdict for the memory
    pair (i, j) in the given iteration relation (carried=False -> same iteration,
    carried=True -> cross iteration)."""

    def __init__(self, instrs, lo, hi, descs, du=None):
        self.instrs = instrs
        self.lo = lo
        self.hi = hi
        self.du = du if du is not None else DefUse(instrs, lo, hi)
        self._single = self.du.single_defs()

        # slot-address temps (single-def IRLoadAddr) -- the M2/M1 convention
        self.addr_off = {name: instrs[k].fp_offset
                         for name, k in self._single.items()
                         if _cname(instrs[k]) == 'IRLoadAddr'}

        # function-wide clean slots + written keys come from M2 (identical across
        # every descriptor of the function); empty when the function has no loop.
        self.clean_slots = set()
        self.written_keys = set()
        for d in descs:
            if d.aliasing_summary is not None:
                self.clean_slots = set(d.aliasing_summary.clean_slots)
                self.written_keys = set(d.aliasing_summary.written_keys)
                break

        # index -> innermost descriptor (for that index's IV facts / invariance)
        self._desc_of = {}
        for d in sorted(descs, key=lambda x: len(x.body_blocks), reverse=True):
            for b in d.body_blocks:
                blk = d.cfg.blocks[b]
                for k in range(blk.lo, blk.hi + 1):
                    self._desc_of[k] = d           # smaller body overwrites larger

        self._cache = {}

    # ── access resolution ─────────────────────────────────────────────────────

    def access(self, k):
        a = self._cache.get(k)
        if a is None:
            a = self._resolve(k)
            self._cache[k] = a
        return a

    def _resolve(self, k):
        ins = self.instrs[k]
        c = _cname(ins)
        if c in ('IRCall', 'IRIndirectCall'):
            return _Access(k, 'barrier', None, None, None, None, True)
        if c in ('IRLoad', 'IRStore'):
            is_w = (c == 'IRStore')
            base, off = ins.base, ins.offset
            if isinstance(base, Temp) and _is_zero(off) and base.name in self.addr_off:
                slot = self.addr_off[base.name]
                # base identity ('stackaddr', slot) unifies a zero-offset slot
                # access with the same object's computed (`&slot + c`) accesses, so
                # a[0] vs a[1] on one local array can be offset-compared.
                return _Access(k, 'stack', ('stackaddr', slot), None, 0, 0, is_w)
            base_id = self._base_identity(base, k)
            iv, coef, const = self._offset_affine(off, k)
            return _Access(k, 'computed', base_id, iv, coef, const, is_w)
        if c in ('IRGlobalLoad', 'IRGlobalStore'):
            is_w = (c == 'IRGlobalStore')
            iv, coef, const = self._offset_affine(ins.offset, k)
            return _Access(k, 'global', ('global', ins.dmem_addr), iv, coef, const, is_w)
        if c in ('IRLoadWide', 'IRStoreWide'):
            is_w = (c == 'IRStoreWide')
            return _Access(k, 'computed', None, None, None, None, is_w)  # wide: conservative
        return _Access(k, 'computed', None, None, None, None, False)

    def _base_identity(self, base, k):
        """A hashable identity for a computed access's base VALUE, or None.
        Two accesses with equal, non-None base_id provably share the same base
        pointer value (so only their offsets need comparing)."""
        if not isinstance(base, Temp):
            return None
        d = self._single.get(base.name)
        if d is None:
            return None
        di = self.instrs[d]
        c = _cname(di)
        if c == 'IRLoadAddr':
            return ('stackaddr', di.fp_offset)             # &local (array base)
        if c == 'IRGlobalAddrOf':
            return ('global', di.dmem_addr)                # &global
        if c == 'IRLoad' and isinstance(di.base, Temp) and _is_zero(di.offset):
            pslot = self.addr_off.get(di.base.name)
            if pslot is not None and self._slot_invariant(pslot, k):
                return ('ptrslot', pslot)                  # invariant pointer local
        return None

    def _slot_invariant(self, slot, k):
        """The pointer stored in `slot` is the same value across all iterations of
        k's loop: the slot is clean (no alias writes it) and is not stored inside
        the loop. Outside any loop, trivially invariant."""
        d = self._desc_of.get(k)
        if d is None:
            return True                                    # straight-line: constant
        if slot not in self.clean_slots:
            return False
        return ('stack', slot) not in self.written_keys

    def _offset_affine(self, off, k):
        """(iv_slot, coef, const) for a byte offset: constant (coef 0), affine in a
        basic IV (coef = stride, from M1 iv_terms, optionally +/- a constant), or
        unknown (coef None). Reuses M1 `iv_terms` for the stride."""
        if isinstance(off, Const):
            return (None, 0, off.value)
        if not isinstance(off, Temp):
            return (None, None, None)
        d = self._desc_of.get(k)
        if d is not None and off.name in d.iv_terms:
            slot, scale = d.iv_terms[off.name]
            return (slot, scale, 0)
        # one level of +/- constant on an IV term:  (iv_term) +/- C
        di_idx = self._single.get(off.name)
        if d is not None and di_idx is not None:
            di = self.instrs[di_idx]
            if _cname(di) == 'IRBinOp' and di.op in ('+', '-'):
                L, R = di.left, di.right
                if (di.op == '+' and isinstance(R, Const) and isinstance(L, Temp)
                        and L.name in d.iv_terms):
                    s, sc = d.iv_terms[L.name]
                    return (s, sc, R.value)
                if (di.op == '+' and isinstance(L, Const) and isinstance(R, Temp)
                        and R.name in d.iv_terms):
                    s, sc = d.iv_terms[R.name]
                    return (s, sc, L.value)
                if (di.op == '-' and isinstance(R, Const) and isinstance(L, Temp)
                        and L.name in d.iv_terms):
                    s, sc = d.iv_terms[L.name]
                    return (s, sc, -R.value)
        return (None, None, None)

    # ── classification ────────────────────────────────────────────────────────

    def classify(self, i, j, carried):
        """Verdict for memory pair (i, j). `carried` picks the iteration relation:
        False = same iteration (intra), True = distinct iterations (loop-carried).
        i < j (program order) is assumed but the logic is symmetric."""
        A = self.access(i)
        B = self.access(j)

        # rule: a call barrier only clears against a clean local slot
        if A.category == 'barrier' or B.category == 'barrier':
            other = B if A.category == 'barrier' else A
            if other.category == 'stack' and other.base_id[1] in self.clean_slots:
                return _disjoint('clean-slot-vs-call')
            return _maybe('barrier')

        # rule 1: a clean local slot is unreachable by computed/global accesses
        for X, Y in ((A, B), (B, A)):
            if (X.category == 'stack' and X.base_id[1] in self.clean_slots
                    and Y.category in ('computed', 'global')):
                return _disjoint('clean-slot-vs-' + Y.category)

        # two zero-offset slot accesses: the base oracle only lets the SAME slot
        # through, so they address the identical scalar location.
        if A.category == 'stack' and B.category == 'stack':
            return _alias('same-stack-slot')            # same slot, both offset 0

        if A.base_id is not None and B.base_id is not None:
            if A.base_id == B.base_id:
                return self._siv(A, B, carried)
            # rule 2: distinct local objects never overlap
            if A.base_id[0] == 'stackaddr' and B.base_id[0] == 'stackaddr':
                return _disjoint('distinct-local-objects')
            return _maybe('distinct-base')

        return _maybe('unknown-base')

    def _siv(self, A, B, carried):
        """Single-index-variable test for two same-base accesses.

        An access address is  base + scale*v + const  where v is the induction
        variable's VALUE (v changes by the IV's step each iteration, NOT by 1).
        For equal scale the intra test is v-independent; the carried test must use
        the true per-iteration byte stride = scale * step, so it needs a nonzero,
        known step (else it stays conservative)."""
        ca, cb = A.coef, B.coef
        if ca is None or cb is None:
            return _maybe('same-base-unknown-offset')
        if A.iv_slot != B.iv_slot and ca != 0 and cb != 0:
            return _maybe('same-base-different-iv')       # MIV: stay conservative
        if ca != cb:
            return _maybe('same-base-different-scale')    # single intra solution
        dc = A.const - B.const
        if ca == 0:                                       # iteration-independent
            return _alias('same-const-address') if dc == 0 \
                else _disjoint('distinct-const-offset')
        if not carried:                                   # same iteration (same v)
            return _alias('same-affine-address') if dc == 0 \
                else _disjoint('distinct-affine-offset')
        # carried: distinct iterations, so distinct IV values v1 != v2 differing by
        # a nonzero multiple of `step`. A conflict needs scale*(v1-v2) = -dc, i.e.
        # dc divisible by the real stride scale*step. Requires a known nonzero step.
        step = self._iv_step(A.iv_slot, A.index)
        if step is None or step == 0:
            return _maybe('siv-carried-unknown-step')     # cannot prove: conservative
        if dc == 0:
            return _disjoint('siv-self-index-no-carry')   # a[i] vs a[i]: intra only
        stride = ca * step
        if stride != 0 and dc % stride == 0:
            return _maybe('siv-carried-distance')         # real dep at distance dc/stride
        return _disjoint('siv-gcd-no-solution')

    def _iv_step(self, iv_slot, ref_index):
        """The signed per-iteration step of the basic IV in `iv_slot`, from M1's
        `basic_ivs` for the loop containing `ref_index`; None if unknown."""
        if iv_slot is None:
            return None
        d = self._desc_of.get(ref_index)
        if d is None:
            return None
        biv = d.basic_ivs.get(iv_slot)
        return biv.step if biv is not None else None


# ── module-level construction / measurement ───────────────────────────────────

def _function_descs(instrs):
    """Discover loops and annotate M1 IVs + M2 memory effects, grouped by slice."""
    descs = discover(instrs)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    by_slice = {}
    for d in descs:
        by_slice.setdefault(d.func_slice, []).append(d)
    return by_slice


def build_disambiguated_function_graphs(instrs):
    """One R2.2-disambiguated DependenceGraph per function slice.

    Returns [(func_name, DependenceGraph)] in program order. Each graph is built
    with a MemoryDisambiguator so its memory edges are refined; register /
    control edges and the whole API are exactly as in R2.1."""
    by_slice = _function_descs(instrs)
    out = []
    for (lo, hi) in func_slices(instrs):
        du = DefUse(instrs, lo, hi)
        disamb = MemoryDisambiguator(instrs, lo, hi, by_slice.get((lo, hi), []), du)
        g = DependenceGraph(instrs, lo, hi, du=du, disambiguator=disamb)
        name = getattr(instrs[lo], 'name', None)
        out.append((name, g))
    return out


def disambiguate_function(instrs, lo, hi, descs=None, du=None):
    """Build one disambiguated graph for a single function slice."""
    if descs is None:
        descs = discover_function(instrs, lo, hi)
        annotate_induction_vars(descs)
        annotate_memory_effects(descs)
    if du is None:
        du = DefUse(instrs, lo, hi)
    disamb = MemoryDisambiguator(instrs, lo, hi, descs, du)
    return DependenceGraph(instrs, lo, hi, du=du, disambiguator=disamb)

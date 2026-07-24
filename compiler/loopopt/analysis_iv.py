"""
analysis_iv.py -- InductionVars analysis (Loop Optimization Framework, M1).

ANALYSIS ONLY. Populates the induction-variable portion of a LoopDescriptor. It
never mutates the IR, never makes an optimization decision, and never depends on
any transform (rotation / canonicalizer / pass manager / legality framework).

Model
-----
At the IR stage where loops are analyzed, local variables are MEMORY-BACKED:
an induction variable lives in a stack slot (an FP offset) and is updated by a
load-modify-store each iteration. So an IV is identified by its slot, exactly as
ivsr.py does. `iv_form` is therefore 'memory-slot' here; a future milestone may
add 'register' IVs (post loop-reg promotion) as an additional form.

A slot `off` is a BASIC induction variable when, inside the loop body:
  * it is stored exactly once, and
  * the stored value is  load(off) +/- Const   (Const = the step), and
  * the slot is CLEAN -- its address never escapes into anything other than a
    zero-offset load/store base (so no alias can update it behind our back).

Cleanness is a SOUNDNESS condition, checked per slot. It is deliberately NOT the
same as ivsr.py's loop-level bail-outs (a call, a wide store, a store through a
computed address). Those bails protect ivsr's TRANSFORM; they are legality, not
analysis. A clean local slot cannot be reached by any pointer, so its IV-ness
survives a call or an unrelated store. This analysis therefore reports the IV in
those loops where ivsr conservatively reports nothing -- a sound superset, and
the reason for the (explained) cross-check differences. Keeping analysis free of
legality is the whole point of the framework.

Derived IVs / IV terms mirror ivsr's `_iv_term`: a temp equal to `basic_iv * C`
(or a bare IV load, scale 1) is linear in that IV. Trip count mirrors ivsr's
`_trip_count`: a header test `iv < C` / `iv <= C` with a positive step and a
constant pre-loop init gives a KNOWN count; an affine test with a non-constant
bound/init is reported SYMBOLIC; anything else is UNKNOWN.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import Const, Temp                                         # noqa: E402
from ir_utils import dest_names, src_names                         # noqa: E402
from analysis import DefUse                                        # noqa: E402

IV_FORM_MEMORY = 'memory-slot'
IV_FORM_REGISTER = 'register'          # reserved for a future milestone


# ── result data structures (public; consumed by future passes) ────────────────

class BasicIV:
    """A basic induction variable held in stack slot `slot` (an FP offset)."""
    __slots__ = ('slot', 'step', 'elem_bytes', 'init', 'update_site', 'clean')

    def __init__(self, slot, step, elem_bytes, init, update_site, clean=True):
        self.slot = slot                # FP offset (the IV's identity)
        self.step = step                # signed per-iteration increment
        self.elem_bytes = elem_bytes    # width of the update's load
        self.init = init                # constant initial value, or None
        self.update_site = update_site  # IR index of the store that updates it
        self.clean = clean              # address never escapes (soundness)

    def __repr__(self):
        return (f"BasicIV(slot={self.slot:+d} step={self.step:+d} "
                f"init={self.init} @st{self.update_site})")


class DerivedIV:
    """A temp that is `base_iv * scale (+ offset)` -- linear in a basic IV."""
    __slots__ = ('name', 'base_slot', 'scale', 'offset', 'site')

    def __init__(self, name, base_slot, scale, offset, site):
        self.name = name
        self.base_slot = base_slot
        self.scale = scale
        self.offset = offset            # invariant additive part (int or None)
        self.site = site                # IR index of its defining instruction

    def __repr__(self):
        return (f"DerivedIV({self.name}=slot{self.base_slot:+d}*{self.scale}"
                f"{'' if not self.offset else f'+{self.offset}'})")


class TripCount:
    """Loop trip count: KNOWN (constant), SYMBOLIC (affine, non-constant), or
    UNKNOWN (not derivable)."""
    KNOWN = 'known'
    SYMBOLIC = 'symbolic'
    UNKNOWN = 'unknown'
    __slots__ = ('kind', 'value', 'expr')

    def __init__(self, kind, value=None, expr=None):
        self.kind = kind
        self.value = value              # int, when KNOWN
        self.expr = expr                # dict describing the affine form, when SYMBOLIC

    @classmethod
    def known(cls, n):
        return cls(cls.KNOWN, value=n)

    @classmethod
    def symbolic(cls, expr):
        return cls(cls.SYMBOLIC, expr=expr)

    @classmethod
    def unknown(cls):
        return cls(cls.UNKNOWN)

    def __repr__(self):
        if self.kind == self.KNOWN:
            return f"TripCount(KNOWN={self.value})"
        if self.kind == self.SYMBOLIC:
            return f"TripCount(SYMBOLIC={self.expr})"
        return "TripCount(UNKNOWN)"


# ── helpers ───────────────────────────────────────────────────────────────────

def _is_zero(off):
    return isinstance(off, Const) and off.value == 0


def _cname(x):
    return type(x).__name__


# ── the analysis ──────────────────────────────────────────────────────────────

def analyze_induction_variables(desc, du=None):
    """Compute IV information for one LoopDescriptor and store it on the
    descriptor's IV fields. Returns the descriptor. Pure analysis: mutates only
    the descriptor's IV fields, never the IR.

    `du` (a DefUse over the function slice) may be passed to share it across the
    loops of one function; otherwise it is built here."""
    instrs = desc.cfg.instrs
    lo, hi = desc.func_slice
    if du is None:
        du = DefUse(instrs, lo, hi)
    def_map = du.single_defs()          # name -> unique def index
    multi = du.multi_names()            # multiply-defined temp names

    # IR indices covered by the loop body (blocks are contiguous per block).
    region = set()
    for b in desc.body_blocks:
        blk = desc.cfg.blocks[b]
        region.update(range(blk.lo, blk.hi + 1))

    # address-temp -> stack offset it names
    addr_off = {name: instrs[k].fp_offset
                for name, k in def_map.items()
                if _cname(instrs[k]) == 'IRLoadAddr'}

    # cleanness: a slot's address must be used ONLY as a zero-offset load/store
    # base within the function slice; if it flows into arithmetic/args/etc. it
    # can escape and be aliased. (Function-slice-scoped: temp names restart per
    # function.)
    clean_use, other_use = set(), set()
    for k in range(lo, hi + 1):
        ins = instrs[k]
        c = _cname(ins)
        clean_base = None
        if c in ('IRLoad', 'IRStore') and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            clean_base = ins.base.name
        for sn in src_names(ins):
            (clean_use if sn == clean_base else other_use).add(sn)

    def _slot_clean(off):
        return not any(ao == off and an in other_use for an, ao in addr_off.items())

    def _iv_load_slot(name):
        """If `name` is a clean zero-offset load of a stack slot, its offset."""
        d = def_map.get(name)
        if d is None or d not in region:
            return None
        ins = instrs[d]
        if (_cname(ins) == 'IRLoad' and isinstance(ins.base, Temp)
                and _is_zero(ins.offset)):
            return addr_off.get(ins.base.name)
        return None

    # store counts per slot within the loop
    store_count = {}
    for k in region:
        ins = instrs[k]
        if (_cname(ins) == 'IRStore' and isinstance(ins.base, Temp)
                and _is_zero(ins.offset)):
            off = addr_off.get(ins.base.name)
            if off is not None:
                store_count[off] = store_count.get(off, 0) + 1

    # header block bounds, for init recovery and the guard test
    hb = desc.cfg.blocks[desc.header]

    def _recover_init(off):
        """Constant value last stored to `off` before the loop header, within
        the function slice; None if not a constant store."""
        for k in range(hb.lo - 1, lo - 1, -1):
            ins = instrs[k]
            if (_cname(ins) == 'IRStore' and isinstance(ins.base, Temp)
                    and _is_zero(ins.offset) and addr_off.get(ins.base.name) == off):
                return ins.src.value if isinstance(ins.src, Const) else None
        return None

    # ── basic induction variables ─────────────────────────────────────────────
    basic_ivs = {}
    for k in sorted(region):
        ins = instrs[k]
        if not (_cname(ins) == 'IRStore' and isinstance(ins.base, Temp)
                and _is_zero(ins.offset)):
            continue
        off = addr_off.get(ins.base.name)
        if off is None or store_count.get(off) != 1 or not _slot_clean(off):
            continue
        sval = ins.src
        if not isinstance(sval, Temp) or sval.name in multi:
            continue
        sd = def_map.get(sval.name)
        if sd is None or sd not in region:
            continue
        upd = instrs[sd]
        if _cname(upd) != 'IRBinOp' or upd.op not in ('+', '-'):
            continue
        L, R = upd.left, upd.right
        step = eb = None
        if isinstance(R, Const) and isinstance(L, Temp) and _iv_load_slot(L.name) == off:
            step = R.value if upd.op == '+' else -R.value
            eb = instrs[def_map[L.name]].elem_bytes
        elif (upd.op == '+' and isinstance(L, Const) and isinstance(R, Temp)
              and _iv_load_slot(R.name) == off):
            step = L.value
            eb = instrs[def_map[R.name]].elem_bytes
        if step is not None:
            basic_ivs[off] = BasicIV(off, step, eb, _recover_init(off), k, clean=True)

    # ── IV terms / derived IVs (temp == basic_iv * scale) ─────────────────────
    def _iv_term(name):
        if name in multi:
            return None
        ivo = _iv_load_slot(name)
        if ivo in basic_ivs:
            return (ivo, 1)
        d = def_map.get(name)
        if d is None or d not in region:
            return None
        ins = instrs[d]
        if _cname(ins) == 'IRBinOp' and ins.op == '*':
            for cst, oth in ((ins.right, ins.left), (ins.left, ins.right)):
                if isinstance(cst, Const) and isinstance(oth, Temp):
                    ivo = _iv_load_slot(oth.name)
                    if ivo in basic_ivs:
                        return (ivo, cst.value)
        return None

    iv_terms = {}
    derived_ivs = {}
    for k in sorted(region):
        for name in dest_names(instrs[k]):
            t = _iv_term(name)
            if t is None:
                continue
            slot, scale = t
            iv_terms[name] = (slot, scale)
            if scale != 1:                          # a genuinely scaled derived IV
                derived_ivs[name] = DerivedIV(name, slot, scale, offset=0, site=k)

    # ── primary IV, guard predicate, trip count ───────────────────────────────
    cond = None
    term = instrs[hb.hi]
    if _cname(term) == 'IRCondJump':
        cond = term
    else:                                           # not top-tested: find any exiting test
        for b in desc.exiting_blocks:
            t = instrs[desc.cfg.blocks[b].hi]
            if _cname(t) == 'IRCondJump':
                cond = t
                break

    primary_iv = None
    guard_predicate = None
    trip = TripCount.unknown()
    if cond is not None and isinstance(cond.left, Temp):
        ivo = _iv_load_slot(cond.left.name)
        if ivo in basic_ivs:
            primary_iv = ivo
            bound = (cond.right.value if isinstance(cond.right, Const)
                     else getattr(cond.right, 'name', str(cond.right)))
            guard_predicate = (ivo, cond.op, bound)
            trip = _compute_trip(basic_ivs[ivo], cond)

    # ── attach to the descriptor (IV fields only) ─────────────────────────────
    desc.iv_form = IV_FORM_MEMORY
    desc.basic_ivs = basic_ivs
    desc.derived_ivs = derived_ivs
    desc.iv_terms = iv_terms
    desc.primary_iv = primary_iv
    desc.guard_predicate = guard_predicate
    desc.trip_count = trip
    desc.is_counted = (trip.kind == TripCount.KNOWN)
    return desc


def _compute_trip(biv, cond):
    """Trip count from a header test against `biv`. KNOWN when the bound and
    init are constant and the step is positive (matches ivsr._trip_count);
    SYMBOLIC when the affine shape holds but the bound/init is not constant;
    UNKNOWN otherwise."""
    if cond.op not in ('<', '<='):
        return TripCount.unknown()
    step = biv.step
    if step <= 0:
        return TripCount.unknown()
    if isinstance(cond.right, Const) and biv.init is not None:
        bound = cond.right.value + (1 if cond.op == '<=' else 0)
        return TripCount.known(max(0, (bound - biv.init + step - 1) // step))
    # affine but non-constant bound or init -> symbolic
    bound_repr = (cond.right.value if isinstance(cond.right, Const)
                  else getattr(cond.right, 'name', str(cond.right)))
    return TripCount.symbolic({'iv': biv.slot, 'op': cond.op, 'bound': bound_repr,
                               'init': biv.init, 'step': step})


# ── convenience: annotate a set of descriptors, sharing DefUse per function ────

def annotate_induction_vars(descs):
    """Run the IV analysis over a list of LoopDescriptors, reusing one DefUse
    per function slice. Returns the same list (IV fields populated)."""
    du_cache = {}
    for d in descs:
        du = du_cache.get(d.func_slice)
        if du is None:
            lo, hi = d.func_slice
            du = DefUse(d.cfg.instrs, lo, hi)
            du_cache[d.func_slice] = du
        analyze_induction_variables(d, du)
    return descs

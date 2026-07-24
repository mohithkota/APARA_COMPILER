"""
analysis_mem.py -- MemEffects analysis (Loop Optimization Framework, M2).

ANALYSIS ONLY. Populates the memory-effects portion of a LoopDescriptor with
FACTS about the loop's memory operations. It never mutates IR, never decides
whether a transform is legal ("may LICM hoist this?" is the future Legality
Framework's job), and never depends on any transform.

It answers: what loads / stores / calls / global accesses exist; does the loop
read or write memory; which instructions are loop-invariant (value-flow fact);
and which memory locations are written (the alias summary future passes query).

================================================================================
ALIASING MODEL  (every assumption stated; validated against compiler behaviour)
================================================================================
At this IR stage memory is addressed three ways. Each access is given an ALIAS
KEY; two accesses may alias unless their keys are provably disjoint. This mirrors
the bundler's alias oracle (`_mem_may_alias`: same base register AND different
CONSTANT offsets => provably disjoint; anything else => may alias), lifted to the
IR's stack/global/computed distinction.

  1. STACK slot            IRLoad/IRStore with base = IRLoadAddr(fp_offset),
                           offset 0.   key = ('stack', fp_offset).
     * two stack keys alias  <=>  same fp_offset.
     * distinct fp_offsets are DISJOINT slots (the compiler assigns each local a
       non-overlapping slot; sub-word packing keeps each element at its own
       word, per ir.py) -- so ('stack', a) and ('stack', b), a != b, never alias.

  2. GLOBAL                 IRGlobalLoad/IRGlobalStore with dmem_addr + offset.
                           key = ('global', dmem_addr, off) where off is the
                           constant offset value, or the string 'var' if the
                           offset is a register/expression.
     * two global keys alias  <=>  same dmem_addr AND (same constant offset OR
       either offset is 'var').   Different dmem_addr => different global object
       => disjoint (distinct globals occupy distinct DMEM regions, ir.py).

  3. COMPUTED pointer      IRLoad/IRStore whose base is NOT a clean
                           IRLoadAddr(slot) -- a pointer produced by arithmetic.
                           key = ('computed',).
     * a computed access MAY alias any global, any ESCAPED stack slot, and any
       other computed access. It does NOT alias a CLEAN stack slot (one whose
       address never escaped -- no pointer to it can exist). Cleanness is the
       same escape test M1 uses for IV soundness, computed here from the
       function slice.

  ESCAPE / CLOBBER-ALL.  A call (IRCall/IRIndirectCall), a wide store
  (IRStoreWide), or a computed store sets `escapes_all` on the alias summary:
  such an operation may write arbitrary escaped/global memory. It still cannot
  touch a CLEAN stack slot (no pointer to it exists), so clean-slot loads remain
  analyzable even under escapes_all. This is exactly why M1 can find IVs in
  call-containing loops that ivsr conservatively skips.

INVARIANCE (value-flow fact, not a hoist decision):
  * an IRLoadAddr / IRGlobalAddrOf is invariant (a fixed address);
  * an IRBinOp/IRUnaryOp/IRAssign/IRCast is invariant iff all temp operands are
    invariant (operand invariant iff it has no definition inside the loop, or
    every in-loop definition is itself invariant) -- the same fixpoint licm2
    uses for these kinds, so licm2's invariant set is a subset of this one;
  * a load (IRLoad/IRGlobalLoad) is invariant iff its address is invariant AND
    its location is not written in the loop (its key not written, and not
    reachable by an escapes_all writer). Loads are included as an analysis FACT
    for future memory-LICM / pipelining; licm2 does not act on them (legality).
  Stores / calls are side effects, not value producers -> never "invariant".
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import Const, Temp                                         # noqa: E402
from ir_utils import src_names, dest_names                         # noqa: E402
from analysis import DefUse                                        # noqa: E402


def _is_zero(off):
    return isinstance(off, Const) and off.value == 0


def _cname(x):
    return type(x).__name__


# ── access / call records (public) ────────────────────────────────────────────

class MemAccess:
    """One memory operation and its alias key."""
    __slots__ = ('index', 'op', 'space', 'key', 'is_write')

    def __init__(self, index, op, space, key, is_write):
        self.index = index          # IR instruction index
        self.op = op                # class name, e.g. 'IRLoad'
        self.space = space          # 'stack' | 'global' | 'computed'
        self.key = key              # alias key (see module docstring)
        self.is_write = is_write

    def __repr__(self):
        return (f"MemAccess(@{self.index} {'W' if self.is_write else 'R'} "
                f"{self.space} key={self.key})")


class CallSite:
    __slots__ = ('index', 'name', 'indirect')

    def __init__(self, index, name, indirect):
        self.index = index
        self.name = name
        self.indirect = indirect

    def __repr__(self):
        return f"CallSite(@{self.index} {'*' if self.indirect else ''}{self.name})"


class AliasSummary:
    """The loop's written locations plus the alias oracle future passes query.

    `written_keys` are the keys of stores in the loop; `escapes_all` is set when
    a call / wide store / computed store may write arbitrary escaped or global
    memory. `may_write(key)` conservatively answers 'could the loop have written
    a location with this key?'."""
    __slots__ = ('written_keys', 'read_keys', 'escapes_all', 'clean_slots')

    def __init__(self, written_keys, read_keys, escapes_all, clean_slots):
        self.written_keys = written_keys        # set of alias keys
        self.read_keys = read_keys
        self.escapes_all = escapes_all
        self.clean_slots = clean_slots          # set of fp_offsets that never escape

    @staticmethod
    def may_alias(ka, kb):
        """The documented alias oracle: True unless provably disjoint."""
        if ka is None or kb is None:
            return True
        a, b = ka[0], kb[0]
        if a == 'stack' and b == 'stack':
            return ka[1] == kb[1]                # same slot only
        if a == 'global' and b == 'global':
            if ka[1] != kb[1]:
                return False                     # different global objects
            return ka[2] == kb[2] or 'var' in (ka[2], kb[2])
        if 'computed' in (a, b):
            # a computed access does not alias a CLEAN stack slot; the caller
            # uses clean_slots for that refinement. Here (no context) be
            # conservative for stack, exact-disjoint only for distinct globals.
            return True
        return True

    def may_write(self, key):
        """Could a store in the loop have written a location aliasing `key`?"""
        if key is not None and key[0] == 'stack' and key[1] in self.clean_slots:
            # a clean slot is unreachable by computed/global/call writers; only a
            # same-slot stack store can hit it.
            return ('stack', key[1]) in self.written_keys
        if self.escapes_all:
            return True
        return any(self.may_alias(key, wk) for wk in self.written_keys)


# ── address / key classification ──────────────────────────────────────────────

def _access_key(instrs, ins, addr_off):
    """(space, key, is_write) for a memory instruction, or None if not memory."""
    c = _cname(ins)
    if c in ('IRLoad', 'IRStore'):
        is_w = (c == 'IRStore')
        if isinstance(ins.base, Temp) and _is_zero(ins.offset) and ins.base.name in addr_off:
            return ('stack', ('stack', addr_off[ins.base.name]), is_w)
        return ('computed', ('computed',), is_w)     # pointer through arithmetic
    if c in ('IRGlobalLoad', 'IRGlobalStore'):
        is_w = (c == 'IRGlobalStore')
        off = ins.offset
        okey = off.value if isinstance(off, Const) else 'var'
        return ('global', ('global', ins.dmem_addr, okey), is_w)
    if c in ('IRLoadWide', 'IRStoreWide'):
        is_w = (c == 'IRStoreWide')
        return ('computed', ('computed',), is_w)     # wide group: treat as computed
    return None


# ── the analysis ──────────────────────────────────────────────────────────────

def analyze_memory_effects(desc, du=None):
    """Compute memory-effect facts for one LoopDescriptor and store them on its
    MemEffects fields. Returns the descriptor. Pure analysis (mutates only the
    descriptor's MemEffects fields)."""
    instrs = desc.cfg.instrs
    lo, hi = desc.func_slice
    if du is None:
        du = DefUse(instrs, lo, hi)
    def_map = du.single_defs()

    # IR indices in the loop body
    region = set()
    for b in desc.body_blocks:
        blk = desc.cfg.blocks[b]
        region.update(range(blk.lo, blk.hi + 1))

    # slot-address temps, and cleanness (escape) analysis over the function slice
    addr_off = {name: instrs[k].fp_offset
                for name, k in def_map.items()
                if _cname(instrs[k]) == 'IRLoadAddr'}
    clean_base = {}   # per-instruction: the base name used cleanly (offset-0 ld/st)
    other_use = set()
    for k in range(lo, hi + 1):
        ins = instrs[k]
        c = _cname(ins)
        cb = None
        if c in ('IRLoad', 'IRStore') and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            cb = ins.base.name
        clean_base[k] = cb
        for sn in src_names(ins):
            if sn != cb:
                other_use.add(sn)
    clean_slots = {ao for an, ao in addr_off.items() if an not in other_use}
    # a slot is clean only if EVERY address temp naming it is used cleanly
    dirty_slots = {ao for an, ao in addr_off.items() if an in other_use}
    clean_slots -= dirty_slots

    # ── catalogue accesses / calls / side effects ─────────────────────────────
    loads, stores, gaccess, calls = [], [], [], []
    written_keys, read_keys = set(), set()
    escapes_all = False
    for k in sorted(region):
        ins = instrs[k]
        c = _cname(ins)
        if c in ('IRCall', 'IRIndirectCall'):
            calls.append(CallSite(k, getattr(ins, 'func_name', None)
                                  or getattr(ins, 'func_ptr', '?'),
                                  c == 'IRIndirectCall'))
            escapes_all = True
            continue
        acc = _access_key(instrs, ins, addr_off)
        if acc is None:
            continue
        space, key, is_w = acc
        rec = MemAccess(k, c, space, key, is_w)
        if space == 'global':
            gaccess.append(rec)
        elif is_w:
            stores.append(rec)
        else:
            loads.append(rec)
        if is_w:
            written_keys.add(key)
            if space == 'computed':
                escapes_all = True          # store through a pointer: may clobber
        else:
            read_keys.add(key)

    alias = AliasSummary(written_keys, read_keys, escapes_all, clean_slots)

    # ── invariance fixpoint (value-flow fact) ─────────────────────────────────
    invariant = _compute_invariants(instrs, region, du, addr_off, alias)

    # ── attach facts ──────────────────────────────────────────────────────────
    desc.mem_analyzed = True
    desc.loads = loads
    desc.stores = stores
    desc.global_accesses = gaccess
    desc.calls = calls
    desc.may_read_memory = bool(loads) or any(not g.is_write for g in gaccess) or bool(calls)
    desc.may_write_memory = (bool(stores) or any(g.is_write for g in gaccess)
                             or bool(calls))
    desc.has_side_effects = bool(stores) or any(g.is_write for g in gaccess) or bool(calls)
    desc.invariant_insts = frozenset(invariant)
    desc.aliasing_summary = alias
    return desc


# whitelisted pure value producers whose invariance matches licm2's fixpoint
_PURE_KINDS = ('IRBinOp', 'IRUnaryOp', 'IRAssign', 'IRCast',
               'IRLoadAddr', 'IRGlobalAddrOf')
_LOAD_KINDS = ('IRLoad', 'IRGlobalLoad')


def _compute_invariants(instrs, region, du, addr_off, alias):
    """Loop-invariant instruction indices (memory-aware). For the pure kinds the
    rule is identical to licm2 (so licm2's invariant set is a subset); loads are
    additionally marked invariant when their location is provably not written."""
    inv = set()

    def op_inv(name):
        defs = [d for d in du.def_sites(name) if d in region]
        return (not defs) or all(d in inv for d in defs)

    def load_key(ins):
        c = _cname(ins)
        if c == 'IRLoad':
            if isinstance(ins.base, Temp) and _is_zero(ins.offset) and ins.base.name in addr_off:
                return ('stack', addr_off[ins.base.name])
            return ('computed',)
        # IRGlobalLoad
        okey = ins.offset.value if isinstance(ins.offset, Const) else 'var'
        return ('global', ins.dmem_addr, okey)

    changed = True
    while changed:
        changed = False
        for i in sorted(region):
            if i in inv:
                continue
            ins = instrs[i]
            c = _cname(ins)
            if c in _PURE_KINDS:
                if all(op_inv(nm) for nm in src_names(ins)):
                    inv.add(i)
                    changed = True
            elif c in _LOAD_KINDS:
                # address invariant AND location not written in the loop
                if all(op_inv(nm) for nm in src_names(ins)) and not alias.may_write(load_key(ins)):
                    inv.add(i)
                    changed = True
    return inv


# ── convenience: annotate a set of descriptors, sharing DefUse per function ────

def annotate_memory_effects(descs):
    """Run MemEffects over a list of LoopDescriptors, reusing one DefUse per
    function slice. Returns the same list (MemEffects fields populated)."""
    du_cache = {}
    for d in descs:
        du = du_cache.get(d.func_slice)
        if du is None:
            lo, hi = d.func_slice
            du = DefUse(d.cfg.instrs, lo, hi)
            du_cache[d.func_slice] = du
        analyze_memory_effects(d, du)
    return descs

"""
mem2reg.py -- conservative register promotion (Milestone 7).

Promotes an eligible local SCALAR stack slot into a single compiler Temp, so its
value flows through a register instead of load/store. This removes the barrier
that made GVN (and SCCP/LICM) ineffective: without promotion each variable read
is a fresh load, so equal values live in distinct temps and cannot be
value-numbered.

Transformation (no SSA, no phi -- we stay in the existing IR):
    store slot <- v   =>   IRAssign(P, v)      (P is the slot's promoted temp)
    load  t <- slot   =>   IRAssign(t, P)
The IRLoadAddr instructions that computed the slot address become dead and are
removed by the following DCE.

--------------------------------------------------------------------------------
Why this is correct WITHOUT phi nodes (single-store + dominance)
--------------------------------------------------------------------------------
We promote a slot ONLY when it has EXACTLY ONE store whose definition DOMINATES
every load of that slot. Then:
  * single store  => there is never a control-flow join of two DIFFERENT stored
    values, so no phi is ever needed (the promoted temp P is single-def);
  * store dominates all loads => the value is always defined before it is read,
    on every path.
So P holds exactly one value that reaches every use, and rewriting each load to
`t = P` is identical to reloading the slot. codegen's ordinary register
allocation handles a single-def temp trivially -- no merge, no loop-carried phi.

A MULTI-store variable (e.g. assigned in both arms of an if/else, or a
loop-updated accumulator) WOULD need a value merge at a join. This IR cannot
express a phi, and codegen does not unify the two arms' writes into one register
at a non-loop join, so promoting such a variable would miscompile. We therefore
leave every multi-store variable in memory. (Loop counters are handled
separately and safely by loop_reg, with its preheader-load / writeback
structure; mem2reg skips loop_reg's slots.)

--------------------------------------------------------------------------------
Eligibility (all required; if uncertain, do NOT promote)
--------------------------------------------------------------------------------
A stack slot (an IRLoadAddr fp_offset) is promoted only when:
  * every access is the clean scalar pattern IRLoadAddr(off) -> IRLoad/IRStore
    at offset 0 (an ordinary scalar object);
  * its address NEVER escapes -- every IRLoadAddr result is used ONLY as such a
    clean load/store base, never in arithmetic, never passed to a call, never
    stored (so the address is never taken, no aliasing, no indirect access);
  * a single consistent element width;
  * it carries no loop_reg scaffolding;
  * it has EXACTLY ONE store, and that store DOMINATES every load (see above).
Globals (IRGlobal*), arrays / structs / pointer-target memory (non-zero or
computed offsets, or an escaping base), wide accesses, multi-store variables,
and anything touched through a call's side effects stay in memory.
"""

import os
from ir import Const, Temp, IRAssign, IRCast
from ir_utils import func_slices, src_names
from analysis import build_cfg, compute_dominators


def _is_zero(off):
    return isinstance(off, Const) and off.value == 0


_m2r_n = [0]
def _fresh():
    _m2r_n[0] += 1
    return f"_m2r{_m2r_n[0]}"


class _Stats:
    def __init__(self):
        self.vars = 0
        self.loads = 0
        self.stores = 0


def _promote_function(instrs, lo, hi, stats):
    # address temp -> slot fp_offset (IRLoadAddr definitions)
    addr_off = {}
    for k in range(lo, hi + 1):
        ins = instrs[k]
        if type(ins).__name__ == 'IRLoadAddr' and isinstance(ins.dest, Temp):
            addr_off[ins.dest.name] = ins.fp_offset
    if not addr_off:
        return

    # escape analysis: an address temp is "clean" only if EVERY use of it is as
    # the base of an offset-0 load/store. Any other use taints the whole slot.
    clean = {a: True for a in addr_off}
    for k in range(lo, hi + 1):
        ins = instrs[k]
        c = type(ins).__name__
        clean_base = None
        if c in ('IRLoad', 'IRStore') and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            clean_base = ins.base.name
        for sn in src_names(ins):
            if sn in addr_off and sn != clean_base:
                clean[sn] = False

    tainted = {off for a, off in addr_off.items() if not clean[a]}

    # gather clean loads/stores per slot, element widths, and loop_reg scaffolding
    loads, stores, ebs, lr = {}, {}, {}, set()
    for k in range(lo, hi + 1):
        ins = instrs[k]
        c = type(ins).__name__
        if c == 'IRLoad' and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            off = addr_off.get(ins.base.name)
            if off is None:
                continue
            loads.setdefault(off, []).append(k)
            ebs.setdefault(off, set()).add(ins.elem_bytes)
            if getattr(ins, '_lr', False):
                lr.add(off)
        elif c == 'IRStore' and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            off = addr_off.get(ins.base.name)
            if off is None:
                continue
            stores.setdefault(off, []).append(k)
            ebs.setdefault(off, set()).add(ins.elem_bytes)
            if getattr(ins, '_lr', False):
                lr.add(off)

    # Dominance is needed to guarantee correctness (see below). Build the CFG +
    # dominators once for this function.
    cfg = build_cfg(instrs, lo, hi)
    dom = compute_dominators(cfg)
    block_of = {}
    for b in cfg.blocks:
        for i in range(b.lo, b.hi + 1):
            block_of[i] = b.id

    def _dominates(def_i, use_i):
        """Does the definition at index def_i dominate the use at index use_i?"""
        bd = block_of.get(def_i)
        bu = block_of.get(use_i)
        if bd is None or bu is None:
            return False
        if bd == bu:
            return def_i < use_i                  # same block: textually earlier
        return dom.dominates(bd, bu)

    for off in set(loads) | set(stores):
        if off in tainted:
            continue                              # address escapes -> keep in memory
        if off in lr:
            continue                              # loop_reg's slot -> leave it
        if len(ebs.get(off, ())) != 1:
            continue                              # inconsistent access width
        width = next(iter(ebs[off]))
        st = stores.get(off, [])
        lds = loads.get(off, [])
        # CONSERVATIVE + PROVABLY SAFE: promote only a variable with EXACTLY ONE
        # store whose definition DOMINATES every load. Single store => no
        # control-flow merge of distinct values (no phi needed). Dominance =>
        # the value is always defined before it is read. The promoted temp is
        # therefore single-def and its one value reaches every use, so codegen's
        # ordinary (non-loop) register allocation is correct -- unlike a
        # multi-store variable, whose values would merge at a join and need a phi
        # that this IR cannot express.
        if len(st) != 1:
            continue
        sk = st[0]
        if not all(_dominates(sk, lk) for lk in lds):
            continue
        p = Temp(_fresh())
        instrs[sk] = IRAssign(p, instrs[sk].src)   # store value flows into P as-is
        stats.stores += 1
        for lk in lds:
            ld = instrs[lk]
            if width == 8:
                instrs[lk] = IRAssign(ld.dest, p)          # pure move
            else:
                # A sub-word slot's store TRUNCATES to `width` bytes and its load
                # SIGN/ZERO-EXTENDS back. Replicate that exactly with a cast of P
                # per this load's own signedness, so e.g. `signed char c = 200`
                # still reads back as -56.
                src_ty = f"${'u' if getattr(ld, 'unsigned', False) else 'i'}{width * 8}"
                instrs[lk] = IRCast(ld.dest, p, '$i64', src_ty)
            stats.loads += 1
        stats.vars += 1


def mem2reg(instrs):
    """Promote eligible local scalar slots to Temps. Returns a NEW list (original
    instruction objects are never mutated -- rewrites replace list slots with
    fresh IRAssign). The now-dead IRLoadAddr instructions are cleaned by the
    following DCE."""
    if os.environ.get('APARA_NO_MEM2REG'):
        return instrs
    instrs = list(instrs)
    stats = _Stats()
    for lo, hi in func_slices(instrs):
        _promote_function(instrs, lo, hi, stats)
    if os.environ.get('APARA_MEM2REG_DEBUG'):
        print(f"[mem2reg] vars={stats.vars} loads={stats.loads} stores={stats.stores}")
    return instrs

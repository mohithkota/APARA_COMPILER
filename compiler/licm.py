"""
licm.py -- Loop-Invariant Code Motion for the APARA compiler.

Hoists provably loop-invariant address computations and LOADS out of innermost
loops into a preheader, so they execute once per loop entry instead of once per
iteration. This is what removes the redundant per-(i,j) reload of A's row in
matmul: row i is constant across the inner j-loop, yet without LICM it is
re-loaded on every j.

Operates on the flat IR list (between IR generation and code generation). It is
conservative by construction -- an instruction is hoisted only if:
  (a) it is a pure address / arithmetic / load operation (never a store/call),
  (b) all of its inputs are loop-invariant, and
  (c) for a load, the memory it reads is provably not written anywhere in the
      loop (no aliasing store, no opaque store, no call, no wide store).
When anything is uncertain it is left in place, so hoisting never changes
program semantics: only side-effect-free instructions whose value is identical
on every iteration are moved.
"""
from ir import *

_HOISTABLE  = {'IRLoadAddr', 'IRGlobalAddrOf', 'IRBinOp', 'IRAssign',
               'IRLoad', 'IRGlobalLoad', 'IRLoadWide'}
_LOAD_KINDS = {'IRLoad', 'IRGlobalLoad', 'IRLoadWide'}


def _dest_names(i):
    if type(i).__name__ == 'IRLoadWide':
        return [d.name for d in i.dests]
    d = getattr(i, 'dest', None)
    return [d.name] if isinstance(d, Temp) else []


def _src_names(i):
    c = type(i).__name__
    ops = []
    if   c == 'IRBinOp':        ops = [i.left, i.right]
    elif c == 'IRUnaryOp':      ops = [i.operand]
    elif c == 'IRAssign':       ops = [i.src]
    elif c == 'IRLoad':         ops = [i.base, i.offset]
    elif c == 'IRLoadWide':     ops = [i.base, i.offset]
    elif c == 'IRStore':        ops = [i.base, i.offset, i.src]
    elif c == 'IRStoreWide':    ops = [i.base, i.offset] + list(i.srcs)
    elif c == 'IRGlobalLoad':   ops = [i.offset]
    elif c == 'IRGlobalStore':  ops = [i.offset, i.src]
    elif c == 'IRGlobalAddrOf': ops = [i.offset]
    elif c == 'IRCondJump':     ops = [i.left, i.right]
    elif c == 'IRReturn':       ops = [i.value] if i.value is not None else []
    elif c == 'IRCall':         ops = list(i.args)
    elif c == 'IRIndirectCall': ops = ([i.func_ptr] if isinstance(i.func_ptr, Temp) else []) + list(i.args)
    elif c == 'IRCast':         ops = [i.src]
    elif c == 'IRFsqrt':        ops = [i.src]
    elif c == 'IRCmov':         ops = [i.check, i.src_true, i.src_false]
    elif c == 'IRSlice':        ops = [i.src]
    elif c == 'IRPack':         ops = [i.src1, i.src2]
    elif c == 'IRVecArith':     ops = [i.src1, i.src2]
    elif c == 'IRVecDot':       ops = [i.src1, i.src2] + ([i.accum] if getattr(i, 'accumulate', False) and i.accum else [])
    elif c == 'IRVecDot128':    ops = [i.a_lo, i.a_hi, i.b_lo, i.b_hi]
    elif c == 'IRVecReduce':    ops = [i.src]
    return [o.name for o in ops if isinstance(o, Temp)]


def _jump_targets(ins):
    c = type(ins).__name__
    if c == 'IRJump':     return [ins.label]
    if c == 'IRCondJump': return [ins.true_label, ins.false_label]
    return []


def _find_loops(instrs):
    """Natural loops as (header_idx, backedge_idx); smallest region first.
    A back-edge is a jump to an earlier label."""
    labels = {i.name: k for k, i in enumerate(instrs) if type(i).__name__ == 'IRLabel'}
    loops = []
    for k, ins in enumerate(instrs):
        for t in _jump_targets(ins):
            ti = labels.get(t)
            if ti is not None and ti < k:
                loops.append((ti, k))
    loops.sort(key=lambda se: se[1] - se[0])
    return loops


def _root_target(name, def_map, seen=None):
    """Memory region a base temp points at: ('g', addr) / ('s', off) / ('?',)."""
    if seen is None: seen = set()
    if name in seen: return ('?',)
    seen.add(name)
    d = def_map.get(name)
    if d is None: return ('?',)
    c = type(d).__name__
    if c == 'IRGlobalAddrOf': return ('g', d.dmem_addr)
    if c == 'IRGlobalLoad':   return ('g', d.dmem_addr)
    if c == 'IRLoadAddr':     return ('s', d.fp_offset)
    if c == 'IRBinOp' and d.op in ('+', '-'):
        for side in (d.left, d.right):
            if isinstance(side, Temp):
                r = _root_target(side.name, def_map, seen)
                if r[0] != '?': return r
    return ('?',)


def _hoist_one(instrs, s, e, def_map):
    header = instrs[s].name
    region = range(s, e + 1)
    # Safety: the preheader is inserted before the header label, so it is only
    # executed on the fall-through entry. If any jump from OUTSIDE the loop
    # targets the header, the preheader would be skipped -> refuse to hoist.
    for k, ins in enumerate(instrs):
        if (k < s or k > e) and header in _jump_targets(ins):
            return instrs

    loop_defs = set()
    multi_def = set()          # names assigned more than once in the loop
    for k in region:
        for d in _dest_names(instrs[k]):
            if d in loop_defs:
                multi_def.add(d)
            loop_defs.add(d)
    # A temp assigned more than once in the loop is control-dependent (e.g. the
    # res=0 / res=1 arms of a short-circuit `&&`/`||` or a comparison result).
    # Hoisting one such assignment out evaluates the condition once with stale
    # values -> the loop mis-behaves (e.g. `while(a && b)` never runs). Never
    # hoist a multiply-defined destination.

    # summary of memory written inside the loop
    clobber_all = False
    wr_globals, wr_stack = set(), set()
    for k in region:
        ins = instrs[k]; c = type(ins).__name__
        if c in ('IRCall', 'IRIndirectCall', 'IRStoreWide'):
            clobber_all = True
        elif c == 'IRGlobalStore':
            wr_globals.add(ins.dmem_addr)
        elif c == 'IRStore':
            r = _root_target(ins.base.name, def_map) if isinstance(ins.base, Temp) else ('?',)
            if   r[0] == 'g': wr_globals.add(r[1])
            elif r[0] == 's': wr_stack.add(r[1])
            else:             clobber_all = True

    def load_safe(ins):
        if clobber_all: return False
        if type(ins).__name__ == 'IRGlobalLoad':
            return ins.dmem_addr not in wr_globals
        base = getattr(ins, 'base', None)
        if not isinstance(base, Temp): return False
        r = _root_target(base.name, def_map)
        if r[0] == 'g': return r[1] not in wr_globals
        if r[0] == 's': return r[1] not in wr_stack
        return False

    inv, inv_dests = set(), set()
    changed = True
    while changed:
        changed = False
        for k in region:
            if k in inv: continue
            ins = instrs[k]
            if type(ins).__name__ not in _HOISTABLE: continue
            if any(d in multi_def for d in _dest_names(ins)):
                continue   # control-dependent (multiply-assigned) -> not invariant
            if not all((sn not in loop_defs) or (sn in inv_dests) for sn in _src_names(ins)):
                continue
            if type(ins).__name__ in _LOAD_KINDS and not load_safe(ins):
                continue
            inv.add(k); inv_dests.update(_dest_names(ins)); changed = True

    if not inv:
        return instrs
    hoisted = [instrs[k] for k in sorted(inv)]
    rest    = [instrs[k] for k in region if k not in inv]
    return instrs[:s] + hoisted + rest + instrs[e + 1:]


def hoist_loop_invariants(instrs):
    """Repeatedly hoist invariants out of innermost loops until stable."""
    for _ in range(100):
        def_map = {}
        for ins in instrs:
            for dn in _dest_names(ins):
                def_map[dn] = ins
        progressed = False
        for (s, e) in _find_loops(instrs):
            new = _hoist_one(instrs, s, e, def_map)
            if new is not instrs:
                instrs = new
                progressed = True
                break
        if not progressed:
            break
    return instrs

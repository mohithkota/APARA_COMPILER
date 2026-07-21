"""
ivsr.py -- Induction-Variable Strength Reduction (pointer strength reduction).

Eliminates per-iteration ADDRESS recomputation in loops. For an access whose
byte address is a linear function of a basic induction variable,

        addr = base + invariant + iv * stride          (base/invariant loop-invariant)

this pass maintains that whole address in ONE register that is initialized once
in a preheader and stepped by (stride * iv_step) each iteration -- turning a
multiply + offset-add + base-materialization (recomputed every iteration) into a
single `p += step`. It complements loop_reg.py, which keeps the induction
variable's VALUE in a register; IVSR removes the arithmetic DERIVED from it.

Runs BEFORE strength_reduce (so it sees the canonical `iv * Const` form, not a
mix of `<<` and `*`) and BEFORE loop_reg (so the basic IV is still the uniform
memory-backed load/increment/store pattern). A general dead-temp elimination
afterward removes the now-orphaned address arithmetic (that is where N actually
drops -- replacing a multiply with an add would otherwise be count-neutral).

ROBUSTNESS on this non-SSA IR: all backward tracing goes through a single-def
map (name -> its unique defining instruction; multiply-defined temps, e.g. the
two arms of a short-circuit `&&`, are excluded and the candidate is dropped).
The induction variable itself is memory-backed, so it is identified with the
same clean-slot / no-escape reasoning loop_reg uses. Every result is re-run
through codegen and kept only if it does not spill (compiler.py tiering), so
correctness cannot regress.

PROFITABILITY: a loop is transformed only when it strictly reduces per-iteration
instruction count, and -- when the trip count is a known small constant -- only
when the whole-loop saving beats the one-time preheader cost. This keeps small
loops from being pessimized by preheader setup.
"""

import os
from ir import *
# Shared analysis framework (compiler/analysis) + IR utilities (ir_utils),
# replacing IVSR's former private single-def map. Loop detection (_find_loops)
# still lives in licm until the CFG/loop-info milestone.
from licm import _find_loops
from ir_utils import (dest_names as _dest_names, src_names as _src_names,
                      jump_targets as _jump_targets, func_slices as _func_bounds,
                      enclosing_slice as _enclosing_func)
from analysis import DefUse

_DBG = bool(os.environ.get('APARA_IVSR_DEBUG'))
def _dbg(*a):
    if _DBG:
        print('[ivsr]', *a)


# ── small helpers ─────────────────────────────────────────────────────────────

def _is_zero(off):
    return isinstance(off, Const) and off.value == 0


_iv_n = [0]
def _fresh(prefix='_iv'):
    _iv_n[0] += 1
    return Temp(f"{prefix}{_iv_n[0]}")


# ── per-loop transform ────────────────────────────────────────────────────────

def _process_loop(instrs, s, e, def_map, multi, fa, fb):
    """Try to strength-reduce induction-variable addresses in loop [s..e].
    fa..fb is the enclosing FUNCTION slice: all TEMP-keyed analysis must stay
    inside it, because temp names restart per function (a bare whole-program
    scan lets another function's identically-named temp poison this slot's
    escape analysis -- same collision class as loop_reg/licm).
    Returns a new instruction list if it applied (profitably), else `instrs`."""
    header = instrs[s]
    if type(header).__name__ != 'IRLabel':
        return instrs
    header_name = header.name
    region = range(s, e + 1)

    # Single-entry: no jump from OUTSIDE may target the header (else the
    # preheader we insert before it would be skipped).
    for k, ins in enumerate(instrs):
        if (k < s or k > e) and header_name in _jump_targets(ins):
            return instrs

    # Bail on anything that makes memory analysis untrustworthy in the loop.
    loop_stored_offsets = set()
    loop_stored_globals = set()
    clobber_all = False
    for k in region:
        c = type(instrs[k]).__name__
        if c in ('IRCall', 'IRIndirectCall', 'IRStoreWide'):
            clobber_all = True
    if clobber_all:
        return instrs

    # address-temp -> stack offset it names  (function-slice-local elsewhere, but
    # def_map keys are unique per function slice already since Temp names restart)
    addr_off = {}
    for name, k in def_map.items():
        if type(instrs[k]).__name__ == 'IRLoadAddr':
            addr_off[name] = instrs[k].fp_offset

    # Which slots are STORED in the loop (for invariance of loads).
    for k in region:
        ins = instrs[k]; c = type(ins).__name__
        if c == 'IRStore' and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            off = addr_off.get(ins.base.name)
            if off is None:
                return instrs           # store through a non-trivial address: unsafe
            loop_stored_offsets.add(off)
        elif c == 'IRStore':
            return instrs               # store with non-zero offset / computed addr
        elif c == 'IRGlobalStore':
            loop_stored_globals.add(ins.dmem_addr)

    # Which address temps are used ONLY as clean (offset-0) load/store bases,
    # i.e. the slot's address never escapes into arithmetic/among args.
    clean_use, other_use = set(), set()
    for k in range(fa, fb + 1):                    # function-slice-local (see docstring)
        ins = instrs[k]
        c = type(ins).__name__
        clean_base = None
        if c in ('IRLoad', 'IRStore') and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            clean_base = ins.base.name
        for sn in _src_names(ins):
            (clean_use if sn == clean_base else other_use).add(sn)
    def _slot_clean(off):
        for aname, ao in addr_off.items():
            if ao == off and aname in other_use:
                return False
        return True

    # ── basic induction variables: slot updated exactly once, by load(off)+/-c ──
    def _is_iv_load_name(name):
        d = def_map.get(name)
        if d is None or not (s <= d <= e):
            return None
        ins = instrs[d]
        if (type(ins).__name__ == 'IRLoad' and isinstance(ins.base, Temp)
                and _is_zero(ins.offset)):
            return addr_off.get(ins.base.name)
        return None

    basic_iv = {}          # off -> (step, elem_bytes)
    iv_store_count = {}
    for k in region:
        ins = instrs[k]
        if (type(ins).__name__ == 'IRStore' and isinstance(ins.base, Temp)
                and _is_zero(ins.offset)):
            off = addr_off.get(ins.base.name)
            iv_store_count[off] = iv_store_count.get(off, 0) + 1
    for k in region:
        ins = instrs[k]
        if not (type(ins).__name__ == 'IRStore' and isinstance(ins.base, Temp)
                and _is_zero(ins.offset)):
            continue
        off = addr_off.get(ins.base.name)
        if off is None or iv_store_count.get(off) != 1 or not _slot_clean(off):
            continue
        sval = ins.src
        if not isinstance(sval, Temp) or sval.name in multi:
            continue
        sd = def_map.get(sval.name)
        if sd is None or not (s <= sd <= e):
            continue
        upd = instrs[sd]
        if type(upd).__name__ != 'IRBinOp' or upd.op not in ('+', '-'):
            continue
        L, R = upd.left, upd.right
        step = None; eb = None
        if isinstance(R, Const) and isinstance(L, Temp) and _is_iv_load_name(L.name) == off:
            step = R.value if upd.op == '+' else -R.value
            eb = instrs[def_map[L.name]].elem_bytes
        elif (upd.op == '+' and isinstance(L, Const) and isinstance(R, Temp)
              and _is_iv_load_name(R.name) == off):
            step = L.value
            eb = instrs[def_map[R.name]].elem_bytes
        if step is not None:
            basic_iv[off] = (step, eb)

    if not basic_iv:
        _dbg(f"loop @{s}: no basic IVs")
        return instrs
    _dbg(f"loop @{s} ({instrs[s].name}): basic_iv offsets={list(basic_iv.keys())}")

    # ── invariance test (memoized) ────────────────────────────────────────────
    inv_memo = {}
    def _inv(name):
        if name in inv_memo:
            return inv_memo[name]
        inv_memo[name] = False                    # cut cycles conservatively
        res = _inv_compute(name)
        inv_memo[name] = res
        return res
    def _opnd_inv(o):
        return True if isinstance(o, Const) else (isinstance(o, Temp) and _inv(o.name))
    def _inv_compute(name):
        if name in multi:
            return False
        d = def_map.get(name)
        if d is None:
            return True                           # param / defined before slice
        if d < s or d > e:
            return True                           # defined before the loop
        ins = instrs[d]; c = type(ins).__name__
        if c in ('IRGlobalAddrOf', 'IRLoadAddr'):
            return True
        if c == 'IRAssign':
            return _opnd_inv(ins.src)
        if c == 'IRBinOp':
            return _opnd_inv(ins.left) and _opnd_inv(ins.right)
        if c == 'IRUnaryOp':
            return _opnd_inv(ins.operand)
        if c == 'IRCast':
            return _opnd_inv(ins.src)
        if c == 'IRLoad' and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            off = addr_off.get(ins.base.name)
            return off is not None and off not in loop_stored_offsets
        if c == 'IRGlobalLoad':
            return ins.dmem_addr not in loop_stored_globals and _opnd_inv(ins.offset)
        return False

    # ── decompose an offset temp into (invariant parts, single iv term) ────────
    def _iv_term(name):
        """(iv_off, m) if `name` == iv*Const (or a bare iv load, m=1), else None."""
        if name in multi:
            return None
        ivo = _is_iv_load_name(name)
        if ivo in basic_iv:
            return (ivo, 1)
        d = def_map.get(name)
        if d is None or not (s <= d <= e):
            return None
        ins = instrs[d]
        if type(ins).__name__ == 'IRBinOp' and ins.op == '*':
            for cst, oth in ((ins.right, ins.left), (ins.left, ins.right)):
                if isinstance(cst, Const) and isinstance(oth, Temp):
                    ivo = _is_iv_load_name(oth.name)
                    if ivo in basic_iv:
                        return (ivo, cst.value)
        return None

    def _decompose(name):
        """(inv_operands, (iv_off, m)) with exactly one iv term, else None.
        inv_operands is a list of ('t', tempname) / ('c', constval)."""
        t = _iv_term(name)
        if t is not None:
            return ([], t)
        if _inv(name):
            return ([('t', name)], None)
        d = def_map.get(name)
        if d is None or not (s <= d <= e):
            return None
        ins = instrs[d]
        if type(ins).__name__ == 'IRBinOp' and ins.op == '+':
            parts, iv = [], None
            for opnd in (ins.left, ins.right):
                if isinstance(opnd, Const):
                    parts.append(('c', opnd.value)); continue
                if not isinstance(opnd, Temp):
                    return None
                sub = _decompose(opnd.name)
                if sub is None:
                    return None
                sub_parts, sub_iv = sub
                parts.extend(sub_parts)
                if sub_iv is not None:
                    if iv is not None:
                        return None               # nonlinear (two IV terms)
                    iv = sub_iv
            return (parts, iv)
        return None

    # ── collect candidate memory accesses ─────────────────────────────────────
    candidates = []   # (mem_idx, kind, base_name, inv_parts, iv_off, m, eb, dest_or_src, uns)
    for k in region:
        ins = instrs[k]; c = type(ins).__name__
        if c not in ('IRLoad', 'IRStore'):
            continue
        if not isinstance(ins.base, Temp) or not isinstance(ins.offset, Temp):
            continue
        if ins.base.name in multi or not _inv(ins.base.name):
            continue
        dec = _decompose(ins.offset.name)
        if dec is None:
            continue
        inv_parts, iv = dec
        if iv is None:
            continue
        iv_off, m = iv
        if iv_off not in basic_iv:
            continue
        if c == 'IRLoad':
            candidates.append((k, 'ld', ins.base.name, inv_parts, iv_off, m,
                               ins.elem_bytes, ins.dest, ins.unsigned))
        else:
            candidates.append((k, 'st', ins.base.name, inv_parts, iv_off, m,
                               ins.elem_bytes, ins.src, False))
    _dbg(f"loop @{s}: {len(candidates)} candidate accesses")
    if not candidates:
        return instrs

    # ── build preheader clones of invariant values ────────────────────────────
    preheader = []
    clone_memo = {}
    def _clone(name):
        """Return a temp holding `name`'s (loop-invariant) value, available in
        the preheader; clone its defining instruction there if it is computed
        inside the loop, else reuse the pre-loop temp as-is."""
        if name in clone_memo:
            return clone_memo[name]
        d = def_map.get(name)
        if d is None or d < s or d > e:
            clone_memo[name] = name               # already available before loop
            return name
        ins = instrs[d]; c = type(ins).__name__
        nd = _fresh().name
        if c == 'IRGlobalAddrOf':
            preheader.append(IRGlobalAddrOf(Temp(nd), ins.dmem_addr, ins.offset))
        elif c == 'IRLoadAddr':
            preheader.append(IRLoadAddr(Temp(nd), ins.fp_offset))
        elif c == 'IRAssign':
            src = ins.src
            src2 = Const(src.value) if isinstance(src, Const) else Temp(_clone(src.name))
            preheader.append(IRAssign(Temp(nd), src2))
        elif c == 'IRBinOp':
            l = Const(ins.left.value) if isinstance(ins.left, Const) else Temp(_clone(ins.left.name))
            r = Const(ins.right.value) if isinstance(ins.right, Const) else Temp(_clone(ins.right.name))
            preheader.append(IRBinOp(Temp(nd), ins.op, l, r,
                                     unsigned=ins.unsigned, ftype=ins.ftype))
        elif c == 'IRCast':
            preheader.append(IRCast(Temp(nd), Temp(_clone(ins.src.name)),
                                    ins.dest_type, ins.src_type))
        elif c == 'IRLoad':
            ab = Temp(_clone(ins.base.name))
            preheader.append(IRLoad(Temp(nd), ab, Const(0), ins.elem_bytes, ins.unsigned))
        elif c == 'IRGlobalLoad':
            off = Const(ins.offset.value) if isinstance(ins.offset, Const) else Temp(_clone(ins.offset.name))
            preheader.append(IRGlobalLoad(Temp(nd), ins.dmem_addr, off,
                                          elem_bytes=ins.elem_bytes, unsigned=ins.unsigned))
        else:
            clone_memo[name] = name               # give up cloning; reuse (may be wrong-scope -> guarded by _inv earlier)
            return name
        clone_memo[name] = nd
        return nd

    # preheader value of each iv slot, materialized once
    iv_entry = {}
    def _iv_entry_val(iv_off, eb):
        if iv_off in iv_entry:
            return iv_entry[iv_off]
        a = _fresh().name; v = _fresh().name
        preheader.append(IRLoadAddr(Temp(a), iv_off))
        preheader.append(IRLoad(Temp(v), Temp(a), Const(0), eb))
        iv_entry[iv_off] = v
        return v

    # ── create the pointers and the body rewrites ─────────────────────────────
    mem_replace = {}       # mem_idx -> new IR instruction
    increments = []        # p += step, appended before the back-edge
    for (k, kind, base_name, inv_parts, iv_off, m, eb, ds, uns) in candidates:
        step_iv, iv_eb = basic_iv[iv_off]
        # p_init = base + Σ invariants + iv_entry*m
        terms = [Temp(_clone(base_name))]
        for tag, val in inv_parts:
            terms.append(Const(val) if tag == 'c' else Temp(_clone(val)))
        ev = _iv_entry_val(iv_off, iv_eb)
        if m == 1:
            terms.append(Temp(ev))
        else:
            scaled = _fresh().name
            preheader.append(IRBinOp(Temp(scaled), '*', Temp(ev), Const(m)))
            terms.append(Temp(scaled))
        p = _fresh('_ivp').name
        acc = terms[0]
        for t in terms[1:]:
            nxt = _fresh().name
            preheader.append(IRBinOp(Temp(nxt), '+', acc, t))
            acc = Temp(nxt)
        preheader.append(IRAssign(Temp(p), acc))
        if kind == 'ld':
            mem_replace[k] = IRLoad(ds, Temp(p), Const(0), eb, uns)
        else:
            mem_replace[k] = IRStore(Temp(p), Const(0), ds, eb)
        increments.append(IRBinOp(Temp(p), '+', Temp(p), Const(step_iv * m)))

    # ── profitability gate ────────────────────────────────────────────────────
    #   saved/iter  = (address-arithmetic instructions that DCE will remove)
    #                 - (pointer increments added)
    # Estimate removals as the cone of each rewritten offset/base that is used
    # only by the rewritten access; measured exactly by a local liveness sweep.
    removed = _count_dead_after(instrs, s, e, fa, fb, mem_replace)
    saved_per_iter = removed - len(increments)
    pre_cost = len(preheader)
    trips = _trip_count(instrs, s, e, def_map, addr_off, basic_iv)
    _dbg(f"loop @{s}: removed={removed} incr={len(increments)} "
         f"saved/iter={saved_per_iter} pre={pre_cost} trips={trips}")
    if saved_per_iter <= 0:
        return instrs                              # neutral/harmful: skip
    if trips is not None and saved_per_iter * trips <= pre_cost:
        return instrs                              # small loop: setup not amortized
    _dbg(f"loop @{s}: APPLIED ({len(candidates)} pointers)")

    # ── splice: preheader + rewritten region (+ increments before back-edge) ──
    new_region = []
    for k in region:
        if k in mem_replace:
            new_region.append(mem_replace[k])
        else:
            new_region.append(instrs[k])
    new_region = new_region[:-1] + increments + [new_region[-1]]
    return instrs[:s] + preheader + new_region + instrs[e + 1:]


def _count_dead_after(instrs, s, e, fa, fb, mem_replace):
    """How many loop-region instructions become dead once the accesses in
    `mem_replace` are swapped for their pointer form: local iterative dead-temp
    count within [s..e]. Counts only side-effect-free instructions. `uses_outside`
    is scoped to the enclosing function slice [fa,fb] (temp names restart per
    function -- a whole-program scan would see an unrelated same-named temp)."""
    reg = list(range(s, e + 1))
    alive = {k: (mem_replace[k] if k in mem_replace else instrs[k]) for k in reg}
    removed = set()
    changed = True
    def uses_outside(name):
        for k in range(fa, fb + 1):
            if s <= k <= e:
                continue
            for sn in _src_names(instrs[k]):
                if sn == name:
                    return True
        return False
    while changed:
        changed = False
        # count reads within region of every temp
        reads = {}
        for k in reg:
            if k in removed:
                continue
            for sn in _src_names(alive[k]):
                reads[sn] = reads.get(sn, 0) + 1
        for k in reg:
            if k in removed:
                continue
            ins = alive[k]
            if type(ins).__name__ not in (
                    'IRBinOp', 'IRUnaryOp', 'IRAssign', 'IRLoadAddr',
                    'IRGlobalAddrOf', 'IRLoad', 'IRGlobalLoad', 'IRCast'):
                continue
            dests = _dest_names(ins)
            if not dests:
                continue
            if all(reads.get(dn, 0) == 0 and not uses_outside(dn) for dn in dests):
                removed.add(k); changed = True
    return len(removed)


def _trip_count(instrs, s, e, def_map, addr_off, basic_iv):
    """Best-effort constant trip count from `if iv < Const goto ...` at the
    header plus a constant pre-loop init; None if not provable."""
    # header condition
    cond = None
    for k in range(s, min(e, s + 4) + 1):
        if type(instrs[k]).__name__ == 'IRCondJump':
            cond = instrs[k]; break
    if cond is None or not isinstance(cond.right, Const) or cond.op not in ('<', '<='):
        return None
    if not isinstance(cond.left, Temp):
        return None
    ivo = None
    d = def_map.get(cond.left.name)
    if d is not None and type(instrs[d]).__name__ == 'IRLoad' and isinstance(instrs[d].base, Temp):
        ivo = addr_off.get(instrs[d].base.name)
    if ivo not in basic_iv:
        return None
    step, _ = basic_iv[ivo]
    if step <= 0:
        return None
    bound = cond.right.value + (1 if cond.op == '<=' else 0)
    # pre-loop constant init: last store of ivo before s
    init = None
    for k in range(s - 1, -1, -1):
        ins = instrs[k]
        if (type(ins).__name__ == 'IRStore' and isinstance(ins.base, Temp)
                and _is_zero(ins.offset) and addr_off.get(ins.base.name) == ivo):
            if isinstance(ins.src, Const):
                init = ins.src.value
            break
    if init is None:
        return None
    return max(0, (bound - init + step - 1) // step)


# ── general dead-temp elimination ─────────────────────────────────────────────

_PURE = {'IRBinOp', 'IRUnaryOp', 'IRAssign', 'IRLoadAddr', 'IRGlobalAddrOf',
         'IRLoad', 'IRGlobalLoad', 'IRCast', 'IRSlice', 'IRPack', 'IRFsqrt',
         'IRFuncAddr', 'IRVaStart', 'IRVecArith', 'IRVecDot', 'IRVecDot128',
         'IRVecReduce'}


def _dce_slice(instrs, lo, hi):
    """DCE within [lo, hi) using reads counted ONLY in that slice. Temp names
    restart per function, so read-counting must stay inside one function slice
    or a dead temp whose name is reused (and live) in another function would
    look used and never be removed. Returns the rebuilt slice list."""
    seg = instrs[lo:hi]
    changed = True
    while changed:
        changed = False
        reads = {}
        for ins in seg:
            for sn in _src_names(ins):
                reads[sn] = reads.get(sn, 0) + 1
        out = []
        for ins in seg:
            if type(ins).__name__ in _PURE:
                dests = _dest_names(ins)
                if dests and all(reads.get(d, 0) == 0 for d in dests):
                    changed = True
                    continue
            out.append(ins)
        seg = out
    return seg


def dead_temp_elim(instrs):
    """Remove pure instructions whose results are never read, function by
    function (see _dce_slice). Safe in this fault-free VM (a dead load cannot
    trap). Anything outside a FUNC_BEGIN..FUNC_END slice is left untouched."""
    out = []
    i = 0
    n = len(instrs)
    while i < n:
        if type(instrs[i]).__name__ == 'IRFuncBegin':
            j = i + 1
            while j < n and type(instrs[j]).__name__ != 'IRFuncEnd':
                j += 1
            end = j + 1 if j < n else n            # include IRFuncEnd
            out.extend(_dce_slice(instrs, i, end))
            i = end
        else:
            out.append(instrs[i])
            i += 1
    return out


# ── public entry ──────────────────────────────────────────────────────────────

def induction_strength_reduce(instrs):
    """Apply IVSR to every eligible loop (innermost first), then clean up the
    orphaned address arithmetic. Returns a new instruction list."""
    for _ in range(200):
        progressed = False
        bounds = _func_bounds(instrs)
        for (s, e) in _find_loops(instrs):
            fa, fb = _enclosing_func(bounds, s, e, len(instrs))
            # Shared Def-Use analysis (compiler/analysis) replaces IVSR's former
            # private single-def map. single_defs()/multi_names() reproduce the
            # exact (def_map, multi) the old _single_def_map returned, so
            # _process_loop is unchanged and output is byte-identical.
            du = DefUse(instrs, fa, fb)
            new = _process_loop(instrs, s, e, du.single_defs(), du.multi_names(), fa, fb)
            if new is not instrs:
                instrs = new
                progressed = True
                break
        if not progressed:
            break
    return dead_temp_elim(instrs)

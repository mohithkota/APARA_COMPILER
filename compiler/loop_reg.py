"""
loop_reg.py -- Loop-carried register promotion for induction variables.

The compiler's memory model gives every named local its own stack slot, so a
loop counter is RELOADED from memory at the top of every iteration and STORED
back on every increment -- a full memory round-trip per iteration:

    wc_4:                          # inner-loop top (back-edge target)
        $ld ($i32) $r15 [$r4 + 0]  # <- reload j from its stack slot, every iter
        ...
        $st ($i32) [$r4 + 0] ...   # <- store j back on increment
        ? $r0 == $goto wc_4        # back-edge re-executes the $ld

This pass promotes such a counter into a register that persists ACROSS the
back-edge: load it once into a fresh temp in a preheader, turn every in-loop
load/store of it into a register move, and write it back once at each loop
exit. The existing loop-aware live-range extension in codegen keeps the temp
live across the back-edge.

Deliberately NARROW and conservative (counters only). A stack slot is promoted
only when ALL of the following hold, otherwise it is left memory-backed:
  * single-entry loop (no jump from outside the loop targets the header, so the
    preheader is never skipped);
  * the slot is touched, EVERYWHERE in the function, only via the exact
    IRLoadAddr -> immediate IRLoad/IRStore(offset 0) pattern, and the address
    temp is used for nothing else (its address never escapes -- no &var passed
    to a call, stored, or used in arithmetic);
  * it is both loaded AND stored inside the loop (a genuine loop-carried
    variable, i.e. a counter/accumulator), with one consistent element width;
  * the loop body contains no call (avoids any interaction with the caller-save
    convention -- conservative; refusing only forgoes the optimization).

Correctness still cannot regress: the result is rerun through codegen and kept
only if it introduces no spilling and does not crash (see compiler.py), exactly
like LICM. This file uses only existing IR nodes (no codegen changes).
"""
from ir import *
from licm import _find_loops, _jump_targets


_lr_n = 0
def _new_temp():
    """Fresh temp in a namespace ('_lrN') that cannot collide with ir_gen's
    per-function '_tN' names. Temp() would continue the global counter from
    wherever the LAST function's IR generation left it (Temp.reset() runs per
    function), so a bare Temp() here can reuse a name the current function
    already defines -- two live values then share one register (u2_binsearch
    infinite-loop bug, 2026-07-18)."""
    global _lr_n
    _lr_n += 1
    return Temp(f"_lr{_lr_n}")


def _is_zero(off):
    return isinstance(off, Const) and off.value == 0


def _build_addr_temp_uses(instrs):
    """name -> list of (idx, role) where role is 'base_load'/'base_store'/'other'
    for every temp that is read. Used to prove an address temp never escapes."""
    from licm import _src_names
    uses = {}
    for k, ins in enumerate(instrs):
        c = type(ins).__name__
        clean_base = None
        if c == 'IRLoad' and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            clean_base = ins.base.name
        elif c == 'IRStore' and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            clean_base = ins.base.name
        for sn in _src_names(ins):
            role = 'clean' if sn == clean_base else 'other'
            uses.setdefault(sn, []).append(role)
    return uses


def _promote_one(instrs, s, e, promoted_offsets, fa, fb, fkey):
    """fa..fb (inclusive) is the enclosing FUNCTION slice: temp names restart
    per function, so the addr_off / escape analyses below must never scan
    outside it (a later function's IRLoadAddr with the same temp name would
    override this one's and resolve a load/store to the wrong slot — same
    cross-function name-collision class as licm's def_map bug, 2026-07-18).

    fkey is a STABLE per-function identity (the function's name) used to key
    promoted_offsets. It must NOT be fa: fa is a raw instruction index that
    SHIFTS every time an earlier function's loop is promoted (each promotion
    inserts preheader/write-back instructions), so keying by (fa, off) lets an
    offset already promoted by an inner loop escape the dedup guard once fa has
    moved -- the enclosing loop then re-promotes the same slot, double-wrapping
    a loop-carried accumulator and corrupting it. Found via the cONNXr MNIST
    port: nn_maxpool's `cur = max(...)` reduction silently stuck at -FLT_MAX,
    but only when the loop-heavy nn_conv2d was promoted just before it (2026-07-19)."""
    header = instrs[s]
    if type(header).__name__ != 'IRLabel':
        return instrs
    header_name = header.name
    region = range(s, e + 1)

    # Single-entry: no jump from OUTSIDE the loop may target the header, or the
    # preheader (inserted before the header label) would be skipped.
    for k, ins in enumerate(instrs):
        if (k < s or k > e) and header_name in _jump_targets(ins):
            return instrs

    # No calls in the loop body (conservative; see module docstring).
    for k in region:
        if type(instrs[k]).__name__ in ('IRCall', 'IRIndirectCall'):
            return instrs

    addr_uses = _build_addr_temp_uses(instrs[fa:fb + 1])

    # Discover, per stack offset, the load/store pairs inside the loop and
    # whether the slot is clean (address never escapes) function-wide.
    # off -> dict(loads=[(la_idx, ld_idx, v, eb, uns)], stores=[(la_idx, st_idx, sval, eb)],
    #             ebs=set(), clean=bool)
    info = {}
    # Map: address-temp name -> the IRLoadAddr offset that produced it
    # (built over THIS function slice only, see docstring).
    addr_off = {}
    for k in range(fa, fb + 1):
        ins = instrs[k]
        if type(ins).__name__ == 'IRLoadAddr':
            addr_off[ins.dest.name] = ins.fp_offset

    # Verify every IRLoadAddr's result is used only as a clean load/store base,
    # function-wide. If not, that offset's address escapes -> not promotable.
    escaped_offsets = set()
    for aname, off in addr_off.items():
        roles = addr_uses.get(aname, [])
        if any(r != 'clean' for r in roles):
            escaped_offsets.add(off)

    for k in region:
        ins = instrs[k]
        c = type(ins).__name__
        # Skip scaffolding (preheader load / write-back store) this pass itself
        # introduced in an inner loop -- it matches the same pattern and must
        # not be re-promoted by an enclosing loop.
        if getattr(ins, '_lr', False):
            continue
        if c == 'IRLoad' and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            off = addr_off.get(ins.base.name)
            if off is None:
                continue
            d = info.setdefault(off, {'loads': [], 'stores': [], 'ebs': set()})
            d['loads'].append((ins.base.name, k, ins.dest, ins.elem_bytes, ins.unsigned))
            d['ebs'].add(ins.elem_bytes)
        elif c == 'IRStore' and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            off = addr_off.get(ins.base.name)
            if off is None:
                continue
            d = info.setdefault(off, {'loads': [], 'stores': [], 'ebs': set()})
            d['stores'].append((ins.base.name, k, ins.src, ins.elem_bytes))
            d['ebs'].add(ins.elem_bytes)

    # Select promotable offsets: clean, both loaded & stored in loop, one width.
    # Never promote an offset already promoted by an inner loop: that loop's
    # (tagged) preheader/write-back still reads & writes this slot's memory, and
    # re-promoting in the enclosing loop would stop maintaining that memory.
    promote = {}
    for off, d in info.items():
        # promoted_offsets is keyed (func_name, off): raw offsets collide
        # across functions (every function has its own -16 slot), and the
        # function name is stable while fa (an index) is not -- see docstring.
        if off in escaped_offsets or (fkey, off) in promoted_offsets:
            continue
        if not d['loads'] or not d['stores']:
            continue
        if len(d['ebs']) != 1:
            continue
        promote[off] = d
    if not promote:
        return instrs
    promoted_offsets.update((fkey, o) for o in promote.keys())

    # Indices of the IRLoadAddr instructions that feed a promoted load/store and
    # so become dead, plus the load/store indices to rewrite into moves.
    dead_loadaddr = set()
    load_rewrite = {}   # ld_idx -> (v_temp)
    store_rewrite = {}  # st_idx -> (sval)
    off_for_vreg = {}   # off -> fresh vreg Temp
    eb_for_off = {}
    uns_for_off = {}
    for off, d in promote.items():
        vreg = _new_temp()
        off_for_vreg[off] = vreg
        eb_for_off[off] = next(iter(d['ebs']))
        uns_for_off[off] = any(uns for (_, _, _, _, uns) in d['loads'])
        for (la_name, ld_idx, v, eb, uns) in d['loads']:
            load_rewrite[ld_idx] = v
            # mark the producing IRLoadAddr (the most recent one before ld_idx
            # that defines la_name) as dead
            for j in range(ld_idx - 1, s - 1, -1):
                ij = instrs[j]
                if type(ij).__name__ == 'IRLoadAddr' and ij.dest.name == la_name:
                    dead_loadaddr.add(j); break
        for (la_name, st_idx, sval, eb) in d['stores']:
            store_rewrite[st_idx] = sval
            for j in range(st_idx - 1, s - 1, -1):
                ij = instrs[j]
                if type(ij).__name__ == 'IRLoadAddr' and ij.dest.name == la_name:
                    dead_loadaddr.add(j); break

    # Preheader: load each promoted slot's initial value into its vreg.
    preheader = []
    for off, vreg in off_for_vreg.items():
        pa = _new_temp()
        la = IRLoadAddr(pa, off); la._lr = True
        ld = IRLoad(vreg, pa, Const(0), eb_for_off[off], uns_for_off[off]); ld._lr = True
        preheader.append(la)
        preheader.append(ld)

    # Rebuild the loop region with loads/stores turned into register moves.
    new_region = []
    for k in region:
        if k in dead_loadaddr:
            continue
        ins = instrs[k]
        if k in load_rewrite:
            off = addr_off[ins.base.name]
            new_region.append(IRAssign(load_rewrite[k], off_for_vreg[off]))
        elif k in store_rewrite:
            off = addr_off[ins.base.name]
            new_region.append(IRAssign(off_for_vreg[off], store_rewrite[k]))
        else:
            new_region.append(ins)

    new = instrs[:s] + preheader + new_region + instrs[e + 1:]

    # Write-back: at every loop EXIT (a jump target inside the loop whose label
    # is defined outside the loop), store each vreg back to its slot once.
    region_labels = {instrs[k].name for k in region
                     if type(instrs[k]).__name__ == 'IRLabel'}
    exit_labels = set()
    for k in region:
        for t in _jump_targets(instrs[k]):
            if t not in region_labels:
                exit_labels.add(t)

    # Build the write-back block (same for each exit).
    def writeback_block():
        blk = []
        for off, vreg in off_for_vreg.items():
            wa = _new_temp()
            la = IRLoadAddr(wa, off); la._lr = True
            st = IRStore(wa, Const(0), vreg, eb_for_off[off]); st._lr = True
            blk.append(la)
            blk.append(st)
        return blk

    if exit_labels:
        out = []
        for ins in new:
            out.append(ins)
            if type(ins).__name__ == 'IRLabel' and ins.name in exit_labels:
                out.extend(writeback_block())
        new = out

    return new


def promote_loop_counters(instrs):
    """Promote loop-carried induction variables to registers, innermost first.
    A given stack slot is promoted by at most one loop (its innermost qualifying
    one); promoted_offsets enforces that across the whole fixpoint."""
    from licm import _func_bounds, _enclosing_func
    promoted_offsets = set()
    for _ in range(100):
        bounds = _func_bounds(instrs)
        progressed = False
        for (s, e) in _find_loops(instrs):
            fa, fb = _enclosing_func(bounds, s, e, len(instrs))
            # Stable per-function key for promoted_offsets: the function's name,
            # not fa (a mutable index). instrs[fa] is the IRFuncBegin for this
            # slice; fall back to fa only if the marker is somehow absent.
            fbegin = instrs[fa] if 0 <= fa < len(instrs) else None
            fkey = getattr(fbegin, 'name', fa) if type(fbegin).__name__ == 'IRFuncBegin' else fa
            res = _promote_one(instrs, s, e, promoted_offsets, fa, fb, fkey)
            if res is not instrs:
                instrs = res
                progressed = True
                break
        if not progressed:
            break
    return instrs

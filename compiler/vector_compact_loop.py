"""
vector_compact_loop.py -- Compact Vector Loop Generation (R4.2.5).

R4.1/R4.2 realise a vectorized kernel by FULLY UNROLLING the chunks: `chunks`
copies of `packed load -> vector op -> (store|accumulate)`, each addressed by a
CONSTANT byte offset. Dynamic operations collapse (-94%), but static size grows
with the trip count -- exactly the situation R2.5 left behind and R2.8 fixed for
software pipelining. This module is the vector analogue of R2.8.

THE COMPACT FORM
    for (i = 0; i < chunks*lanes; i += lanes)
        <packed body addressed by the REGISTER offset i*elem_bytes>
    <original scalar loop, resuming at i = chunks*lanes, for the remainder>

Static size becomes O(1) in the trip count instead of O(chunks).

THREE DESIGN DECISIONS, each made to reuse existing machinery rather than add any:

1. **The loop reuses the kernel's OWN induction variable slot.** The scalar loop
   already counts elements in `desc.primary_iv`; the vector loop advances that
   same slot by `lanes` per chunk and exits with it holding exactly
   `chunks*lanes`. The scalar remainder loop that follows therefore needs NO
   modification at all -- it simply resumes from where the vector loop stopped.
   (The unrolled form had to rewrite the IV init store to `chunks*lanes`; the
   compact form leaves it at 0 and lets the loop do the counting.)

2. **The loop is emitted in the front end's canonical counted-loop shape**
   (cond / body / incr / goto cond / end, with a MEMORY-slot IV). This is not
   cosmetic: M1 induction-variable analysis is memory-slot based (the lesson R2.7
   learned the hard way), so a fresh register counter would be invisible to it.
   Emitting the canonical shape keeps the loop recognisable to LoopInfo, the
   dependence graph, the scheduler, R3.1 SWP and R3.2 superblock formation, so
   none of them regress.

3. **Loop-carried values stay in MEMORY, never in a register across the back
   edge.** The dot/reduction accumulator is loaded at the top of the body and
   stored at the bottom, mirroring the scalar loop exactly. R2.8 had to invent the
   `_codegen_keeps_alive` invariant precisely because the IR differential cannot
   see register allocation; keeping the recurrence in its slot sidesteps that
   whole class of bug, and the scalar optimizer's own register promotion (R2.6)
   can still hoist it afterwards if it is profitable.

WHEN THE COMPACT FORM IS USED is decided by measurement, not by assumption: both
candidates are compiled through the real backend and the one with fewer bundles
wins; at an equal count the unrolled form is kept, since the loop is strictly
slower for the same size. With few chunks the unrolled form genuinely wins --
the bundler packs independent chunks into wide bundles, and a loop would add a
compare, a branch and an IV update per chunk. See R4_2_5_DELIVERY.md for the
measured crossover.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import (Const, Temp, IRLoad, IRStore, IRLoadAddr, IRLabel, IRJump,
                IRCondJump, IRBinOp, IRAssign)
from vector_lowering import _fresh


def _cname(x):
    return type(x).__name__

_lbl_n = [0]


def reset_labels():
    """Reset the label counter so repeated runs emit identical names."""
    _lbl_n[0] = 0


def _fresh_label(kind):
    _lbl_n[0] += 1
    return f"vcl_{_lbl_n[0]}_{kind}"


# ── packed accesses at a REGISTER offset (the unrolled form uses constants) ─────

def packed_load_at(dest, slot, off_temp, lanes, eb, signed):
    """A packed 64-bit load of `lanes` contiguous elements at a register offset."""
    base = _fresh('_vcb')
    la = IRLoadAddr(base, slot)
    ld = IRLoad(dest, base, off_temp, elem_bytes=8, unsigned=(not signed))
    ld._vec_pack = (lanes, eb)
    return [la, ld]


def aligned_pair_at(slot, off_temp, shift_bytes, lanes, eb):
    """(instrs, w0, w1) -- the two ALIGNED words a window at `shift_bytes` needs.

    Shared across taps (R6.3 Phase 2): every tap that starts in the same 64-bit
    word reads exactly these two words, so they are loaded once per word per
    chunk instead of once per tap."""
    base = _fresh('_vwb'); aoff2 = _fresh('_vwc')
    w0 = _fresh('_vw0'); w1 = _fresh('_vw1')
    out = [IRLoadAddr(base, slot)]
    # R6.3.2 Phase 3: emit the final affine address directly. When the pair is
    # keyed on a tap that already starts at the word boundary the correction is
    # zero, and `off - 0` is a wasted instruction in the steady-state body.
    if shift_bytes:
        aoff = _fresh('_vwa')
        out.append(IRBinOp(aoff, '-', off_temp, Const(shift_bytes)))
    else:
        aoff = off_temp
    out.append(IRBinOp(aoff2, '+', aoff, Const(8)))
    ld0 = IRLoad(w0, base, aoff, elem_bytes=8, unsigned=True)
    ld1 = IRLoad(w1, base, aoff2, elem_bytes=8, unsigned=True)
    ld0._vec_pack = (lanes, eb); ld1._vec_pack = (lanes, eb)
    return out + [ld0, ld1], w0, w1


def window_from_pair(dest, w0, w1, shift_bytes):
    """The funnel shift itself: dest = (w0 << 8s) | (w1 >>u (64-8s))."""
    if not shift_bytes:
        return [IRAssign(dest, w0)]          # the word IS the window
    sh = 8 * shift_bytes
    hi = _fresh('_vwh'); lo = _fresh('_vwl')
    return [IRBinOp(hi, '<<', w0, Const(sh)),
            IRBinOp(lo, '>>', w1, Const(64 - sh), unsigned=True),
            IRBinOp(dest, '|', hi, lo)]


def packed_window_load_at(dest, slot, off_temp, shift_bytes, lanes, eb, signed):
    """A packed window starting `shift_bytes` into a word, from ALIGNED loads."""
    if not shift_bytes:
        return packed_load_at(dest, slot, off_temp, lanes, eb, signed)
    sh = 8 * shift_bytes
    base = _fresh('_vwb'); aoff = _fresh('_vwa'); aoff2 = _fresh('_vwc')
    w0 = _fresh('_vw0'); w1 = _fresh('_vw1')
    hi = _fresh('_vwh'); lo = _fresh('_vwl')
    out = [IRLoadAddr(base, slot),
           IRBinOp(aoff, '-', off_temp, Const(shift_bytes)),
           IRBinOp(aoff2, '+', aoff, Const(8))]
    ld0 = IRLoad(w0, base, aoff, elem_bytes=8, unsigned=True)
    ld1 = IRLoad(w1, base, aoff2, elem_bytes=8, unsigned=True)
    ld0._vec_pack = (lanes, eb); ld1._vec_pack = (lanes, eb)
    out += [ld0, ld1,
            IRBinOp(hi, '<<', w0, Const(sh)),
            IRBinOp(lo, '>>', w1, Const(64 - sh), unsigned=True),
            IRBinOp(dest, '|', hi, lo)]
    return out


def packed_store_at(slot, off_temp, value, lanes, eb):
    """Store one packed 64-bit result back at a register offset."""
    base = _fresh('_vcs')
    la = IRLoadAddr(base, slot)
    st = IRStore(base, off_temp, value, 8)
    st._vec_pack = (lanes, eb)
    return [la, st]


def slot_load(slot, signed=True, elem_bytes=8):
    """(instrs, value_temp) -- load a scalar stack slot.

    `elem_bytes` MUST match the width every other access to this slot uses. A
    DMEM word is 64 bits and a sub-word access lands in a specific part of it,
    so an 8-byte store followed by a 4-byte load of the same slot reads the
    wrong half and yields 0 -- see `slot_width`."""
    base = _fresh('_vcl')
    val = _fresh('_vcv')
    return [IRLoadAddr(base, slot),
            IRLoad(val, base, Const(0), elem_bytes=elem_bytes,
                   unsigned=(not signed))], val


def slot_store(slot, value, elem_bytes=8):
    base = _fresh('_vct')
    return [IRLoadAddr(base, slot), IRStore(base, Const(0), value, elem_bytes)]


def slot_width(instrs, lo, hi, slot):
    """The element width the surrounding SCALAR code uses for `slot`, or None if
    it is unused or the accesses disagree.

    R6.2C / defect D2. `build_compact_chunk_loop` REUSES the scalar loop's own
    induction-variable slot -- that is deliberate, and is what lets a scalar
    remainder loop resume over the same slot with no fix-up. But the slot
    belongs to an `int`, which the scalar code reads and writes 4 bytes at a
    time, while this module used to access it 8 bytes at a time. Both are
    correct in isolation; together they are a miscompile, because APARA places a
    4-byte access in one half of the 64-bit DMEM word and an 8-byte access
    across the whole of it.

    The mismatch was invisible to the IR differential oracle, which models
    memory as a flat byte dict with no word structure, and it only became
    OBSERVABLE when a client re-read the slot through `clone_offset` (GEMM)
    rather than using the loop's own value temp."""
    addr = {}
    for k in range(lo, hi + 1):
        ins = instrs[k]
        if _cname(ins) == 'IRLoadAddr':
            addr[ins.dest.name] = ins.fp_offset
    widths = set()
    for k in range(lo, hi + 1):
        ins = instrs[k]
        if _cname(ins) not in ('IRLoad', 'IRStore'):
            continue
        b = getattr(ins, 'base', None)
        if isinstance(b, Temp) and addr.get(b.name) == slot:
            widths.add(ins.elem_bytes)
    if len(widths) != 1:
        return None
    return widths.pop()


# ── the compact chunk loop ──────────────────────────────────────────────────────

def build_compact_chunk_loop(iv_slot, eb, lanes, chunks, emit_body,
                            iv_bytes=8):
    """Emit `for (i = iv_slot; i < chunks*lanes; i += lanes) emit_body(i*eb)`.

    `emit_body(off_temp)` returns the instruction list for ONE chunk, addressing
    packed arrays at the register byte-offset `off_temp`. The IV slot is assumed
    to hold 0 on entry (the kernel's own init store) and holds exactly
    `chunks*lanes` on exit, so a scalar remainder loop over the same slot resumes
    correctly with no fix-up.

    Returns (instrs, per_iter_ops) -- per_iter_ops is the exact number of
    instructions ONE chunk iteration executes (cond + body + incr), which the
    client's dynamic model needs: unlike the unrolled form, a compact loop pays a
    compare, a branch and an IV update on every chunk."""
    cond_l = _fresh_label('cond')
    body_l = _fresh_label('body')
    incr_l = _fresh_label('incr')
    end_l = _fresh_label('end')
    out = []

    # cond:  if (i < chunks*lanes) goto body else end
    out.append(IRLabel(cond_l))
    ld, i_cur = slot_load(iv_slot, elem_bytes=iv_bytes)
    out += ld
    out.append(IRCondJump(i_cur, '<', Const(chunks * lanes), body_l, end_l))

    # body:  off = i * eb ; <packed work>
    out.append(IRLabel(body_l))
    ld, i_body = slot_load(iv_slot, elem_bytes=iv_bytes)
    out += ld
    if eb == 1:
        off = i_body                            # byte offset == element index
    else:
        off = _fresh('_vco')
        out.append(IRBinOp(off, '*', i_body, Const(eb)))
    out += emit_body(off)

    # incr:  i += lanes ; goto cond
    out.append(IRLabel(incr_l))
    ld, i_inc = slot_load(iv_slot, elem_bytes=iv_bytes)
    out += ld
    nxt = _fresh('_vcn')
    out.append(IRBinOp(nxt, '+', i_inc, Const(lanes)))
    out += slot_store(iv_slot, nxt, elem_bytes=iv_bytes)
    out.append(IRJump(cond_l))

    out.append(IRLabel(end_l))
    # executed instructions per full iteration = everything but the 4 labels
    per_iter = len(out) - 4
    return out, per_iter


def realisation_of(instrs):
    """'compact' if `instrs` contains a compact vector loop, else 'unrolled'.

    Reads the emitted IR rather than threading a field through the pipeline: the
    `vcl_` label prefix is unique to this module, so the realisation is
    recoverable from the output itself and `vector_pipeline.py` needs no change.
    """
    from ir import IRLabel
    from ir_utils import dest_names
    base = ('compact'
            if any(isinstance(i, IRLabel) and i.name.startswith('vcl_')
                   for i in instrs)
            else 'unrolled')
    peeled = any(n.startswith('_vrp') for i in instrs
                 for n in (dest_names(i) or ()))
    return base + ('+peeled' if peeled else '')


# ── candidate selection: measure, do not assume ─────────────────────────────────

# A challenger must beat the incumbent by at least this FRACTION of its bundle
# count to be taken. Rationale: every alternative realisation this module offers
# (a compact loop, a peeled remainder) trades DYNAMIC operations for static size.
# Measured example -- `vector add vi8`, 4 chunks: compact saves 1 bundle of 31
# (-3%) but costs +47 executed operations (+168%). That is a bad trade; the
# 8-chunk kernels, which save 13 of 43 (-30%), are a good one. A margin separates
# them without hardcoding a chunk threshold that would not survive a different
# lane count or issue width. Tunable via APARA_VECTOR_COMPACT_MARGIN.
_DEFAULT_MARGIN = 0.10

# MEASURED, AND IT CORRECTED A WRONG HYPOTHESIS. Remainder peeling was expected
# to be a two-axis win (delete the tail loop, so both size and speed improve).
# Post-optimizer measurement says otherwise: at remainder 4 the peeled tail is 4
# copies of the body (~29 instructions) where the tail LOOP was ~10 plus one body,
# so peeling is dynamically faster but statically LARGER -- the mirror image of
# compaction, not an exception to it. `add vi8` N=20: unrolled 21 bundles vs
# unrolled+peeled 29. Every challenger therefore clears the same margin; peeling
# wins only where it genuinely shrinks the code (`reduction vi16` N=30: 33 -> 27).

def _margin():
    import os
    try:
        return float(os.environ.get('APARA_VECTOR_COMPACT_MARGIN',
                                    _DEFAULT_MARGIN))
    except ValueError:
        return _DEFAULT_MARGIN


def choose_smaller(candidates, global_base):
    """Compile each (name, slice) candidate and return (best_slice, name,
    {name: bundles}). A candidate that fails to compile or spills is discarded.

    THE FIRST CANDIDATE IS THE INCUMBENT, and callers pass the UNROLLED form
    first on purpose: it is dynamically the fastest realisation, so every other
    form must EARN the switch by a MEANINGFUL static win -- at least
    `_DEFAULT_MARGIN` of the incumbent's bundle count -- not merely tie or shave
    one bundle. Among challengers that clear the margin, the smallest wins.

    `APARA_VECTOR_REALISATION=<name>` forces one form, for A/B measurement.

    R4.2.6: the measurement is the POST-OPTIMIZER size (`vector_size_probe`) --
    each candidate is run through production's tier-1 scalar optimizer and
    superblock scheduling before bundling, because those passes favour
    straight-line code and used to invert the ranking after lowering had already
    chosen. `APARA_VECTOR_FAST_PROBE=1` reverts to the cheap pre-optimizer probe.

    The pipeline still applies its own spill and differential gates to whatever is
    returned; this is a SIZE decision, not a correctness one."""
    import os
    from vector_size_probe import probe_bundles
    forced = os.environ.get('APARA_VECTOR_REALISATION')
    if forced:
        for cand in candidates:
            if cand[0] == forced and cand[1] is not None:
                return cand[1], cand[0], {cand[0]: None}

    scores = {}
    measured = []
    for cand in candidates:
        # (name, slice) or (name, slice, needs_margin). `needs_margin` is False
        # for a challenger that does NOT trade dynamic operations for size -- see
        # the note below -- and such a candidate wins on any strict improvement.
        name, slc = cand[0], cand[1]
        needs_margin = cand[2] if len(cand) > 2 else True
        if slc is None:
            continue
        b, spilled = probe_bundles(slc, global_base)
        scores[name] = (None if b is None or spilled else b)
        if b is not None and not spilled:
            measured.append((name, slc, b, needs_margin))
    if not measured:
        # R4.6.1: every candidate spilled under the POST-OPTIMIZER probe. That
        # probe models tier-1 + superblock, which raises register pressure; the
        # pipeline's own commit gate uses the PLAIN backend, so discarding here
        # threw away kernels the pipeline would have accepted (measured on
        # multi-operand 2-D stencils: 6 packed loads spill after mem2reg/LICM but
        # compile clean without them). Fall back to the plain measurement and let
        # the pipeline's spill gate make the real decision.
        from vector_pipeline import _bundles
        for cand in candidates:
            name, slc = cand[0], cand[1]
            if slc is None:
                continue
            b, spilled = _bundles(slc, global_base)
            if b is not None and not spilled:
                measured.append((name, slc, b,
                                 cand[2] if len(cand) > 2 else True))
                scores[name] = b
    if not measured:
        return None, None, scores

    inc_name, inc_slice, inc_b, _ = measured[0]     # the incumbent (unrolled)
    threshold = inc_b * (1.0 - _margin())
    best, best_name, best_b = inc_slice, inc_name, inc_b
    for name, slc, b, needs_margin in measured[1:]:
        limit = threshold if needs_margin else inc_b
        if b <= limit and b < best_b:
            best, best_name, best_b = slc, name, b
    return best, best_name, scores

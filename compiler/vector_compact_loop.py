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
                IRCondJump, IRBinOp)
from vector_lowering import _fresh

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


def packed_store_at(slot, off_temp, value, lanes, eb):
    """Store one packed 64-bit result back at a register offset."""
    base = _fresh('_vcs')
    la = IRLoadAddr(base, slot)
    st = IRStore(base, off_temp, value, 8)
    st._vec_pack = (lanes, eb)
    return [la, st]


def slot_load(slot, signed=True):
    """(instrs, value_temp) -- load a scalar stack slot."""
    base = _fresh('_vcl')
    val = _fresh('_vcv')
    return [IRLoadAddr(base, slot),
            IRLoad(val, base, Const(0), elem_bytes=8, unsigned=(not signed))], val


def slot_store(slot, value):
    base = _fresh('_vct')
    return [IRLoadAddr(base, slot), IRStore(base, Const(0), value, 8)]


# ── the compact chunk loop ──────────────────────────────────────────────────────

def build_compact_chunk_loop(iv_slot, eb, lanes, chunks, emit_body):
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
    ld, i_cur = slot_load(iv_slot)
    out += ld
    out.append(IRCondJump(i_cur, '<', Const(chunks * lanes), body_l, end_l))

    # body:  off = i * eb ; <packed work>
    out.append(IRLabel(body_l))
    ld, i_body = slot_load(iv_slot)
    out += ld
    if eb == 1:
        off = i_body                            # byte offset == element index
    else:
        off = _fresh('_vco')
        out.append(IRBinOp(off, '*', i_body, Const(eb)))
    out += emit_body(off)

    # incr:  i += lanes ; goto cond
    out.append(IRLabel(incr_l))
    ld, i_inc = slot_load(iv_slot)
    out += ld
    nxt = _fresh('_vcn')
    out.append(IRBinOp(nxt, '+', i_inc, Const(lanes)))
    out += slot_store(iv_slot, nxt)
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
    return ('compact'
            if any(isinstance(i, IRLabel) and i.name.startswith('vcl_')
                   for i in instrs)
            else 'unrolled')


# ── candidate selection: measure, do not assume ─────────────────────────────────

def choose_smaller(candidates, global_base):
    """Compile each (name, slice) candidate through the REAL backend and return
    (best_slice, name, {name: bundles}). A candidate that fails to compile or
    spills is discarded.

    TIES GO TO THE FIRST CANDIDATE, and callers pass the UNROLLED form first on
    purpose. At an equal bundle count the compact loop occupies the same IMEM but
    executes strictly more instructions (a compare, a branch and an IV update per
    chunk), so it must EARN the switch with a strict size win rather than take it
    on a tie.

    `APARA_VECTOR_REALISATION=compact|unrolled` forces one form, for A/B
    measurement.

    Reuses `vector_pipeline._bundles` -- the same backend probe the pipeline's own
    compile gate uses -- rather than duplicating it. The pipeline still applies its
    own spill and differential gates to whatever is returned; this is a SIZE
    decision, not a correctness one."""
    import os
    from vector_pipeline import _bundles
    forced = os.environ.get('APARA_VECTOR_REALISATION')
    if forced:
        for name, slc in candidates:
            if name == forced and slc is not None:
                return slc, name, {name: None}
    scores = {}
    best = best_name = None
    best_b = None
    for name, slc in candidates:
        if slc is None:
            continue
        b, spilled = _bundles(slc, global_base)
        scores[name] = (None if b is None or spilled else b)
        if b is None or spilled:
            continue
        if best_b is None or b < best_b:
            best, best_name, best_b = slc, name, b
    return best, best_name, scores

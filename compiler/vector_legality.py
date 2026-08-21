"""
vector_legality.py -- Vector Legality Analysis (Milestone R4.0).

ANALYSIS ONLY. Given a recognised kernel candidate (kernel_detector.py), decides
whether it is LEGAL to vectorize on the APARA ISA -- reusing the existing
CFG / LoopInfo / Dominators / DependenceGraph / MemoryDisambiguator and the vector
capability layer. It emits no code and makes no profitability decision.

A kernel is legal only when ALL hold, else it is rejected with a specific reason:

  * the kernel kind is recognised and its operation is supported for the element
    type by the capability layer (reliable -- excludes the known-broken ops);
  * the loop is an innermost counted loop with a single exit and no calls
    (unsupported control flow is rejected);
  * the vectorizable memory accesses are affine / unit-stride in the IV
    (unsupported memory layouts are rejected);
  * there is no unproven aliasing between the store and the read arrays -- the
    R2.2 disambiguator must prove the loop-carried memory edges are only the
    (clean) reduction recurrence (unknown aliasing is rejected);
  * the element data type is supported (unsupported types rejected).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import Temp
from ir_utils import func_slices
from loopopt.discovery import discover_function
from loopopt.analysis_iv import annotate_induction_vars, TripCount
from loopopt.analysis_mem import annotate_memory_effects
from loopopt.depgraph import (DependenceGraph, MEM_RAW, MEM_WAR, MEM_WAW)
from loopopt.depgraph_disambig import MemoryDisambiguator
from vector_capability import VectorCapability
import vector_capability_db as _vdb
from kernel_detector import _detect_loop, KernelCandidate

_cap = VectorCapability()

# the reduction / operation each kernel kind needs from the ISA
_KIND_OP = {
    'dot-product':   'dot',
    'matmul':        'dot',
    'sum-reduction': 'reduce_sum',
    'vector-add':    'add',
    'saxpy':         'mul',            # needs $v mul + add
    'convolution':   'dot',
}
_UNSUPPORTED_BODY = frozenset({'IRCall', 'IRIndirectCall', 'IRVecArith',
                               'IRVecDot', 'IRVecReduce'})


class KernelLegality:
    __slots__ = ('kernel', 'legal', 'reason', 'operation', 'capability',
                 'lanes', 'remainder')

    def __init__(self, kernel):
        self.kernel = kernel
        self.legal = False
        self.reason = 'not-analyzed'
        self.operation = None
        self.capability = None
        self.lanes = 0
        self.remainder = None

    def __repr__(self):
        if self.legal:
            return (f"Legal({self.kernel.kind} {self.kernel.vtype} via "
                    f"{self.capability.mnemonic} x{self.lanes})")
        return f"Illegal({self.kernel.kind or '-'}: {self.reason})"


def _body_indices(desc):
    idx = []
    for b in desc.body_blocks:
        blk = desc.cfg.blocks[b]
        idx.extend(range(blk.lo, blk.hi + 1))
    return idx


def analyze_legality_loop(desc, instrs, graph):
    kernel = _detect_loop(desc, instrs)
    res = KernelLegality(kernel)

    if kernel.kind is None:
        res.reason = 'no-recognised-kernel'
        return res
    # ── control flow ──────────────────────────────────────────────────────────
    if not desc.is_innermost and kernel.kind not in ('matmul', 'convolution'):
        res.reason = 'not-innermost'
        return res
    if len(desc.exit_blocks) != 1 or len(desc.exiting_blocks) != 1:
        res.reason = 'multi-exit-control-flow'
        return res
    for i in _body_indices(desc):
        if type(instrs[i]).__name__ in _UNSUPPORTED_BODY:
            res.reason = f'unsupported-body-op:{type(instrs[i]).__name__}'
            return res
    if desc.trip_count is None or desc.trip_count.kind == TripCount.UNKNOWN:
        res.reason = 'trip-count-unknown'
        return res

    # ── element type ──────────────────────────────────────────────────────────
    if kernel.vtype is None:
        res.reason = 'unsupported-or-unanalyzable-element-type'
        return res
    if not _cap.is_reliable_type(kernel.vtype):
        res.reason = f'element-type-broken:{kernel.vtype}'
        return res

    # ── ISA capability for the kernel's operation ─────────────────────────────
    op = _KIND_OP.get(kernel.kind)
    res.operation = op
    cap = _cap.can(op, kernel.vtype, want_accumulate=(op == 'dot'))
    if not cap.ok:
        res.reason = f'isa-unsupported:{cap.reason}'
        return res
    res.capability = cap
    res.lanes = cap.lanes

    # ── memory layout: the vectorizable accesses must be affine/unit-stride ────
    if kernel.kind in ('dot-product', 'sum-reduction') and kernel.affine_loads < 1:
        res.reason = 'non-affine-memory-access'
        return res
    if kernel.kind in ('vector-add', 'saxpy') and \
            (kernel.affine_stores < 1 or kernel.affine_loads < 1):
        res.reason = 'non-affine-memory-access'
        return res
    if kernel.kind in ('matmul', 'convolution') and kernel.vtype is None:
        res.reason = 'non-affine-2d-access'
        return res

    # ── aliasing: the only loop-carried memory edge may be the reduction slot ──
    if not _aliasing_ok(desc, graph, kernel):
        res.reason = 'unproven-aliasing'
        return res

    # ── ISA alignment of every packed access (R6.2C / defect D1) ──────────────
    aligned, bad = _packed_accesses_aligned(desc, instrs)
    if not aligned:
        off = getattr(bad, 'const_off', None)
        res.reason = ('unaligned-packed-access'
                      + (f':byte-offset-{off}' if off is not None else ''))
        return res

    # ── remainder (scalar tail) ───────────────────────────────────────────────
    if kernel.trip is not None:
        res.remainder = kernel.trip % res.lanes

    res.legal = True
    res.reason = 'ok'
    return res


def _aliasing_ok(desc, graph, kernel):
    """The only loop-carried memory dependences the R2.2 disambiguator may leave
    are on the CLEAN SCALAR SLOTS -- the induction variable(s) and the reduction
    accumulator -- which register promotion removes and which are not array
    aliasing. Any carried memory edge on an ARRAY access means the store may alias
    a later load, so lane-parallel execution could observe a different order:
    reject. (R2.2 already prunes false array edges, so a clean kernel has none.)"""
    opset = set(_body_indices(desc))
    clean_slots = set(desc.basic_ivs.keys())
    # R14.1a: EVERY accumulator slot the detector proved to be a clean scalar
    # recurrence, not just the first. The rule itself is unchanged -- a carried
    # dependence on a clean scalar stack slot is a reduction, not array
    # aliasing -- it simply now sees all of them. Each entry in
    # `kernel.reductions` was established structurally (single store site, the
    # stored value is `load(slot) + V`), which is precisely the property this
    # exemption has always relied on.
    #
    # ORDER MATTERS: this is deliberately widened only AFTER the lowering can
    # emit N reduction streams. Widening it first makes legality accept loops
    # whose extra outputs the lowering would silently drop.
    for _r in (kernel.reductions or []):
        if _r.slot is not None:
            clean_slots.add(_r.slot)
    if kernel.reduction_slot is not None:
        clean_slots.add(kernel.reduction_slot)
    for e in graph.edges:
        if not e.carried or e.src not in opset or e.dst not in opset:
            continue
        if e.kind in (MEM_RAW, MEM_WAR, MEM_WAW):
            res = e.resource
            # scalar stack-slot recurrence (IV / accumulator) -> fine
            if isinstance(res, tuple) and len(res) > 1 and res[0] == 'stack' \
                    and res[1] in clean_slots:
                continue
            # R4.4 additive DISPROOF (never adds an edge; only excuses one R2.2
            # could not analyse). R2.2's SIV rule handles a bare `IV*const`
            # offset, so AXPY passes, but a 2-D-indexed offset like C[i*N+j] is a
            # SUM and falls back to a generic ('computed',) may-alias. The facts
            # needed to disprove it exist in vector_affine (R4.2.8):
            #   * different stack slots  -> distinct objects, cannot alias;
            #   * same slot AND the SAME offset temp, both CONTIGUOUS -> the two
            #     accesses share one address, so they collide only within a single
            #     iteration; distinct iterations touch distinct elements.
            # Anything else still rejects.
            if _lane_disjoint(desc, graph, e):
                continue
            return False                            # array carried dependence
    return True


def _access_slot(instrs, def_map, ins):
    base = getattr(ins, 'base', None)
    if not isinstance(base, Temp):
        return None
    d = def_map.get(base.name)
    if d is None or type(instrs[d]).__name__ != 'IRLoadAddr':
        return None
    return instrs[d].fp_offset


def _lane_disjoint(desc, graph, e):
    """True if the carried edge `e` cannot order two DIFFERENT iterations."""
    try:
        from vector_affine import LoopAffineContext, classify_access, CONTIGUOUS
        from analysis import DefUse
        instrs = desc.cfg.instrs
        a, b = instrs[e.src], instrs[e.dst]
        if type(a).__name__ not in ('IRLoad', 'IRStore') or \
           type(b).__name__ not in ('IRLoad', 'IRStore'):
            return False
        ctx = LoopAffineContext(instrs, desc)
        if classify_access(a, ctx).kind != CONTIGUOUS or \
           classify_access(b, ctx).kind != CONTIGUOUS:
            return False
        lo, hi = desc.func_slice
        dm = DefUse(instrs, lo, hi).single_defs()
        sa, sb = _access_slot(instrs, dm, a), _access_slot(instrs, dm, b)
        if sa is None or sb is None:
            return False
        if sa != sb:
            return True                             # distinct objects
        oa, ob = getattr(a, 'offset', None), getattr(b, 'offset', None)
        return (isinstance(oa, Temp) and isinstance(ob, Temp)
                and oa.name == ob.name)             # identical address
    except Exception:
        return False


# ── R6.2C: ISA alignment of the addresses the packed lowering will emit ───────
#
# The APARA datapath reads exactly ONE aligned DMEM word per access
# (`McodeExecute.cpp`: `Read_Data_Dword(base & 0xfffffff8, ...)`), and
# `AddrIsAligned` requires (addr & 7) == 0 for an 8-byte transfer. A wide access
# spanning two words is not expressible, so an unaligned one is an ILLEGAL
# instruction -- the simulator prints an error and then reads the CONTAINING
# word, silently producing wrong data.
#
# Nothing in the legality layer used to say so, which is R6.2A defect D1: a
# shifted stencil window `in[i+1]` lowers to a packed load at `base + 1`, and two
# of every three convolution loads were unaligned by construction.
#
# The rule is a property of the ACCESS, not of any kernel: the lowering
# substitutes the induction variable with a multiple of the packed word, so
# alignment reduces to the invariant part of the offset (vector_affine's
# `word_aligned`). It is applied to every contiguous access of every client.

_WORD_BYTES = _vdb.WORD_BITS // 8


def _packed_accesses_aligned(desc, instrs):
    """(ok, offending access). Every access the packed lowering turns into a
    wide memory operation must be PROVABLY word-aligned; anything unproven is
    rejected, because an unproven address is exactly the one that must not be
    lowered to a wide access."""
    if os.environ.get('APARA_R62C_NO_ALIGN_GATE'):
        # DANGEROUS, measurement only. Setting this re-enables generation of
        # packed accesses at addresses the ISA cannot perform -- the simulator
        # reports `Unaligned address in load` and silently returns the
        # CONTAINING word, so results are wrong. It exists solely to reproduce
        # the before/after evidence in R6_2C_CORRECTNESS_FIXES.md. Never set it
        # for code that will be run.
        return True, None
    try:
        from vector_affine import (LoopAffineContext, classify_access,
                                   word_aligned, CONTIGUOUS)
        ctx = LoopAffineContext(instrs, desc)
        for b in sorted(desc.body_blocks):
            blk = desc.cfg.blocks[b]
            for k in range(blk.lo, blk.hi + 1):
                ins = instrs[k]
                if type(ins).__name__ not in ('IRLoad', 'IRStore'):
                    continue
                acc = classify_access(ins, ctx)
                if acc.kind != CONTIGUOUS:
                    continue                     # scalar or already rejected
                if word_aligned(acc, _WORD_BYTES):
                    continue
                reconstructable = (type(ins).__name__ == 'IRLoad'
                                   and acc.const_off is not None
                                   and not (acc.sym_div
                                            and acc.sym_div % _WORD_BYTES))
                if not reconstructable:
                    return False, acc
        return True, None
    except Exception:
        return False, None                       # unprovable => not legal


def analyze_legality_function(instrs, lo, hi):
    descs = discover_function(instrs, lo, hi)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    disamb = MemoryDisambiguator(instrs, lo, hi, descs)
    graph = DependenceGraph(instrs, lo, hi, disambiguator=disamb)
    out = []
    for d in descs:
        if not d.is_innermost:
            continue
        out.append(analyze_legality_loop(d, instrs, graph))
    return out


def analyze_legality_module(instrs):
    """Legality analysis for every innermost loop in a module. Analysis only."""
    out = []
    for (lo, hi) in func_slices(instrs):
        out.extend(analyze_legality_function(instrs, lo, hi))
    return out

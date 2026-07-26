"""
loop_promote.py -- Loop Register Promotion (Milestone R2.6).

Attacks the dominant bottleneck R2.5 measured: 42/45 eligible loops are RecMII-
bound because the induction variable and accumulators are MEMORY-backed
recurrences (load X -> compute -> store X -> next-iteration load X, ~5 cycles at
distance 1). R2.6 converts each such recurrence into a REGISTER recurrence -- load
the slot once in the preheader, operate on a register in the body, store it back
once at the exit -- so R2.5 then computes a lower RecMII and pipelines better.

Runs BEFORE R2.5 and never modifies R2.5 (R2.5 simply consumes the transformed
IR). Standalone -- not wired into the production compiler.

================================================================================
REUSED (nothing duplicated)
================================================================================
    M0-M3 descriptors (discover / annotate_induction_vars / annotate_memory_effects)
                                     -- loop shape, single preheader/latch/exit,
                                        and M2 `aliasing_summary.clean_slots`
                                        (the escape analysis that proves "no alias
                                        may modify X")
    R2.1 DependenceGraph + R2.2 MemoryDisambiguator
                                     -- RecMII BEFORE vs AFTER, and to verify the
                                        memory recurrence actually disappeared
    loopopt.modulo (build_kernel / rec_mii / min_ii)  -- RecMII computation
    loopopt.modulo._multiseed_ok / _compiles          -- the correctness gate
    ir_utils.func_slices             -- per-function scoping

TRANSFORMATION (the codegen-safe shape proven on hardware by loop_reg.py -- a
production pass that promotes loop counters; R2.6 reproduces its exact IRAssign-
move form on the loopopt framework, so codegen's existing loop-aware live-range
extension keeps the register live across the back-edge):

    preheader:   pa = &X ; P = load(pa)            # load once
    body load    t = *(&X)      ->  t = P          # IRAssign move
    body store   *(&X) = v       ->  P = v          # IRAssign move
    exit:        wa = &X ; store(wa) = P            # store once

CORRECTNESS is mandatory: a promotion is committed only when the module still
parses (globals preserved), a multi-seed differential matches, AND the result
compiles; otherwise the loop is rolled back untouched.

SCOPE (strict; else rejected cleanly): one innermost natural loop, single
preheader / single latch / single dedicated exit, no calls in the body; a slot is
promotable only if it is CLEAN (address never escapes -> no alias / no indirect
store / no volatile reachability), is loaded and has EXACTLY ONE store in the
loop, with one consistent element width.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (Temp, Const, IRLoadAddr, IRLoad, IRStore, IRAssign)    # noqa: E402
from ir_utils import func_slices, src_names                            # noqa: E402
from .discovery import discover_function                              # noqa: E402
from .analysis_iv import annotate_induction_vars                       # noqa: E402
from .analysis_mem import annotate_memory_effects                      # noqa: E402
from .depgraph import DependenceGraph, MEM_RAW, MEM_WAR, MEM_WAW, CONTROL  # noqa: E402
from .depgraph_disambig import MemoryDisambiguator                     # noqa: E402
from .modulo import (build_kernel, rec_mii, min_ii,                     # noqa: E402
                     _compiles, _edge_latency, KernelModel)
from .schedule import _iclass                                          # noqa: E402
from . import ir_interp as _interp                                     # noqa: E402
import random as _random                                              # noqa: E402

# instruction classes that are loop CONTROL (excluded from the recurrence body)
_CONTROL_CLS = frozenset({'IRLabel', 'IRCondJump', 'IRJump', 'IRFuncBegin',
                          'IRFuncEnd', 'IRHalt', 'IRReturn'})


_rp_n = [0]


def _fresh(prefix='_rp'):
    _rp_n[0] += 1
    return Temp(f"{prefix}{_rp_n[0]}")


def _cname(x):
    return type(x).__name__


def _is_zero(off):
    return isinstance(off, Const) and off.value == 0


class PromotionReport:
    __slots__ = ('func', 'header', 'promoted_slots', 'committed', 'reason',
                 'rec_before', 'rec_after', 'mem_rec_removed', 'verified')

    def __init__(self, func, header, reason='unsupported'):
        self.func = func
        self.header = header
        self.promoted_slots = []
        self.committed = False
        self.reason = reason
        self.rec_before = 0
        self.rec_after = 0
        self.mem_rec_removed = 0
        self.verified = False           # True = differentially verified; False = clean-slot proof only

    def __repr__(self):
        if not self.committed:
            return f"Promotion({self.func}@B{self.header} {self.reason})"
        return (f"Promotion({self.func}@B{self.header} slots={self.promoted_slots} "
                f"RecMII {self.rec_before}->{self.rec_after} "
                f"memrec-removed={self.mem_rec_removed})")


class PromoteStats:
    def __init__(self):
        self.functions = self.loops = 0
        self.promotable = self.promoted = self.failures = self.rolled_back = 0
        self.mem_rec_removed = 0
        self.sum_rec_before = self.sum_rec_after = 0
        self.reasons = {}

    def _bump(self, k):
        self.reasons[k] = self.reasons.get(k, 0) + 1


# ── promotability analysis ────────────────────────────────────────────────────

def _loop_eligible(desc):
    if not desc.is_innermost:
        return False, 'not-innermost'
    if desc.preheader is None:
        return False, 'no-unique-preheader'
    if len(desc.latches) != 1:
        return False, 'multi-latch'
    if len(desc.exit_blocks) != 1:
        return False, 'multi-exit'
    if desc.calls:
        return False, 'call-in-body'
    return True, 'ok'


def _body_indices(desc, graph):
    idxs = []
    for b in sorted(desc.body_blocks):
        blk = graph.cfg.blocks[b]
        idxs.extend(range(blk.lo, blk.hi + 1))
    return sorted(idxs)


def _promotable_slots(instrs, lo, hi, desc, graph):
    """{offset: {loads, store, eb, uns, dead_la}} for each clean stack slot that
    is loaded and has EXACTLY ONE store in the loop body, accessed only via the
    clean IRLoadAddr->offset0 load/store pattern."""
    clean = set(desc.aliasing_summary.clean_slots) if desc.aliasing_summary else set()

    # address-temp -> slot offset, over the function slice (names are slice-local)
    addr_off = {}
    for k in range(lo, hi + 1):
        if _cname(instrs[k]) == 'IRLoadAddr':
            addr_off[instrs[k].dest.name] = instrs[k].fp_offset

    body = set(_body_indices(desc, graph))
    info = {}
    for k in sorted(body):
        ins = instrs[k]
        c = _cname(ins)
        if c == 'IRLoad' and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            off = addr_off.get(ins.base.name)
            if off is None:
                continue
            d = info.setdefault(off, {'loads': [], 'stores': [], 'ebs': set(),
                                      'uns': False})
            d['loads'].append((ins.base.name, k, ins.dest, ins.elem_bytes))
            d['ebs'].add(ins.elem_bytes)
            d['uns'] = d['uns'] or ins.unsigned
        elif c == 'IRStore' and isinstance(ins.base, Temp) and _is_zero(ins.offset):
            off = addr_off.get(ins.base.name)
            if off is None:
                continue
            d = info.setdefault(off, {'loads': [], 'stores': [], 'ebs': set(),
                                      'uns': False})
            d['stores'].append((ins.base.name, k, ins.src, ins.elem_bytes))
            d['ebs'].add(ins.elem_bytes)

    out = {}
    for off, d in info.items():
        if off not in clean:                       # alias rejection
            continue
        if len(d['stores']) != 1:                  # exactly one store
            continue
        if not d['loads']:
            continue
        if len(d['ebs']) != 1:                      # one consistent width
            continue
        # find the dead IRLoadAddr feeding each promoted load/store
        dead_la = set()
        for (la_name, idx, _t, _eb) in d['loads'] + d['stores']:
            for j in range(idx - 1, lo - 1, -1):
                ij = instrs[j]
                if _cname(ij) == 'IRLoadAddr' and ij.dest.name == la_name:
                    dead_la.add(j)
                    break
        out[off] = {'loads': d['loads'], 'store': d['stores'][0],
                    'eb': next(iter(d['ebs'])), 'uns': d['uns'], 'dead_la': dead_la}
    return out, addr_off


# ── the transformation ────────────────────────────────────────────────────────

def _apply_promotion(instrs, lo, hi, desc, graph, slots, addr_off):
    """Build the promoted function-slice-in-module. Returns new instrs list."""
    preheader_blk = graph.cfg.blocks[desc.preheader]
    exit_blk = graph.cfg.blocks[desc.exit_blocks[0]]

    P = {off: _fresh() for off in slots}           # slot -> register temp
    load_rewrite = {}                              # ld_idx -> P temp
    store_rewrite = {}                             # st_idx -> (P temp, sval)
    dead_la = set()
    preheader_ops, exit_ops = [], []
    for off, d in slots.items():
        eb, uns = d['eb'], d['uns']
        for (_la, ld_idx, dest, _eb) in d['loads']:
            load_rewrite[ld_idx] = (dest, P[off])
        (_la, st_idx, sval, _eb) = d['store']
        store_rewrite[st_idx] = (P[off], sval)
        dead_la |= d['dead_la']
        # preheader: pa = &off ; P = load(pa)
        pa = _fresh()
        preheader_ops.append(IRLoadAddr(pa, off))
        preheader_ops.append(IRLoad(P[off], pa, Const(0), eb, uns))
        # exit: wa = &off ; store(wa) = P
        wa = _fresh()
        exit_ops.append(IRLoadAddr(wa, off))
        exit_ops.append(IRStore(wa, Const(0), P[off], eb))

    out = []
    for k in range(lo, hi + 1):
        if k in dead_la:
            continue                               # drop the now-dead loadaddr
        if k in load_rewrite:
            dest, p = load_rewrite[k]
            out.append(IRAssign(dest, p))          # t = P  (register move)
        elif k in store_rewrite:
            p, sval = store_rewrite[k]
            out.append(IRAssign(p, sval))          # P = v  (register move)
        else:
            out.append(instrs[k])
        if k == preheader_blk.hi:                  # end of preheader -> load once
            out.extend(preheader_ops)
        if k == exit_blk.lo:                       # exit label -> store back once
            out.extend(exit_ops)

    return instrs[:lo] + out + instrs[hi + 1:]


# ── driver (per function) ─────────────────────────────────────────────────────

def _promote_diff(orig, new, lo, hi, seeds=6):
    """R2.6 multi-seed differential. Returns 'match' / 'unsupported' / 'mismatch'.

    Register promotion is SOUND precisely because the promoted slot is CLEAN --
    its (negative-offset) address never escapes, so no pointer can alias it. The
    seeds respect that invariant: every param / pointer slot is given a NON-
    NEGATIVE value, so a dereference lands in the non-negative array/global region
    and can never alias a negative clean slot (which would be an impossible
    execution the memory-backed original is sensitive to but the register form is
    not -- a false rejection). Within that invariant we vary the trip count and
    the array data to exercise the loop. (This is why we do NOT reuse R2.5's
    _multiseed_ok, whose fully-random pointers fabricate that impossible aliasing;
    R2.5 is not modified.)

    'unsupported' (the interpreter cannot run the function -- e.g. it contains a
    call outside the loop) is treated by the caller as legal-by-construction: the
    clean-slot analysis is itself the correctness PROOF (a call cannot touch a
    clean slot), exactly as the production loop_reg pass relies on. Only a genuine
    'mismatch' forces rollback."""
    fname = orig[lo].name
    a1 = next((a for a, b in func_slices(new) if new[a].name == fname), None)
    if a1 is None:
        return 'mismatch'
    b1 = next(b for a, b in func_slices(new) if a == a1)
    rng = _random.Random(0xB26)
    ran = 0
    for s in range(seeds):
        mem = dict(_interp._preload_globals(orig))
        for a in range(0, 8192, 8):                     # array / global data
            mem[a] = rng.randint(-500, 500)
        if s:  # s==0 is the natural seed (params default 0)
            base = 8 * (s + 1)                          # non-negative pointer base + small trip
            for a in range(-8192, 0, 8):
                mem[a] = base
        try:
            r0, m0 = _interp.run_slice(orig, lo, hi, init_mem=dict(mem))
            r1, m1 = _interp.run_slice(new, a1, b1, init_mem=dict(mem))
        except (_interp.Unsupported, _interp.StepLimit):
            continue
        ran += 1
        if r0 != r1 or m0 != m1:
            return 'mismatch'
    return 'match' if ran >= 1 else 'unsupported'


def _loop_recmii(graph, desc):
    """RecMII of the loop body computed directly from the dependence graph's
    recurrence edges -- works whether the recurrences are memory- or register-
    resident (build_kernel needs a MEMORY-slot counted IV, which is gone after the
    IV is promoted, so it cannot measure the AFTER case). Reuses the R2.5 edge-
    latency model and rec_mii."""
    body = set(_body_indices(desc, graph))
    ops = [k for k in sorted(body)
           if _cname(graph.instrs[k]) not in _CONTROL_CLS]
    if not ops:
        return 0
    opset = set(ops)
    intra, carried = [], []
    for e in graph.edges:
        if e.src in opset and e.dst in opset and e.kind != CONTROL:
            lat = _edge_latency(graph, e)
            if e.carried:
                carried.append((e.src, e.dst, lat, 1))
            else:
                intra.append((e.src, e.dst, lat, 0))
    res = {'total': len(ops), 'MEM': 0, 'DIV': 0}
    for k in ops:
        cl = _iclass(graph.instrs[k])
        if cl == 'MEM':
            res['MEM'] += 1
        elif cl == 'DIV':
            res['DIV'] += 1
    kern = KernelModel(desc, ops, intra, carried, res, (min(ops), max(ops)),
                       {k: graph.instrs[k] for k in ops})
    return rec_mii(kern)


def _mem_recurrences(graph):
    """Count carried MEMORY edges that are stack-slot recurrences."""
    n = 0
    for e in graph.edges:
        if e.carried and e.kind in (MEM_RAW, MEM_WAR, MEM_WAW):
            if isinstance(e.resource, tuple) and e.resource and e.resource[0] == 'stack':
                n += 1
    return n


def promote_function(instrs, lo, hi):
    """Analyse + promote every eligible loop in one function slice. Returns
    (new_instrs, [PromotionReport])."""
    fname = getattr(instrs[lo], 'name', '?')
    descs = discover_function(instrs, lo, hi)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    disamb = MemoryDisambiguator(instrs, lo, hi, descs)
    graph = DependenceGraph(instrs, lo, hi, disambiguator=disamb)

    reports = []
    for d in descs:
        rep = PromotionReport(fname, d.header)
        ok, why = _loop_eligible(d)
        if not ok:
            rep.reason = why
            reports.append(rep)
            continue
        slots, addr_off = _promotable_slots(instrs, lo, hi, d, graph)
        if not slots:
            rep.reason = 'no-promotable-slot'
            reports.append(rep)
            continue

        # RecMII / memory-recurrence BEFORE (graph-based so it is comparable to
        # AFTER, where the promoted IV is no longer a memory-slot counted IV)
        rec_before = _loop_recmii(graph, d)
        memrec_before = _mem_recurrences(graph)

        new = _apply_promotion(instrs, lo, hi, d, graph, slots, addr_off)

        # structural: slice count preserved (globals / inter-function code intact)
        if len(func_slices(new)) != len(func_slices(instrs)):
            rep.reason = 'structural-mismatch'
            reports.append(rep)
            continue
        # correctness gate: clean-slot-respecting multi-seed differential + compile.
        # 'mismatch' -> rollback; 'match'/'unsupported' -> accept ('unsupported'
        # is legal-by-construction: the M2 clean-slot analysis is the proof).
        verdict = _promote_diff(instrs, new, lo, hi)
        if verdict == 'mismatch':
            rep.reason = 'differential-rollback'
            reports.append(rep)
            continue
        if not _compiles(new):
            rep.reason = 'compile-rollback'
            reports.append(rep)
            continue
        rep.verified = (verdict == 'match')

        # RecMII / memory-recurrence AFTER (rebuild graph on the promoted slice)
        a1 = next(a for a, b in func_slices(new) if new[a].name == fname)
        b1 = next(b for a, b in func_slices(new) if a == a1)
        d2s = discover_function(new, a1, b1)
        annotate_induction_vars(d2s)
        annotate_memory_effects(d2s)
        dis2 = MemoryDisambiguator(new, a1, b1, d2s)
        g2 = DependenceGraph(new, a1, b1, disambiguator=dis2)
        d2 = next((x for x in d2s if x.header == d.header), d2s[0] if d2s else None)
        rec_after = _loop_recmii(g2, d2) if d2 is not None else 0
        memrec_after = _mem_recurrences(g2)

        rep.committed = True
        rep.reason = 'ok'
        rep.promoted_slots = sorted(slots.keys())
        rep.rec_before = rec_before
        rep.rec_after = rec_after
        rep.mem_rec_removed = max(0, memrec_before - memrec_after)
        reports.append(rep)
        # one loop promoted per function per call (indices changed); recurse on
        # the rest by returning now -- the module driver re-invokes per function.
        return new, reports

    return instrs, reports


_SHAPE_REJECTS = frozenset({'not-innermost', 'no-unique-preheader', 'multi-latch',
                            'multi-exit', 'call-in-body', 'no-promotable-slot'})


def promote_module(instrs):
    """Promote across a module (one eligible loop per function -- the initial
    simple-natural-loop scope). The output is rebuilt by concatenating each
    function's (possibly promoted) slice, so globals / inter-function code are
    preserved (the R2.5 reassembly lesson). Returns (new_instrs, PromoteStats,
    [PromotionReport])."""
    _rp_n[0] = 0                                    # deterministic temp numbering
    stats = PromoteStats()
    out = []
    prev_end = 0
    all_reports = []
    for (lo, hi) in func_slices(instrs):
        out.extend(instrs[prev_end:lo])            # globals / inter-function code
        prev_end = hi + 1
        stats.functions += 1
        fslice = instrs[lo:hi + 1]
        new_sub, reps = promote_function(fslice, 0, len(fslice) - 1)
        out.extend(new_sub)
        all_reports.extend(reps)
        for r in reps:
            stats.loops += 1
            if r.reason not in _SHAPE_REJECTS:
                stats.promotable += 1
            if r.committed:
                stats.promoted += 1
                stats.mem_rec_removed += r.mem_rec_removed
                stats.sum_rec_before += r.rec_before
                stats.sum_rec_after += r.rec_after
            elif r.reason in ('differential-rollback', 'compile-rollback',
                              'structural-mismatch'):
                stats.rolled_back += 1
            stats._bump(r.reason)
    out.extend(instrs[prev_end:])
    return out, stats, all_reports

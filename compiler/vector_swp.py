"""
vector_swp.py -- Vector Software Pipelining for elementwise and AXPY (R6.8).

R6.6A measured which vector kernels software pipelining can help and which it
cannot:

    elementwise   HIGH      7 -> 3 bundles/iteration projected
    axpy          HIGH      7 -> 4
    reduction     LIMITED   10 -> 8, and R6.6's accumulator expansion already
                            beat that (-50% vs -20%), so SWP is not the tool
    GEMM          NONE      the vector loop has no counted induction variable
    convolution   NONE      fully unrolled -- there is no loop to pipeline
    dot           NONE      fully unrolled

This module implements the two HIGH cases and nothing else.

NO NEW SCHEDULER. The modulo scheduler (R2.5 `loopopt/modulo.py`), the compact
MVE kernel realiser (R2.8 `loopopt/pipeline_mve.py`), the dependence graph, the
memory disambiguator, the differential oracle and the rollback discipline are all
reused unmodified. The only change made to any of them is an optional `select`
predicate on `pipeline_mve_function`, because a vectorized program's FIRST loop is
a scalar initialisation loop and this pass has to target the vector one.

WHY A STRUCTURAL TEST AND NOT THE KERNEL NAME. The vectorizer's own `kind` cannot
be used to pick the two eligible families: convolution reports as `vector-add`
exactly like elementwise, and AXPY reports as `saxpy` exactly like GEMM (the GEMM
client owns that kind and chains through it). So eligibility is decided on the
emitted loop instead -- see `eligible_loop`.

WHERE IT RUNS. On the production-optimized IR, after the scalar optimizer and
R3.1's scalar SWP, pipelining only the vector loop. R3.1 applies SWP to the
PRE-optimization IR and splices whole function slices, which would be wrong here:
these programs keep their vector kernel in `main` alongside scalar initialisation
loops, and replacing the whole optimized `main` would discard LICM, IVSR and loop
register promotion for those loops.
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import IRLabel                                               # noqa: E402
from ir_utils import func_slices                                     # noqa: E402
from loopopt import modulo                                           # noqa: E402
from loopopt.pipeline_mve import (pipeline_mve_function, MVEStats)   # noqa: E402
from loopopt.analysis_iv import TripCount                            # noqa: E402

# Vector operations the modulo kernel model refuses by default (R2.5 scope guard).
# Their COST MODEL already exists -- `loopopt.schedule._latency` returns 2/4/4/3
# for them and `_iclass` classifies them ALU -- so the guard is scope, not missing
# support (R6.6A, section 2).
_VEC_ARITH = 'IRVecArith'
_VEC_FORBIDDEN = ('IRVecReduce', 'IRVecDot', 'IRVecDot128')

# Bitwise ops that only the R6.3 sliding-window (convolution) lowering emits. The
# window is reconstructed as `(W0 << 8s) | (W1 >>u (64-8s))`, so an `|` or a `>>`
# in a vector loop body means convolution, which R6.6A ruled out.
_WINDOW_OPS = ('|', '>>')

_DEFAULT_MIN_TRIP = int(os.environ.get('APARA_VSWP_MIN_TRIP', '4'))
# Minimum FRACTION of estimated dynamic bundles a pipeline must save to be kept.
_DEFAULT_MARGIN = float(os.environ.get('APARA_VSWP_MARGIN', '0.01'))


def _disabled():
    return os.environ.get('APARA_NO_VECTOR_SWP', '') not in ('', '0')


def _unblock():
    """Admit pipelines that only ever evict rematerializable values. OFF by
    default: it currently exposes an R6.8 defect (see the gate in
    `apply_vector_swp`). Kept as a one-flag reproducer for that work."""
    return os.environ.get('APARA_VSWP_UNBLOCK', '') not in ('', '0')


class _RelaxedKernelScope:
    """Temporarily let `modulo.build_kernel` accept vector arithmetic.

    Scoped rather than permanent: R3.1's scalar SWP shares this module, and
    widening its scope globally would change which SCALAR loops it pipelines --
    a behaviour change this milestone has no evidence for. Compilation is
    single-threaded, so a save/restore around the vector attempt is sufficient."""

    def __enter__(self):
        self._saved = modulo._UNSUPPORTED_KERNEL
        modulo._UNSUPPORTED_KERNEL = frozenset(
            x for x in self._saved if x != _VEC_ARITH)
        return self

    def __exit__(self, *exc):
        modulo._UNSUPPORTED_KERNEL = self._saved
        return False


class VectorSWPRecord:
    __slots__ = ('func', 'header', 'label', 'reason', 'committed',
                 'trip', 'mii', 'rec_mii', 'res_mii', 'ii', 'stages',
                 'bundles_before', 'bundles_after', 'compacted', 'loop_trips')

    def __init__(self, func, header, label):
        self.func = func
        self.header = header
        self.label = label
        self.reason = 'not-attempted'
        self.committed = False
        self.trip = self.mii = self.rec_mii = self.res_mii = None
        self.ii = self.stages = None
        self.compacted = False
        self.loop_trips = 0
        self.bundles_before = self.bundles_after = None

    def __repr__(self):
        return (f'<vswp {self.func}:{self.label} '
                f'{"committed" if self.committed else self.reason}>')


class VectorSWPSummary:
    def __init__(self):
        self.loops_seen = 0
        self.eligible = 0
        self.attempted = 0
        self.committed = 0
        self.rolled_back = 0
        self.changed = False


def _body_indices(desc, graph):
    idx = []
    for b in sorted(desc.body_blocks):
        blk = graph.cfg.blocks[b]
        idx.extend(range(blk.lo, blk.hi + 1))
    return sorted(idx)


def _header_label(desc, sub, graph):
    hblk = graph.cfg.blocks[desc.header]
    ins = sub[hblk.lo]
    return ins.name if isinstance(ins, IRLabel) else None


def eligible_loop(desc, sub, graph):
    """(ok, reason) -- is this loop an elementwise/AXPY vector kernel?

    Positive characterisation, decided on the EMITTED loop rather than on the
    vectorizer's kernel name (which does not separate these families):

      * it is a COMPACT vector loop            -- `vcl_` header label. A fully
        unrolled realisation has no loop, which is why convolution and dot are
        excluded automatically;
      * it performs vector ARITHMETIC          -- `IRVecArith` present;
      * it is NOT a reduction or dot product   -- no `IRVecReduce`, `IRVecDot`,
        `IRVecDot128`. This is what excludes reduction, whose loop is otherwise
        perfectly schedulable (R6.6 already gave it a better transform);
      * it is NOT a sliding window             -- no `|` / `>>`, which only the
        convolution lowering emits. Defence in depth: convolution currently has
        no compact realisation at any unroll factor, so this is unreachable
        today, but the exclusion is a property of the milestone, not of the
        current realisation choice;
      * it is counted and innermost            -- GEMM's vector loop fails this
        (`no-counted-iv` after optimization), which is what excludes GEMM.
    """
    label = _header_label(desc, sub, graph)
    if not label or not label.startswith('vcl_'):
        return False, 'not-a-compact-vector-loop'
    if not desc.is_innermost:
        return False, 'not-innermost'
    if desc.trip_count.kind != TripCount.KNOWN:
        return False, 'trip-not-known'

    kinds = set()
    ops = []
    for i in _body_indices(desc, graph):
        ins = sub[i]
        kinds.add(type(ins).__name__)
        if type(ins).__name__ == 'IRBinOp':
            ops.append(ins.op)
    if _VEC_ARITH not in kinds:
        return False, 'no-vector-arithmetic'
    for bad in _VEC_FORBIDDEN:
        if bad in kinds:
            return False, f'excluded-kernel:{bad}'
    for o in _WINDOW_OPS:
        if o in ops:
            return False, f'sliding-window-op:{o}'
    return True, 'ok'


def eligible_loops(ir):
    """[(fname, lo, hi, desc, reason)] for every loop, with its verdict. Analysis
    only -- used by the driver and by the tests."""
    from loopopt.discovery import discover_function
    from loopopt.analysis_iv import annotate_induction_vars
    from loopopt.analysis_mem import annotate_memory_effects
    from loopopt.depgraph import DependenceGraph
    from loopopt.depgraph_disambig import MemoryDisambiguator

    out = []
    for (lo, hi) in func_slices(ir):
        fname = getattr(ir[lo], 'name', '?')
        sub = ir[lo:hi + 1]
        descs = discover_function(sub, 0, len(sub) - 1)
        annotate_induction_vars(descs)
        annotate_memory_effects(descs)
        dis = MemoryDisambiguator(sub, 0, len(sub) - 1, descs)
        g = DependenceGraph(sub, 0, len(sub) - 1, disambiguator=dis)
        for d in descs:
            ok, why = eligible_loop(d, sub, g)
            out.append((fname, lo, hi, d, _header_label(d, sub, g), ok, why))
    return out


def _estimated_dynamic_bundles(ir, global_base, freq_override=None):
    """R6.1's frequency-weighted dynamic bundle count -- the SAME objective
    R6.4.1 validated against the simulator before it was allowed to choose an
    unroll factor. Reused rather than re-derived, and measured on the code that
    would actually ship.

    `freq_override` is REQUIRED for a pipelined candidate and the estimate is
    invalid without it. The register form promotes loop registers across the whole
    function, which erases the memory-slot induction variable `loopopt.analysis_iv`
    needs to prove a trip count (the known R2.6 interaction). Frequencies for the
    UNTOUCHED loops then silently collapse from 64 to 1, and the estimate fell
    from 702 to 76 bundles -- a 90% "gain" that was pure measurement error. The
    caller supplies the proven pre-SWP frequencies plus the new kernel's own trip
    count instead."""
    from vector_backend import ilp_analysis as _ia
    from vector_backend import occupancy as _occ
    from codegen import CodeGen
    cg = CodeGen(global_base=global_base)
    body = cg.generate(copy.deepcopy(ir), global_base=global_base)
    # R7.1 NOTE -- this gate deliberately still uses `spilled`, NOT the narrower
    # `spilled_to_memory`, even though rematerialization now makes several of
    # these pipelines memory-spill-free.
    #
    # Relaxing it admits `axpy vi32` / `axpy vu32`, whose pipelined code had never
    # executed before (the spill gate had always rejected it). It FAILS simulator
    # verification: "5 PostCondition comparisons performed, 4 declared" -- the
    # result-writing code runs more than once, so control flow through the
    # pipelined loop is wrong. Rematerialization is not the cause: with the SWP
    # pass disabled the same kernel passes with rematerialization ON, and the
    # emitted mcode is byte-identical with it on and off across all 18
    # kernel/marker combinations.
    #
    # So this is a latent R6.8 defect that R7.1 merely makes reachable, and its
    # own IR-level differential oracle does not detect it. Until that is fixed,
    # R7.1 ships as a memory-traffic optimization only and admits no new
    # pipelines. Reproduce with APARA_VSWP_UNBLOCK=1.
    if cg.spilled and not _unblock():
        return None
    if cg.spilled_to_memory:
        return None
    freq, _unknown = _ia.label_frequencies(ir)
    if freq_override:
        freq = dict(freq)
        freq.update(freq_override)
    return _occ.analyze_mcode(body, label_freq=freq).totals(dynamic=True)['bundles']


def _analysis(desc, sub, graph, rec):
    """Fill the record's RecMII / ResMII / MII / II / stages, for the report."""
    kernel, why = modulo.build_kernel(desc, graph)
    if kernel is None:
        rec.reason = f'kernel:{why}'
        return None
    mii, rmii, smii = modulo.min_ii(kernel)
    rec.mii, rec.rec_mii, rec.res_mii = mii, rmii, smii
    sched, _swhy = modulo.modulo_schedule(kernel, mii)
    if sched is not None:
        rec.ii = sched.ii
        rec.stages = max(sched.stage_of(o) for o in kernel.ops) + 1
    return kernel


def apply_vector_swp(prod_ir, global_base=0x400,
                     min_trip=_DEFAULT_MIN_TRIP, margin=_DEFAULT_MARGIN):
    """Software-pipeline eligible vector loops in the production IR.

    Returns (final_ir, [VectorSWPRecord], VectorSWPSummary). `final_ir is
    prod_ir` when nothing was applied, so the caller can keep the proven output
    byte for byte.

    Gates, all of which must pass -- none of them new, all reused:
      1. the loop is an elementwise/AXPY compact vector loop (`eligible_loop`);
      2. its trip count is KNOWN and at least `min_trip`, so the prologue and
         epilogue can be amortised (R6.6A measured reduction's entire projected
         gain being consumed by them at trip 4);
      3. R2.8 commits a pipeline for it -- which means the modulo schedule was
         found AND independently verified AND its own differential oracle
         matched AND `_codegen_keeps_alive` held;
      4. the whole program still compiles with ZERO spills;
      5. the estimated DYNAMIC bundle count falls by at least `margin`.
    """
    summary = VectorSWPSummary()
    records = []
    if _disabled():
        return prod_ir, records, summary

    targets = []
    for (fname, lo, hi, desc, label, ok, why) in eligible_loops(prod_ir):
        summary.loops_seen += 1
        if not ok:
            continue
        rec = VectorSWPRecord(fname, desc.header, label)
        rec.trip = desc.trip_count.value
        if rec.trip is None or rec.trip < min_trip:
            rec.reason = f'trip-too-small:{rec.trip}'
            records.append(rec)
            continue
        summary.eligible += 1
        targets.append((fname, lo, hi, desc.header, rec))

    if not targets:
        return prod_ir, records, summary

    base = _estimated_dynamic_bundles(prod_ir, global_base)
    if base is None:
        return prod_ir, records, summary
    # The trip counts proved on the UNPIPELINED IR. Loop bodies the pipeline does
    # not touch execute the same number of times whatever the register form does
    # to their code, so these stay authoritative for them.
    from vector_backend import ilp_analysis as _ia
    proven_freq, _unk = _ia.label_frequencies(prod_ir)

    working = prod_ir
    for (fname, lo, hi, header, rec) in targets:
        summary.attempted += 1
        rec.bundles_before = base
        try:
            with _RelaxedKernelScope():
                # Re-derive the slice from the CURRENT working IR: an earlier
                # commit may have changed instruction indices.
                b = _slice_of(working, fname)
                if b is None:
                    rec.reason = 'slice-not-found'
                    records.append(rec)
                    summary.rolled_back += 1
                    continue
                wlo, whi = b
                stats = MVEStats()
                reps = []
                new_slice = pipeline_mve_function(
                    working, wlo, whi, stats, reps,
                    select=lambda d, sub, g: (d.header == header
                                              and eligible_loop(d, sub, g)[0]))
                sub_old = working[wlo:whi + 1]
                # commitment is recorded per REPORT (MVEStats has no such field);
                # the identity check also catches a silent no-op.
                if not any(getattr(r, 'committed', False) for r in reps) \
                        or list(new_slice) == list(sub_old):
                    rec.reason = (reps[0].reason if reps else 'no-loop-selected')
                    records.append(rec)
                    summary.rolled_back += 1
                    continue
                # record the schedule facts for the report
                _fill_from_reports(rec, reps)
                candidate = working[:wlo] + list(new_slice) + working[whi + 1:]
        except Exception as e:                      # never break the build
            rec.reason = f'exception:{type(e).__name__}'
            records.append(rec)
            summary.rolled_back += 1
            continue

        # The pipelined kernel is a NEW loop with a new label; its trip count is
        # not inferable from the promoted IR but the realiser reports it exactly.
        override = dict(proven_freq)
        klabel = _kernel_label(new_slice, sub_old)
        if klabel is not None and rec.loop_trips:
            override[klabel] = float(rec.loop_trips)
        elif rec.compacted:
            rec.reason = 'kernel-trip-unknown'   # refuse to guess
            records.append(rec)
            summary.rolled_back += 1
            continue
        after = _estimated_dynamic_bundles(candidate, global_base,
                                           freq_override=override)
        if after is None:
            rec.reason = 'spilled'
            records.append(rec)
            summary.rolled_back += 1
            continue
        rec.bundles_after = after
        if after > base * (1.0 - margin):
            rec.reason = f'not-profitable:{base}->{after}'
            records.append(rec)
            summary.rolled_back += 1
            continue

        working = candidate
        base = after
        rec.committed = True
        rec.reason = 'ok'
        records.append(rec)
        summary.committed += 1

    summary.changed = working is not prod_ir
    return working, records, summary


def _kernel_label(new_slice, old_slice):
    """The label the compact MVE kernel loop was given -- the one label present
    after pipelining that was not there before."""
    old = {i.name for i in old_slice if isinstance(i, IRLabel)}
    new = [i.name for i in new_slice if isinstance(i, IRLabel) and i.name not in old]
    return new[0] if len(new) == 1 else None


def _slice_of(ir, fname):
    for (lo, hi) in func_slices(ir):
        if getattr(ir[lo], 'name', None) == fname:
            return lo, hi
    return None


def _fill_from_reports(rec, reps):
    for r in reps:
        if not getattr(r, 'committed', False):
            continue
        rec.ii = getattr(r, 'ii', None) or rec.ii
        rec.stages = getattr(r, 'stages', None) or rec.stages
        rec.compacted = bool(getattr(r, 'compacted', False))
        rec.loop_trips = getattr(r, 'loop_trips', 0)


def format_vector_swp(records, summary):
    lines = [f"vector SWP: {summary.committed} committed / {summary.attempted} "
             f"attempted / {summary.eligible} eligible of {summary.loops_seen} loops"]
    for r in records:
        lines.append(f"  {r.func}:{r.label} trip={r.trip} "
                     f"RecMII={r.rec_mii} ResMII={r.res_mii} MII={r.mii} "
                     f"II={r.ii} stages={r.stages} "
                     f"bundles {r.bundles_before}->{r.bundles_after} "
                     f"{'COMMITTED' if r.committed else r.reason}")
    return "\n".join(lines)

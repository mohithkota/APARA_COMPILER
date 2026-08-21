"""
dot_vectorizer.py -- Automatic Dot-Product & Sum-Reduction Vectorization (R4.1,
converted to a `vector_pipeline` client in R4.2).

The first production vector transformation. As of R4.2 it no longer owns a driver:
the detect -> legality -> profitability -> transform -> validate -> compile ->
commit/rollback sequence lives in `vector_pipeline.py`, and this module supplies
only the dot/reduction-specific parts:

    kinds           dot-product, sum-reduction
    lower()         packed loads + $dot / $vreduce + a scalar remainder
    dynamic_model() the executed-operation accounting for a reduction kernel

Pattern matching stays inside `vector_lowering.plan_lowering` (which reports
'unpacked-array-stride', 'iv-init-not-found', ...), so the default match() hook
is used and R4.1's gate order and outcomes are preserved EXACTLY -- the conversion
is a refactor, not a behaviour change.

`reduction_vectorizer.py` selects the sum-reduction half of the same client.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vector_pipeline import (VectorTransform, MatchResult, DynamicModel,
                             run_module, VectorizeReport, VectorizeStats,
                             _bundles)
import vector_lowering as _vl
import vector_compact_loop as _vcl
from vector_lowering import lower_kernel, plan_lowering, differential_packed
from vector_dynamic import model_realisation

# R13.0: 'matmul' joins the kinds this client claims. It is the SAME client,
# the SAME planner and the SAME $dot emitter -- a matmul inner loop is a dot
# product whose operand addresses carry an invariant row base, which
# `plan_lowering` now expresses (`array_offs`/`array_addr`). No second dot
# backend and no matmul-specific lowering exists.
_SUPPORTED = ('dot-product', 'sum-reduction', 'matmul')

# R13.0 kill switch, same idiom as APARA_NO_ALIGN_BUNDLE (R9.5) and
# APARA_NO_PROBE_RESCUE (R11): drop 'matmul' to reproduce the R12.1 behaviour
# exactly, so an A/B is measured on ONE binary rather than across commits.
if os.environ.get('APARA_NO_MATMUL_DOT', '') not in ('', '0'):
    _SUPPORTED = ('dot-product', 'sum-reduction')


class DotReductionTransform(VectorTransform):
    """Dot product and sum reduction over packed arrays (the R4.1 kernels)."""

    name = 'dot-reduction'

    def __init__(self, kinds=_SUPPORTED, global_base=0x400):
        self.kinds = tuple(kinds)
        # R4.2.5 needs the real backend to compare realisations. It is passed in
        # by the entry point, so the PIPELINE is untouched -- clients are already
        # constructed by the caller.
        self.global_base = global_base

    def reset(self):
        # shared fresh-temp / label counters: reset per module so repeated runs of
        # the same program emit identical names (determinism).
        _vl._vec_n[0] = 0
        _vcl.reset_labels()

    def match(self, desc, instrs, kernel, legality):
        """Pattern matching lives here (symmetric with the elementwise client), so
        the plan it produces is available to lower() and dynamic_model()."""
        plan = plan_lowering(desc, instrs, kernel, legality)
        if not plan.ok:
            return MatchResult(False, plan.reason)
        return MatchResult(True, info=plan)

    def lower(self, instrs, lo, hi, desc, kernel, legality, match):
        return lower_kernel(instrs, lo, hi, match.info, self.global_base)

    def dynamic_model(self, desc, kernel, legality, match):
        """Executed-operation accounting for whichever realisation lowering chose.

        Unrolled: the straight-line body runs once, so its exact emitted length IS
        the executed count (this reproduces R4.1's hand-derived 5/chunk + 4 and
        4/chunk + 4 constants exactly, now derived instead of hardcoded).
        Compact: `chunks` iterations of the loop body plus the exit test."""
        return model_realisation(match.info, desc)


# ── entry points ────────────────────────────────────────────────────────────────

def vectorize_module(instrs, allowed=_SUPPORTED, global_base=0x400):
    """Vectorize dot-product / sum-reduction kernels across a module. Returns
    (new_instrs, VectorizeStats, [VectorizeReport]).

    R4.1-compatible signature, now implemented on the generic pipeline."""
    return run_module(instrs, [DotReductionTransform(allowed, global_base)],
                      global_base=global_base)


def vectorize_dot_module(instrs, global_base=0x400):
    """Vectorize ONLY dot-product loops."""
    return vectorize_module(instrs, allowed=('dot-product',),
                            global_base=global_base)


def _build_module(instrs, global_base):
    from elementwise_vectorizer import ElementwiseTransform
    from gemm_vectorizer import GemmTransform
    # GemmTransform owns the 'saxpy' kind and chains GEMM -> AXPY -> elementwise
    # multiply internally (see its docstring), so it supersedes AxpyTransform in
    # production while reusing both of their planners and lowerings.
    return run_module(instrs, [DotReductionTransform(_SUPPORTED, global_base),
                               ElementwiseTransform(global_base),
                               GemmTransform(global_base)],
                      global_base=global_base)


# R6.4.1: unroll factors the adaptive search considers, largest first so a tie
# keeps the larger factor (fewer loop iterations for the same estimate).
_UNROLL_CANDIDATES = (8, 4, 2, 1)


def _estimated_dynamic_bundles(vec_ir, global_base):
    """Frequency-weighted dynamic bundle count of a candidate build.

    Reuses R6.1 wholesale: `label_frequencies` supplies each block's proved trip
    count and `occupancy.analyze_mcode` weights every emitted bundle by it. A
    STATIC bundle count is the wrong objective for unrolling -- it always prefers
    no unrolling -- and a count that weights only the vector loop is also wrong,
    because it ignores the other loops in the program. This objective was
    validated against the simulator before being used to choose anything: it
    picks the measured-fastest factor on 8 of 8 kernels (R6_4_1 report)."""
    import copy as _copy
    from vector_backend import ilp_analysis as _ia
    from vector_backend import occupancy as _occ
    _p, body, _t = _ia.production_codegen(_copy.deepcopy(vec_ir))
    freq, _unknown = _ia.label_frequencies(vec_ir)
    rep = _occ.analyze_mcode(body, label_freq=freq)
    return rep.totals(dynamic=True)['bundles']


def _expanded(vec_ir):
    """True if R6.6 accumulator expansion actually fired in this build.

    Read off the emitted IR (the `_vxa` accumulator prefix is unique to
    `reduction_accumulator_expansion`) rather than threaded through the plan, so
    no client signature changes -- the same trick `vector_compact_loop.
    realisation_of` uses for the realisation."""
    from ir_utils import dest_names
    for i in vec_ir:
        for n in (dest_names(i) or ()):
            if n.startswith('_vxa'):
                return True
    return False


def vectorize_all_module(instrs, global_base=0x400):
    """THE PRODUCTION ENTRY POINT (R4.2): every vectorizer, one pipeline, one
    pass over the module. Clients are tried in order per loop; each loop is
    claimed by whichever client recognises its kind.

    R6.4.1: the vector unroll factor is CHOSEN BY MEASUREMENT rather than fixed.
    Each candidate factor is built through this same pipeline -- so each is
    validated by the differential oracle exactly as before -- and the one with
    the lowest estimated dynamic bundle count wins. `APARA_VECTOR_UNROLL` pins
    the factor and skips the search."""
    import os as _os
    if _os.environ.get('APARA_VECTOR_UNROLL'):
        return _build_module(instrs, global_base)

    import copy as _copy
    best = None

    def _try(u, no_expand):
        """Build one candidate and return (cost, out, stats, reps) or None."""
        _os.environ['APARA_VECTOR_UNROLL'] = str(u)
        if no_expand:
            _os.environ['APARA_NO_ACC_EXPAND'] = '1'
        try:
            out, stats, reps = _build_module(_copy.deepcopy(instrs), global_base)
            if not stats.vectorized:
                return None                    # this factor lost the kernel
            return (_estimated_dynamic_bundles(out, global_base), out, stats, reps)
        except Exception:
            return None                        # a candidate never breaks the build
        finally:
            _os.environ.pop('APARA_VECTOR_UNROLL', None)
            if no_expand:
                _os.environ.pop('APARA_NO_ACC_EXPAND', None)

    for u in _UNROLL_CANDIDATES:
        c = _try(u, no_expand=False)
        if c is not None and (best is None or c[0] < best[0]):
            best = c
        # R6.6: accumulator expansion is a CANDIDATE, not a default. It shortens
        # the reduction recurrence (RecMII 8 -> 3 on reduction vi32) but makes
        # the vector body denser, and R3.2's superblock pass accepts or rejects
        # its merges for the WHOLE module -- so at u=8 a denser body can cost an
        # unrelated 64-iteration scalar loop its merge, which is worth more than
        # the loop the expansion improved. Measured, not predicted: expansion is
        # kept only where it lowers the same estimated dynamic bundle count
        # R6.4.1 already validated against the simulator.
        #
        # The no-expand variant is only built when the expanded build actually
        # expanded something, so every non-reduction module compiles in exactly
        # the same time as before.
        if c is not None and _expanded(c[1]):
            c2 = _try(u, no_expand=True)
            if c2 is not None and c2[0] < best[0]:
                best = c2
    if best is None:
        return _build_module(instrs, global_base)   # nothing vectorized; scalar
    return best[1], best[2], best[3]

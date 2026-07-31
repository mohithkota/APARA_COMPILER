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


def vectorize_all_module(instrs, global_base=0x400):
    """THE PRODUCTION ENTRY POINT (R4.2): every vectorizer, one pipeline, one
    pass over the module. Clients are tried in order per loop; each loop is
    claimed by whichever client recognises its kind."""
    from elementwise_vectorizer import ElementwiseTransform
    return run_module(instrs, [DotReductionTransform(_SUPPORTED, global_base),
                               ElementwiseTransform(global_base)],
                      global_base=global_base)

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

from vector_pipeline import (VectorTransform, DynamicModel, run_module,
                             VectorizeReport, VectorizeStats, _bundles)
import vector_lowering as _vl
from vector_lowering import lower_kernel, differential_packed

_SUPPORTED = ('dot-product', 'sum-reduction')


class DotReductionTransform(VectorTransform):
    """Dot product and sum reduction over packed arrays (the R4.1 kernels)."""

    name = 'dot-reduction'

    def __init__(self, kinds=_SUPPORTED):
        self.kinds = tuple(kinds)

    def reset(self):
        # shared fresh-temp counter: reset per module so repeated runs of the same
        # program emit identical temp names (determinism).
        _vl._vec_n[0] = 0

    def lower(self, instrs, lo, hi, desc, kernel, legality, match):
        return lower_kernel(instrs, lo, hi, desc, kernel, legality)

    def dynamic_model(self, desc, kernel, legality, match):
        """R4.1's model, moved here unchanged.

        A loop's cost is per-iteration work x trip. Vectorization runs the packed
        body once (chunks unrolled) + the scalar remainder, replacing `lanes`
        scalar iterations with one vector op. Static code may grow -- the
        code-size-for-speed trade -- so the gate is on DYNAMIC operations."""
        body_ops = getattr(desc, 'body_inst_count', 0) or 1
        lanes = max(1, legality.lanes)
        chunks = kernel.trip // lanes
        remainder = kernel.trip % lanes
        per_chunk = 5 if kernel.kind == 'dot-product' else 4   # loads + $dot/$vreduce
        scalar_ops = body_ops * kernel.trip
        vector_ops = chunks * per_chunk + 4 + body_ops * remainder
        return DynamicModel(scalar_ops, vector_ops, chunks=chunks,
                            remainder=remainder)


# ── entry points ────────────────────────────────────────────────────────────────

def vectorize_module(instrs, allowed=_SUPPORTED, global_base=0x400):
    """Vectorize dot-product / sum-reduction kernels across a module. Returns
    (new_instrs, VectorizeStats, [VectorizeReport]).

    R4.1-compatible signature, now implemented on the generic pipeline."""
    return run_module(instrs, [DotReductionTransform(allowed)],
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
    return run_module(instrs, [DotReductionTransform(), ElementwiseTransform()],
                      global_base=global_base)

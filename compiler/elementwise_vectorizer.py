"""
elementwise_vectorizer.py -- Automatic Elementwise Vectorization (R4.2).

The second client of the generic vectorization pipeline (`vector_pipeline.py`).
It supplies ONLY pattern matching, lowering and a dynamic model; detection,
legality, profitability, validation, compilation, commit and rollback are the
framework's -- there is one production vectorization pipeline, not two.

Supported shapes (everything else is rejected):

    A[i] = B[i];   A[i] = B[i] + C[i];   A[i] = B[i] - C[i];   A[i] = B[i] * C[i];

Requirements enforced by the match: packed arrays, affine accesses, a known trip
count shared by every access (one IV, one loop), and contiguous (stride ==
element size) accesses.

Why the detector's kinds are only a PRE-FILTER: kernel_detector labels a loop
'vector-add' when the stored value has no multiply and 'saxpy' when it does, so
`A[i] = B[i] * C[i]` arrives as 'saxpy'. This client claims both kinds and then
does its own exact shape analysis -- a real saxpy (`a*x[i]`, scalar times array)
fails the match because its operand is not an array load, and is rolled back.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vector_pipeline import (VectorTransform, MatchResult, DynamicModel,
                             run_module)
import vector_lowering as _vl
from vector_elementwise_lowering import plan_elementwise, lower_elementwise


class ElementwiseTransform(VectorTransform):
    """Elementwise copy / add / sub / multiply over packed arrays."""

    name = 'elementwise'
    kinds = ('vector-add', 'saxpy')     # pre-filter; match() decides for real

    def reset(self):
        # shared fresh-temp counter: reset per module so two runs of the same
        # program emit identical names (the determinism property R4.1 relies on)
        _vl._vec_n[0] = 0

    def match(self, desc, instrs, kernel, legality):
        plan = plan_elementwise(desc, instrs, kernel, legality)
        if not plan.ok:
            return MatchResult(False, plan.reason)
        return MatchResult(True, info=plan)

    def lower(self, instrs, lo, hi, desc, kernel, legality, match):
        return lower_elementwise(instrs, lo, hi, desc, kernel, legality,
                                 match.info)

    def dynamic_model(self, desc, kernel, legality, match):
        """Executed-operation accounting.

        The scalar loop runs `body_ops` instructions `trip` times. The vector form
        runs the emitted straight-line body ONCE (its length is exact -- the
        chunks are unrolled, so there is no loop overhead to estimate) plus the
        scalar remainder iterations. Static size may GROW; what must fall is this
        dynamic count, which is what the pipeline gates on."""
        plan = match.info
        body_ops = getattr(desc, 'body_inst_count', 0) or 1
        scalar_ops = body_ops * plan.trip
        vector_ops = plan.body_len + body_ops * plan.remainder
        return DynamicModel(scalar_ops, vector_ops,
                            chunks=plan.chunks, remainder=plan.remainder)


def vectorize_elementwise_module(instrs, global_base=0x400):
    """Vectorize ONLY elementwise loops. (The production compiler runs the full
    client set; this entry point exists for testing and corpus measurement.)"""
    return run_module(instrs, [ElementwiseTransform()], global_base=global_base)

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
import vector_compact_loop as _vcl
from vector_elementwise_lowering import plan_elementwise, lower_elementwise
from vector_dynamic import model_realisation


class ElementwiseTransform(VectorTransform):
    """Elementwise copy / add / sub / multiply over packed arrays."""

    name = 'elementwise'
    kinds = ('vector-add',)             # 'saxpy' is owned by AxpyTransform (R4.3)

    def __init__(self, global_base=0x400):
        # R4.2.5 needs the real backend to compare realisations. It is passed in
        # by the entry point, so the PIPELINE is untouched -- clients are already
        # constructed by the caller.
        self.global_base = global_base

    def reset(self):
        # shared fresh-temp / label counters: reset per module so two runs of the
        # same program emit identical names (the determinism property R4.1 relies
        # on)
        _vl._vec_n[0] = 0
        _vcl.reset_labels()

    def match(self, desc, instrs, kernel, legality):
        plan = plan_elementwise(desc, instrs, kernel, legality)
        if not plan.ok:
            return MatchResult(False, plan.reason)
        return MatchResult(True, info=plan)

    def lower(self, instrs, lo, hi, desc, kernel, legality, match):
        return lower_elementwise(instrs, lo, hi, desc, kernel, legality,
                                 match.info, self.global_base)

    def dynamic_model(self, desc, kernel, legality, match):
        """Executed-operation accounting for whichever realisation lowering chose
        (see vector_dynamic). Static size may GROW; what must fall is this dynamic
        count, which is what the pipeline gates on."""
        return model_realisation(match.info, desc)


def vectorize_elementwise_module(instrs, global_base=0x400):
    """Vectorize ONLY elementwise loops. (The production compiler runs the full
    client set; this entry point exists for testing and corpus measurement.)

    R4.3 note: `kernel_detector` labels an elementwise MULTIPLY (`C[i]=A[i]*B[i]`)
    as kind 'saxpy', which the AXPY client now owns and which falls back to the
    elementwise planner for exactly this shape. Registering both clients here
    keeps this entry point's contract -- every elementwise shape R4.2 handled --
    unchanged."""
    from axpy_vectorizer import AxpyTransform
    return run_module(instrs, [ElementwiseTransform(global_base),
                               AxpyTransform(global_base)],
                      global_base=global_base)

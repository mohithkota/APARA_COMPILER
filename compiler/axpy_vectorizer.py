"""
axpy_vectorizer.py -- Automatic AXPY Vectorization (R4.3).

The first production client of `vector_affine` (R4.2.8): it supplies pattern
matching, lowering and a dynamic model, and every access classification it makes
is delegated to that analysis. The pipeline (`vector_pipeline.py`) is untouched.

    for (i)  Y[i] += a * X[i];      ->   packed load X / $v * $replicate(a)
                                         packed load Y / $v + / packed store Y

WHY THIS CLIENT ALSO OWNS THE ELEMENTWISE-MULTIPLY SHAPE
--------------------------------------------------------
`kernel_detector` labels ANY loop whose stored value contains a multiply as
'saxpy' -- both `Y[i] += a*X[i]` (a real AXPY) and `C[i] = A[i]*B[i]` (elementwise,
owned by R4.2). The pipeline dispatches one client per detected kind, so exactly
one of them can claim 'saxpy'.

Rather than change the pipeline or coarsen the detector, the AXPY client claims
'saxpy' and, when its own match fails, falls back to the R4.2 elementwise planner
and lowering. Elementwise keeps 'vector-add' as before. The net effect is that
every shape that vectorized under R4.2 still vectorizes, by the same code, plus
AXPY -- verified by the R4.2 suite passing unchanged.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vector_pipeline import VectorTransform, MatchResult, run_module
import vector_lowering as _vl
import vector_compact_loop as _vcl
from vector_dynamic import model_realisation
from axpy_lowering import plan_axpy, lower_axpy
from vector_elementwise_lowering import plan_elementwise, lower_elementwise


class AxpyTransform(VectorTransform):
    """Y[i] += a*X[i] over packed 1-D arrays (falls back to elementwise-multiply)."""

    name = 'axpy'
    kinds = ('saxpy',)

    def __init__(self, global_base=0x400):
        self.global_base = global_base

    def reset(self):
        _vl._vec_n[0] = 0
        _vcl.reset_labels()

    def match(self, desc, instrs, kernel, legality):
        plan = plan_axpy(desc, instrs, kernel, legality)
        if plan.ok:
            return MatchResult(True, info=plan)
        alt = plan_elementwise(desc, instrs, kernel, legality)
        if alt.ok:
            return MatchResult(True, info=alt)      # R4.2 elementwise-multiply
        return MatchResult(False, f'{plan.reason}|elementwise:{alt.reason}')

    def lower(self, instrs, lo, hi, desc, kernel, legality, match):
        plan = match.info
        if hasattr(plan, 'a_slot'):                 # an AxpyPlan
            return lower_axpy(instrs, lo, hi, plan, self.global_base)
        return lower_elementwise(instrs, lo, hi, desc, kernel, legality, plan,
                                 self.global_base)

    def dynamic_model(self, desc, kernel, legality, match):
        return model_realisation(match.info, desc)


def vectorize_axpy_module(instrs, global_base=0x400):
    """Vectorize ONLY AXPY (and the elementwise-multiply shape it inherits)."""
    return run_module(instrs, [AxpyTransform(global_base)],
                      global_base=global_base)

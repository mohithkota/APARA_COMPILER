"""
gemm_vectorizer.py -- Automatic Packed GEMM Vectorization (R4.4).

The GEMM inner loop is an AXPY over a row, so this client is a thin extension of
R4.3's rather than a new transformation: it tries `plan_gemm` first (row-aware),
then falls back to the plain AXPY plan and finally to the R4.2 elementwise plan.
`vector_pipeline.py` is untouched.

Why the chain lives in one client: `kernel_detector` labels every multiply-bearing
stored value 'saxpy' -- GEMM, AXPY and elementwise multiply alike -- and the
pipeline dispatches ONE client per detected kind. Chaining inside the owner keeps
that contract without changing the pipeline or coarsening the detector.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vector_pipeline import VectorTransform, MatchResult, run_module
import vector_lowering as _vl
import vector_compact_loop as _vcl
from vector_dynamic import model_realisation
from gemm_lowering import plan_gemm, lower_gemm
from axpy_lowering import plan_axpy, lower_axpy
from vector_elementwise_lowering import plan_elementwise, lower_elementwise


class GemmTransform(VectorTransform):
    """Packed GEMM (i-k-j) -> AXPY -> elementwise multiply, in that order."""

    name = 'gemm'
    kinds = ('saxpy',)

    def __init__(self, global_base=0x400):
        self.global_base = global_base

    def reset(self):
        _vl._vec_n[0] = 0
        _vcl.reset_labels()

    def match(self, desc, instrs, kernel, legality):
        g = plan_gemm(desc, instrs, kernel, legality)
        if g.ok:
            return MatchResult(True, info=g)
        a = plan_axpy(desc, instrs, kernel, legality)
        if a.ok:
            return MatchResult(True, info=a)
        e = plan_elementwise(desc, instrs, kernel, legality)
        if e.ok:
            return MatchResult(True, info=e)
        return MatchResult(False, f'{g.reason}|axpy:{a.reason}|elem:{e.reason}')

    def lower(self, instrs, lo, hi, desc, kernel, legality, match):
        plan = match.info
        if getattr(plan, 'row_based', False):
            return lower_gemm(instrs, lo, hi, plan, self.global_base)
        if hasattr(plan, 'a_slot'):
            return lower_axpy(instrs, lo, hi, plan, self.global_base)
        return lower_elementwise(instrs, lo, hi, desc, kernel, legality, plan,
                                 self.global_base)

    def dynamic_model(self, desc, kernel, legality, match):
        return model_realisation(match.info, desc)


def vectorize_gemm_module(instrs, global_base=0x400):
    """Vectorize the GEMM/AXPY/elementwise-multiply family only."""
    return run_module(instrs, [GemmTransform(global_base)],
                      global_base=global_base)

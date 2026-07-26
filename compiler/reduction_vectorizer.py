"""
reduction_vectorizer.py -- Automatic Sum-Reduction Vectorization (R4.1).

The sum-reduction specialization of the shared vectorization driver in
`dot_vectorizer.py`. There is ONE pipeline (detect -> legality -> profitability ->
lowering -> differential validation -> backend/bundle check -> commit-or-rollback);
this module simply restricts it to the sum-reduction kernel class, which lowers to
a packed vector load + `$vreduce` + a scalar accumulate, with a scalar remainder
loop. No infrastructure is duplicated.
"""

from dot_vectorizer import vectorize_module


def vectorize_reduction_module(instrs, global_base=0x400):
    """Vectorize ONLY sum-reduction loops. Returns (new_instrs, VectorizeStats,
    [VectorizeReport])."""
    return vectorize_module(instrs, allowed=('sum-reduction',),
                            global_base=global_base)

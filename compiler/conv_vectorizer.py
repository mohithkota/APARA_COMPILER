"""
conv_vectorizer.py -- Automatic Convolution Vectorization (R4.6).

THE HEADLINE RESULT: convolution required NO new client logic at all.

A convolution written in its natural fused form

    out[i] = w0*in[i] + w1*in[i+1] + w2*in[i+2];          (1-D, 3-tap)
    out[i*N+j] = w0*in[i*N+j] + w1*in[i*N+j+1] + ...      (2-D, inner row)

is -- to the existing infrastructure -- an ELEMENTWISE EXPRESSION over shifted
contiguous accesses. `vector_affine` already classifies `in[i+k]` as CONTIGUOUS,
`expression_tree` already represents the fused sum-of-products, and
`expression_lowering` already emits both its vector body and its scalar remainder.

The ONE thing that was missing was not recognition or lowering but ADDRESSING: the
tree-driven vector body addressed every array as `base + chunk*lanes*elem_bytes`,
which silently assumes the invariant part of the offset is zero. That is true for
`a[i]` and false for `in[i+k]`. R4.4 had already solved the identical problem for
GEMM row bases with `clone_offset` (re-emit the loop's own address computation with
the induction variable substituted); R4.6 wires that same mechanism into the tree
path. Every convolution form then vectorizes through the untouched pipeline.

So this module is deliberately thin: it exposes an entry point and documents the
result. There is no `ConvTransform`, no convolution recognizer and no convolution
lowering, because none is needed -- which is exactly what this milestone set out
to test.

WHAT IS SUPPORTED (measured, see conv_corpus.py)
    1-D 3/5/7-tap fused convolution, constant or scalar-variable weights
    2-D stencils whose innermost dimension is contiguous
    remainder trip counts (peeled or scalar-tailed by the shared framework)

WHAT IS REJECTED (and why, by existing analysis -- no new rejection logic)
    dynamic/data-dependent windows   gathers -> `vector_affine` says UNKNOWN
    column-strided stencils          `vector_affine` says STRIDED
    windows deeper than MAX_DEPTH    `expression_tree` declines, never mis-lowers
    unsupported element widths       the R4.0 capability layer
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dot_vectorizer import vectorize_all_module


def vectorize_conv_module(instrs, global_base=0x400):
    """Vectorize convolution kernels.

    Intentionally delegates to the standard client set: a fused convolution is
    recognised and lowered by the elementwise/expression client. Provided so the
    corpus and tests can name the capability being measured."""
    return vectorize_all_module(instrs, global_base=global_base)

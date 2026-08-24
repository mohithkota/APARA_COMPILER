"""
expression_lowering.py -- Recursive Vector & Scalar Expression Lowering (R4.5).

One tree, two evaluators. Neither knows anything about a kernel:

    lower_vector(tree, ...)   -> packed loads + `$v` ops for ONE chunk
    lower_scalar(tree, idx)   -> ordinary scalar IR for ONE element

`lower_scalar` is what the remainder framework uses, so a client that describes
its computation once gets both the vector body and the peeled tail from the same
description -- there is no second, hand-written scalar lowering to drift.

No new IR and no new vector instruction: the output is `IRVecArith` (`$v`) with
`$replicate` for broadcast scalars, exactly what R4.0's capability database
already reports.

ONE ISA CONSTRAINT IS EXPLICIT HERE. `$replicate` broadcasts **src2** only (see
codegen `_gen_IRVecArith`). A scalar on the LEFT of a commutative operator is
commuted into src2; on the left of `-` it cannot be, so such a tree is rejected
rather than mis-emitted.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import (Const as IRConst, IRAssign, IRLoad, IRStore, IRLoadAddr,
                IRBinOp, IRVecArith, emit_array_base)
from vector_lowering import _fresh
import expression_tree as et

_COMMUTATIVE = ('+', '*')


# ── vector side ─────────────────────────────────────────────────────────────────

def lower_vector(node, vtype, load_array, prefix='_ve'):
    """Emit vector IR computing `node` for one chunk.

    `load_array(ArrayRef, dest_temp)` -> instrs performs the packed load, so the
    caller decides constant-offset (unrolled) vs register-offset (compact)
    addressing. Returns (instrs, value_temp, is_scalar) or (None, reason, None).
    `is_scalar` marks a value that lives in one register rather than lanes."""
    if isinstance(node, et.Const):
        t = _fresh(prefix + 'k')
        return [IRAssign(t, IRConst(node.value))], t, True
    if isinstance(node, et.ScalarRef):
        b, t = _fresh(prefix + 'b'), _fresh(prefix + 's')
        return ([emit_array_base(b, node.slot),
                 IRLoad(t, b, IRConst(0), elem_bytes=node.elem_bytes,
                        unsigned=node.unsigned)], t, True)
    if isinstance(node, et.ArrayRef):
        t = _fresh(prefix + 'a')
        return list(load_array(node, t)), t, False
    if not isinstance(node, et.BinOp):
        return None, 'unsupported-node', None

    li, lt, lscal = lower_vector(node.left, vtype, load_array, prefix)
    if li is None:
        return None, lt, None
    ri, rt, rscal = lower_vector(node.right, vtype, load_array, prefix)
    if ri is None:
        return None, rt, None
    out = list(li) + list(ri)
    d = _fresh(prefix + 'r')

    if lscal and rscal:                     # wholly invariant subtree: stays scalar
        out.append(IRBinOp(d, node.op, lt, rt, unsigned=node.unsigned))
        return out, d, True
    if rscal:                               # broadcast the right operand
        out.append(IRVecArith(d, node.op, lt, rt, '$' + vtype, replicate=True))
        return out, d, False
    if lscal:
        if node.op not in _COMMUTATIVE:
            # $replicate only broadcasts src2; `scalar - vector` cannot be
            # expressed without a new instruction, so it is refused.
            return None, f'scalar-on-left-of-{node.op}', None
        out.append(IRVecArith(d, node.op, rt, lt, '$' + vtype, replicate=True))
        return out, d, False
    out.append(IRVecArith(d, node.op, lt, rt, '$' + vtype))
    return out, d, False


# ── scalar side (the remainder tail) ────────────────────────────────────────────

def lower_scalar(node, idx, prefix='_vrp'):
    """Emit ordinary scalar IR computing `node` for element `idx`.

    Replays the ORIGINAL widths and signedness recorded in the leaves, so integer
    promotion and sub-word truncation match the loop this replaces."""
    if isinstance(node, et.Const):
        t = _fresh(prefix + 'k')
        return [IRAssign(t, IRConst(node.value))], t
    if isinstance(node, et.ScalarRef):
        b, t = _fresh(prefix + 'b'), _fresh(prefix + 'v')
        return [emit_array_base(b, node.slot),
                IRLoad(t, b, IRConst(0), elem_bytes=node.elem_bytes,
                       unsigned=node.unsigned)], t
    if isinstance(node, et.ArrayRef):
        pre, off = node.address(idx)
        b, t = _fresh(prefix + 'b'), _fresh(prefix + 'v')
        return list(pre) + [emit_array_base(b, node.slot),
                            IRLoad(t, b, off, elem_bytes=node.elem_bytes,
                                   unsigned=node.unsigned)], t
    if not isinstance(node, et.BinOp):
        raise ValueError(f'unsupported node {type(node).__name__}')
    li, lt = lower_scalar(node.left, idx, prefix)
    ri, rt = lower_scalar(node.right, idx, prefix)
    d = _fresh(prefix + 'r')
    return li + ri + [IRBinOp(d, node.op, lt, rt, unsigned=node.unsigned)], d


def vector_feasible(node, prefix='_probe'):
    """(ok, reason) -- can `node` be emitted as vector IR at all? Used by clients
    to reject a tree at MATCH time instead of discovering it during lowering."""
    if isinstance(node, et.BinOp):
        for side, other in ((node.left, node.right), (node.right, node.left)):
            pass
        l_ok, l_why = vector_feasible(node.left)
        if not l_ok:
            return False, l_why
        r_ok, r_why = vector_feasible(node.right)
        if not r_ok:
            return False, r_why
        if et.is_invariant(node.left) and not et.is_invariant(node.right) \
                and node.op not in _COMMUTATIVE:
            return False, f'scalar-on-left-of-{node.op}'
    return True, None

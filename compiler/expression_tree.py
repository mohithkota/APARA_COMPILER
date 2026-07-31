"""
expression_tree.py -- Reusable Vector Expression Trees (R4.5).

Every vector client until now described its computation as at most
`operand OP operand`. That is enough for assignment, AXPY, GEMM and simple
elementwise kernels, but not for `a+b+c`, `a*b+c`, `(a+b)*c` or anything a
convolution or a general vectorizer would need.

This module is the kernel-independent representation. Nodes are IMMUTABLE and
carry no knowledge of any client:

    Const(value)                       a literal
    ArrayRef(slot, elem_bytes, ...)    one element of a packed array
    ScalarRef(slot, elem_bytes, ...)   a loop-invariant scalar
    BinOp(op, left, right, unsigned)   + - *

`ArrayRef.offset_at(idx)` optionally overrides address formation -- GEMM uses it
to address a row -- and defaults to `idx * elem_bytes` from the array base.

`build_expression` recognises a tree from the IR, using `vector_affine` (and
nothing else) to decide whether a leaf is contiguous, invariant, or unsupported.
There are no kernel-specific subclasses and no client-specific walkers: clients
describe WHAT they compute and `expression_lowering` decides HOW to emit it, both
as vector chunks and as scalar remainder elements.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import Const as IRConst, Temp
from analysis import DefUse
from vector_affine import (LoopAffineContext, classify_access, CONTIGUOUS,
                           INVARIANT)

SUPPORTED_OPS = ('+', '-', '*')
MAX_DEPTH = 4                       # bounded on purpose: small expressions only


def _cname(x):
    return type(x).__name__


# ── immutable nodes ─────────────────────────────────────────────────────────────

class Const:
    __slots__ = ('value',)

    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return f"{self.value}"


class ArrayRef:
    __slots__ = ('slot', 'elem_bytes', 'unsigned', 'offset_at')

    def __init__(self, slot, elem_bytes, unsigned=False, offset_at=None):
        self.slot = slot
        self.elem_bytes = elem_bytes
        self.unsigned = unsigned
        self.offset_at = offset_at

    def address(self, idx):
        if self.offset_at is not None:
            return self.offset_at(idx)
        return [], IRConst(idx * self.elem_bytes)

    def __repr__(self):
        return f"A[{self.slot}]"


class ScalarRef:
    __slots__ = ('slot', 'elem_bytes', 'unsigned')

    def __init__(self, slot, elem_bytes=8, unsigned=False):
        self.slot = slot
        self.elem_bytes = elem_bytes
        self.unsigned = unsigned

    def __repr__(self):
        return f"s[{self.slot}]"


class BinOp:
    __slots__ = ('op', 'left', 'right', 'unsigned')

    def __init__(self, op, left, right, unsigned=False):
        self.op = op
        self.left = left
        self.right = right
        self.unsigned = unsigned

    def __repr__(self):
        return f"({self.left} {self.op} {self.right})"


# ── queries ─────────────────────────────────────────────────────────────────────

def walk(node):
    yield node
    if isinstance(node, BinOp):
        for n in walk(node.left):
            yield n
        for n in walk(node.right):
            yield n


def arrays(node):
    return [n for n in walk(node) if isinstance(n, ArrayRef)]


def depth(node):
    if not isinstance(node, BinOp):
        return 1
    return 1 + max(depth(node.left), depth(node.right))


def is_invariant(node):
    """True if the whole subtree is loop-invariant (no array element in it)."""
    return not arrays(node)


def map_arrays(node, fn):
    """Rebuild the tree with `fn` applied to every ArrayRef. Immutable in, new
    tree out -- GEMM uses this to swap in row-aware addressing."""
    if isinstance(node, ArrayRef):
        return fn(node)
    if isinstance(node, BinOp):
        return BinOp(node.op, map_arrays(node.left, fn),
                     map_arrays(node.right, fn), node.unsigned)
    return node


# ── recognition ─────────────────────────────────────────────────────────────────

class ExprContext:
    """Everything `build_expression` needs about one innermost loop."""
    __slots__ = ('instrs', 'def_map', 'addr_slot', 'region', 'affine', 'elem_bytes')

    def __init__(self, instrs, desc, elem_bytes=None):
        lo, hi = desc.func_slice
        self.instrs = instrs
        self.def_map = DefUse(instrs, lo, hi).single_defs()
        self.addr_slot = {n: instrs[i].fp_offset
                          for n, i in self.def_map.items()
                          if _cname(instrs[i]) == 'IRLoadAddr'}
        region = set()
        for b in desc.body_blocks:
            blk = desc.cfg.blocks[b]
            region.update(range(blk.lo, blk.hi + 1))
        self.region = region
        self.affine = LoopAffineContext(instrs, desc)
        self.elem_bytes = elem_bytes

    def slot_of(self, ins):
        base = getattr(ins, 'base', None)
        return self.addr_slot.get(base.name) if isinstance(base, Temp) else None


def build_expression(value, ctx, depth_left=None):
    """Recognise `value` as an expression tree, or (None, reason).

    Leaves are decided by `vector_affine` only: a CONTIGUOUS access becomes an
    ArrayRef, a loop-invariant slot load becomes a ScalarRef, a literal becomes a
    Const. Anything else -- a strided access, a gather, an unsupported opcode,
    a too-deep tree -- is rejected with a reason."""
    if depth_left is None:
        depth_left = MAX_DEPTH          # read at CALL time so it is tunable
    if depth_left <= 0:
        return None, 'expression-too-deep'
    if isinstance(value, IRConst):
        return Const(value.value), None
    if not isinstance(value, Temp):
        return None, 'operand-not-a-value'
    d = ctx.def_map.get(value.name)
    if d is None:
        return (ScalarRef(0), None) if False else (None, 'operand-has-no-single-def')
    ins = ctx.instrs[d]
    c = _cname(ins)

    if c == 'IRLoad':
        kind = classify_access(ins, ctx.affine).kind
        slot = ctx.slot_of(ins)
        if slot is None:
            return None, 'load-not-from-a-local-slot'
        if kind == CONTIGUOUS:
            if ctx.elem_bytes is not None and ins.elem_bytes != ctx.elem_bytes:
                return None, 'operand-width-mismatch'
            return ArrayRef(slot, ins.elem_bytes,
                            bool(getattr(ins, 'unsigned', False))), None
        if kind == INVARIANT and not ctx.affine.varies(value.name):
            return ScalarRef(slot, ins.elem_bytes,
                             bool(getattr(ins, 'unsigned', False))), None
        return None, f'operand-{kind}'

    if c == 'IRBinOp':
        if ins.op not in SUPPORTED_OPS:
            return None, f'unsupported-operator:{ins.op}'
        l, err = build_expression(ins.left, ctx, depth_left - 1)
        if err:
            return None, err
        r, err = build_expression(ins.right, ctx, depth_left - 1)
        if err:
            return None, err
        return BinOp(ins.op, l, r, bool(getattr(ins, 'unsigned', False))), None

    if c == 'IRAssign':
        return build_expression(ins.src, ctx, depth_left)

    if d not in ctx.region and not ctx.affine.varies(value.name):
        return None, 'invariant-non-slot-value'
    return None, f'unsupported-node:{c}'

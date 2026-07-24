"""
copyprop.py -- Forward Copy Propagation (Milestone 2A).

Rewrites Temp USES of a copied temporary to use the copy's source instead:

        t2 = t1
        x  = t2 + y        -->    x = t1 + y      (the copy `t2 = t1` REMAINS)

This pass ONLY rewrites uses. It never deletes the (now dead) copy -- dead-code
elimination is a separate, later milestone. It performs no coalescing, no
constant propagation, and builds no CFG or dominance information.

Analysis: reuses the shared per-function `DefUse` (compiler/analysis) and the
`basic_blocks` partition from ir_utils -- no bespoke single-def map is rebuilt.
Runs per function slice, to a fixpoint.

--------------------------------------------------------------------------------
Correctness
--------------------------------------------------------------------------------
Replacing a use U of `dst` with `src` preserves semantics when:

    P1: dst's only reaching definition is the copy `dst = src`
        (so the value read at U is exactly the value src had at the copy);
    P2: src's value at U is identical to src's value at the copy.

Rule A establishes P2 because `src` has a SINGLE REACHING DEFINITION -- there is
no other definition of src that could reach U with a different value.

Rule B establishes P2 for a multiply-defined src by restricting to uses in the
SAME straight-line basic block as the copy with NO intervening redefinition of
src between the copy and the use, so along that single straight-line path src is
provably unchanged.

P1 holds for both rules because `dst` is single-def (its one definition is the
copy) and well-formed IR never uses an undefined temp, so that definition
reaches every use.

--------------------------------------------------------------------------------
IR invariants relied upon (current IR)
--------------------------------------------------------------------------------
  * IRCall / IRIndirectCall define ONLY their explicit destination Temp; a call
    does NOT clobber arbitrary Temps.
  * Memory operations (loads/stores) never redefine a Temp other than a load's
    explicit destination.
Therefore the only thing that can change `src`'s value is an instruction that
DEFINES src, which P2 already accounts for -- so propagation across a call is
sound (Rule A). Rule B additionally treats a call as a block boundary, so it is
strictly more conservative and never reasons across one.
"""

import os
import copy as _copy
from ir import Temp
from ir_utils import dest_names, basic_blocks
from analysis import DefUse


# Per IR node type: which attributes hold SOURCE (read) Temp operands. The
# destination is deliberately excluded -- copy propagation rewrites uses only.
# scalar attrs hold a single operand; list attrs hold an operand list.
_SRC_ATTRS = {
    'IRBinOp':        (('left', 'right'), ()),
    'IRUnaryOp':      (('operand',), ()),
    'IRAssign':       (('src',), ()),
    'IRLoad':         (('base', 'offset'), ()),
    'IRLoadWide':     (('base', 'offset'), ()),
    'IRStore':        (('base', 'offset', 'src'), ()),
    'IRStoreWide':    (('base', 'offset'), ('srcs',)),
    'IRGlobalLoad':   (('offset',), ()),
    'IRGlobalStore':  (('offset', 'src'), ()),
    'IRGlobalAddrOf': (('offset',), ()),
    'IRCondJump':     (('left', 'right'), ()),
    'IRReturn':       (('value',), ()),
    'IRCall':         ((), ('args',)),
    'IRIndirectCall': (('func_ptr',), ('args',)),
    'IRCast':         (('src',), ()),
    'IRFsqrt':        (('src',), ()),
    'IRCmov':         (('check', 'src_true', 'src_false'), ()),
    'IRSlice':        (('src',), ()),
    'IRPack':         (('src1', 'src2'), ()),
    'IRVecArith':     (('src1', 'src2'), ()),
    'IRVecDot':       (('src1', 'src2', 'accum'), ()),
    'IRVecDot128':    (('a_lo', 'a_hi', 'b_lo', 'b_hi'), ()),
    'IRVecReduce':    (('src',), ()),
}


def _replace_uses(ins, old_name, new_temp):
    """Return (instr, changed): a SHALLOW COPY of `ins` with every source
    operand named `old_name` replaced by `new_temp`, or `ins` unchanged if it
    reads no such operand. Never mutates `ins` (the original object may be
    shared with the pristine verification IR)."""
    spec = _SRC_ATTRS.get(type(ins).__name__)
    if spec is None:
        return ins, False
    scalars, lists = spec
    ni = None
    for a in scalars:
        v = getattr(ins, a, None)
        if isinstance(v, Temp) and v.name == old_name:
            if ni is None:
                ni = _copy.copy(ins)
            setattr(ni, a, new_temp)
    for a in lists:
        lst = getattr(ins, a, None)
        if lst and any(isinstance(x, Temp) and x.name == old_name for x in lst):
            if ni is None:
                ni = _copy.copy(ins)
            setattr(ni, a, [new_temp if isinstance(x, Temp) and x.name == old_name
                            else x for x in lst])
    return (ni, True) if ni is not None else (ins, False)


def _block_of(blocks, idx):
    for bs, be in blocks:
        if bs <= idx <= be:
            return bs, be
    return idx, idx      # defensive; the partition always covers idx


# ── Rule A: whole-function ─────────────────────────────────────────────────────

def _apply_rule_A(instrs, copy_idx, dst, src_temp, du):
    """src has a single reaching definition (P2 holds everywhere): rewrite every
    use of dst to src. Returns True if any use was rewritten. Kept independent
    of Rule B so each is separately testable."""
    changed = False
    for u in du.use_sites(dst):
        if u == copy_idx:
            continue                         # the copy reads src, not dst
        new_ins, ch = _replace_uses(instrs[u], dst, src_temp)
        if ch:
            instrs[u] = new_ins
            changed = True
    return changed


# ── Rule B: local basic-block ──────────────────────────────────────────────────

def _apply_rule_B(instrs, copy_idx, dst, src_temp, du, blocks):
    """src is multiply-defined, so restrict to uses in the copy's own
    straight-line block with no textual redefinition of src (or dst) strictly
    between the copy and the use -- along that single path P2 holds. Returns
    True if any use was rewritten."""
    bs, be = _block_of(blocks, copy_idx)
    src = src_temp.name
    changed = False
    for u in du.use_sites(dst):
        if not (bs <= u <= be) or u <= copy_idx:
            continue                         # must be later in the same block
        # No redefinition of src or dst on the straight-line path (copy, use).
        # (dst is single-def, so its check can only pass, but we keep it explicit.)
        redefined = any(
            src in dest_names(instrs[j]) or dst in dest_names(instrs[j])
            for j in range(copy_idx + 1, u)
        )
        if redefined:
            continue
        new_ins, ch = _replace_uses(instrs[u], dst, src_temp)
        if ch:
            instrs[u] = new_ins
            changed = True
    return changed


# ── Driver ─────────────────────────────────────────────────────────────────────

def _is_copy(ins):
    """A copy is `IRAssign(dst, src)` with src a Temp (NOT a constant)."""
    return (type(ins).__name__ == 'IRAssign'
            and isinstance(ins.dest, Temp) and isinstance(ins.src, Temp))


def _propagate_slice(instrs, lo, hi):
    """Fixpoint over one function slice [lo, hi]. Rewrites only change operands
    (no insert/delete), so slice bounds and def-counts stay valid across
    rewrites; only use sites move, so DefUse is rebuilt each round."""
    while True:
        du = DefUse(instrs, lo, hi)
        blocks = basic_blocks(instrs, lo, hi)
        changed = False
        for k in range(lo, hi + 1):
            ins = instrs[k]
            if not _is_copy(ins):
                continue
            dst, src = ins.dest.name, ins.src.name
            if dst == src:
                continue                     # self-copy: nothing to do
            if not du.is_single_def(dst):
                continue                     # dst's sole def must be this copy (P1)
            if du.is_multi_def(src):
                changed |= _apply_rule_B(instrs, k, dst, ins.src, du, blocks)
            else:
                changed |= _apply_rule_A(instrs, k, dst, ins.src, du)
        if not changed:
            break


def copy_propagate(instrs):
    """Forward copy propagation over every function slice, to a fixpoint.
    Returns a NEW list; original instruction objects are never mutated (rewrites
    produce shallow copies). Dead copies are intentionally left in place."""
    if os.environ.get('APARA_NO_COPYPROP'):
        return instrs
    from ir_utils import func_slices
    out = list(instrs)
    for lo, hi in func_slices(out):
        _propagate_slice(out, lo, hi)
    return out

"""
coalesce.py -- Copy Coalescing (Milestone 2B).

Complements forward copy propagation (Milestone 2A): where propagation rewrote
USES of a copy, coalescing rewrites the DEFINITION that feeds a copy and then
removes the copy.

        t_prod = a + b                 t_dst = a + b
        t_dst  = t_prod        -->     (copy removed; t_prod disappears)

This is NOT general dead-code elimination: the pass removes ONLY the copy it
actively transforms, and never searches for unrelated dead instructions.
General DCE remains Milestone 3.

Analysis: reuses the shared per-function `DefUse` (compiler/analysis) and the
`basic_blocks` partition from ir_utils -- no bespoke analysis, no CFG, no
dominance. Runs per function slice, to a fixpoint. Rewrites produce shallow
copies (original IR objects are never mutated); only the local working list is
mutated by deleting coalesced copies.

--------------------------------------------------------------------------------
Correctness
--------------------------------------------------------------------------------
For a copy c: `IRAssign(dst, src)` whose source is produced by p, retargeting p
to write `dst` and deleting c preserves semantics because:

  1. The producer p already computes exactly the value the copy writes into dst.
  2. dst is neither read nor written on the straight-line path strictly between
     p and c, so no instruction there observed dst's old value or overwrote it;
     every later reader of dst therefore sees the same value the deleted copy
     would have written.
  3. src has no user other than the copy, so retargeting p from src to dst loses
     no other reader of src.
  4. p and c lie in one straight-line basic block, so p provably reaches c along
     a single path (no CFG/dominance reasoning required).

Benefit: it shortens the (often loop-carried) dependence chain by removing an
intermediate move, which can let the scheduler pack more tightly.
"""

import os
import copy as _copy
from ir import Temp
from ir_utils import dest_names, src_names, func_slices, basic_blocks
from analysis import DefUse


# Producers we may retarget: side-effect-free, single-destination, pure
# value-producing instructions. Loads are pure reads in the current fault-free
# IR (no traps, no volatile), so they qualify. Deliberately EXCLUDES stores,
# calls, control transfers, wide/multi-destination ops, and anything with side
# effects. Conservative by design.
_COALESCEABLE_PRODUCERS = frozenset({
    'IRAssign', 'IRUnaryOp', 'IRBinOp', 'IRCast',
    'IRLoad', 'IRGlobalLoad', 'IRLoadAddr', 'IRGlobalAddrOf',
    'IRFsqrt', 'IRSlice', 'IRPack', 'IRVecArith', 'IRVecReduce',
})


def _is_copy(ins):
    """A copy is `IRAssign(dst, src)` with src a Temp (not a constant)."""
    return (type(ins).__name__ == 'IRAssign'
            and isinstance(ins.dest, Temp) and isinstance(ins.src, Temp))


def _block_of(blocks, idx):
    for bs, be in blocks:
        if bs <= idx <= be:
            return bs, be
    return idx, idx


def _coalesce_one_in_slice(instrs, lo, hi):
    """Find and apply ONE coalesce in [lo, hi]. Returns True (and mutates
    `instrs`: retargeted producer + deleted copy) if one was applied, else
    False. Applies one at a time because deleting the copy shifts indices."""
    du = DefUse(instrs, lo, hi)
    blocks = basic_blocks(instrs, lo, hi)

    for c in range(lo, hi + 1):
        ins = instrs[c]
        if not _is_copy(ins):
            continue
        dst, src = ins.dest, ins.src
        if dst.name == src.name:
            continue                                    # self-copy

        # Cond 1: src has exactly one reaching definition, its producer p.
        p = du.single_def(src.name)
        if p is None or p >= c:
            continue

        # Cond 2: src's only use is this copy.
        if du.use_sites(src.name) != [c]:
            continue

        # Cond 5: producer is a pure, single-destination value producer whose
        # destination really is src.
        prod = instrs[p]
        if type(prod).__name__ not in _COALESCEABLE_PRODUCERS:
            continue
        pdest = getattr(prod, 'dest', None)
        if not isinstance(pdest, Temp) or pdest.name != src.name:
            continue

        # Cond 3: producer and copy are in the same straight-line block.
        bs, be = _block_of(blocks, c)
        if not (bs <= p <= be):
            continue

        # Cond 4: dst is neither used nor defined in the OPEN interval (p, c).
        if any(dst.name in dest_names(instrs[j]) or dst.name in src_names(instrs[j])
               for j in range(p + 1, c)):
            continue

        # Transform: retarget the producer's destination src -> dst (on a shallow
        # copy so the original object is untouched), then delete the copy.
        new_prod = _copy.copy(prod)
        new_prod.dest = dst
        instrs[p] = new_prod
        del instrs[c]
        return True

    return False


def copy_coalesce(instrs):
    """Copy coalescing over every function slice, to a fixpoint. Returns a NEW
    list; original instruction objects are never mutated. Removes only the
    copies it coalesces."""
    if os.environ.get('APARA_NO_COALESCE'):
        return instrs
    instrs = list(instrs)
    changed = True
    while changed:
        changed = False
        # Recompute slices each round: deleting a copy shifts later indices.
        for lo, hi in func_slices(instrs):
            if _coalesce_one_in_slice(instrs, lo, hi):
                changed = True
                break
    return instrs

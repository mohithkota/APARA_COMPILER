"""
dce.py -- Global Dead Code Elimination (Milestone 3).

Removes instructions whose computed value is never used and which have no
observable side effect. This is the general cleanup that reclaims the dead
copies forward propagation (2A) bypassed and the dead temporaries coalescing
(2B) exposed -- with no special-case logic; it simply removes anything provably
useless, to a fixpoint (so chains of dead computations peel away layer by layer).

Analysis: reuses the shared per-function `DefUse` (compiler/analysis) -- no CFG,
no dominance, no liveness dataflow. Operates per function slice; rewrites produce
a new list (original instruction objects are never mutated).

--------------------------------------------------------------------------------
Correctness
--------------------------------------------------------------------------------
An instruction may be removed only when BOTH hold:
  1. it has NO observable side effect, and
  2. the value it produces is never used (its single destination Temp has zero
     use sites in its function).
Removing such an instruction cannot change program behaviour: nothing observes
its result, and it changes no memory / control / call state. Everything else is
kept, regardless of uses.

Only PURE, SINGLE-DESTINATION value producers are eligible (see _PURE). Loads
are treated as pure reads -- correct for the current fault-free IR (no traps, no
volatile / MMIO). Instructions with side effects or observable behaviour are
NEVER removed, whatever their uses: stores, calls, returns, jumps, branches,
labels, halts, wide/multi-destination ops, function/global declarations, nops.
"""

import os
from ir_utils import dest_names, func_slices
from analysis import DefUse


# Pure, single-destination value producers -- removable when their result is
# unused. Anything NOT listed here is kept regardless of uses (conservative).
# Mirrors the set proven safe by IVSR's internal dead-temp elimination.
_PURE = frozenset({
    'IRBinOp', 'IRUnaryOp', 'IRAssign', 'IRCast',
    'IRLoad', 'IRGlobalLoad', 'IRLoadAddr', 'IRGlobalAddrOf',
    'IRFsqrt', 'IRSlice', 'IRPack', 'IRFuncAddr', 'IRVaStart',
    'IRVecArith', 'IRVecDot', 'IRVecDot128', 'IRVecReduce',
})


def dead_code_eliminate(instrs):
    """Remove pure, single-destination instructions whose result is unused, to a
    fixpoint, per function slice. Returns a NEW list (originals never mutated).

    Each round: build DefUse per slice, mark every eligible instruction whose
    one destination has zero uses, then delete them all and repeat. Deleting a
    layer can make its inputs' definitions dead, so the loop continues until a
    round removes nothing -- this is how chains of dead computations collapse."""
    if os.environ.get('APARA_NO_DCE'):
        return instrs
    instrs = list(instrs)
    while True:
        dead = set()
        for lo, hi in func_slices(instrs):
            du = DefUse(instrs, lo, hi)
            for k in range(lo, hi + 1):
                ins = instrs[k]
                if type(ins).__name__ not in _PURE:
                    continue                       # side-effecting / non-producer: keep
                dests = dest_names(ins)
                if len(dests) != 1:
                    continue                       # must define exactly one Temp
                if du.use_sites(dests[0]):
                    continue                       # result is used: keep
                dead.add(k)
        if not dead:
            break
        instrs = [ins for k, ins in enumerate(instrs) if k not in dead]
    return instrs

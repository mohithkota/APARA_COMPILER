"""
vector_size_probe.py -- Post-optimizer size probe for vector realisations (R4.2.6).

R4.2.5 chose between vector realisations (unrolled vs compact) by compiling each
candidate with `CodeGen` + `bundle_mcode` alone. That measured the vectorized IR
BEFORE the scalar optimizer, software pipelining and superblock scheduling ran --
and those passes systematically favour straight-line code (superblock merges
regions; the bundler packs independent unrolled chunks 8 wide). The ranking could
therefore flip after lowering had already committed.

The R4.2.5 delivery documented one measured case: a 4-loop program with one
8-chunk vectorized loop finished at 67 bundles unrolled versus 69 compact, even
though the compact form had 34 FEWER instructions (119 vs 153). The probe never
saw the passes that produced that inversion.

THIS MODULE CLOSES THAT GAP by running the candidate through the SAME sequence
production uses after vectorization, then bundling:

    scalar optimizer tier 1   IVSR -> strength-reduce -> LICM -> loop-reg
                              -> copy-prop/coalesce/DCE/SCCP/GVN/mem2reg/LICM
    superblock scheduling     R3.2 region formation + rescheduling
    codegen + bundler

Tier 1 is the tier production selects for the overwhelming majority of programs
(it is tried first and only rejected on a spill), so it is the right predictor.
If a candidate spills under tier 1, that is reported as a spill exactly as the
production tier selector would see it.

EVERYTHING IS REUSED, NOTHING REIMPLEMENTED: the passes are imported from the
same modules `compile_c_to_mcode` imports them from, so the probe cannot drift
from production. Any failure falls back to the cheap R4.2.5 probe rather than
losing the candidate -- a probe is a size heuristic, and the pipeline's own
spill/differential gates remain the correctness authority.

`APARA_VECTOR_FAST_PROBE=1` reverts to the cheap pre-optimizer probe.
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _optimize_like_production(ir):
    """Apply production's tier-1 scalar optimization to a function slice.

    Mirrors `compile_c_to_mcode`'s first tier exactly, importing every pass from
    the module compiler.py imports it from."""
    from strength_reduce import strength_reduce
    from loopopt.pipeline import (induction_strength_reduce,
                                  loop_invariant_code_motion)
    from licm import hoist_loop_invariants
    from loop_reg import promote_loop_counters
    from copyprop import copy_propagate
    from coalesce import copy_coalesce
    from dce import dead_code_eliminate
    from sccp import sparse_conditional_constant_propagation
    from gvn import global_value_numbering
    from mem2reg import mem2reg

    def _clean(x):
        return dead_code_eliminate(copy_coalesce(copy_propagate(x)))

    x = induction_strength_reduce(list(ir))
    x = strength_reduce(x)[0]
    x = hoist_loop_invariants(x)
    x = promote_loop_counters(x)
    x = _clean(x)
    x = dead_code_eliminate(sparse_conditional_constant_propagation(x))
    x = global_value_numbering(x)
    x = mem2reg(x)
    x = loop_invariant_code_motion(x)
    return _clean(x)


def _superblock(ir, global_base):
    """Apply R3.2 superblock scheduling as production does. Best-effort: this is
    a size probe, so a failure just means the un-superblocked size is used."""
    try:
        from trace_scheduler import apply_superblock_scheduling
        sb_ir, sb_sum = apply_superblock_scheduling(ir, global_base=global_base,
                                                    verbose=False)
        if sb_sum.accepted:
            return sb_ir
    except Exception:
        pass
    return ir


def probe_bundles(ir, global_base):
    """(bundle_count, spilled?) for a candidate AS PRODUCTION WOULD BUILD IT.

    Returns (None, True) if it does not compile or it spills -- the same contract
    as the cheap probe, so callers need no special-casing."""
    from vector_pipeline import _bundles
    if os.environ.get('APARA_VECTOR_FAST_PROBE'):
        return _bundles(ir, global_base)
    try:
        opt = _optimize_like_production(copy.deepcopy(ir))
        opt = _superblock(opt, global_base)
        return _bundles(opt, global_base)
    except Exception:
        # Never lose a candidate to a probe failure: fall back to the cheap
        # pre-optimizer measurement, which is what R4.2.5 used throughout.
        return _bundles(ir, global_base)

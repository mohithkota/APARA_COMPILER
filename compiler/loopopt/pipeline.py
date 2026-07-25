"""
pipeline.py -- production integration of the migrated loop passes (Milestone M10).

M10 makes the framework LoopTransform implementations the CANONICAL execution path
of the compiler's optimization pipeline. It is an INTEGRATION milestone only: no
optimization behaviour changes, no pass is added / removed / reordered, and the
legacy modules (ivsr.py, licm2.py, licm.py, loop_reg.py) are retained untouched as
specifications and regression references.

This module exposes DROP-IN replacements -- identical name, signature and return
contract -- for the two legacy loop passes that (a) appear in the production
pipeline and (b) have a framework migration:

    induction_strength_reduce(instrs)   -> M9 LoopIVSR  (loopopt.loop_ivsr.ivsr_module)
    loop_invariant_code_motion(instrs)  -> M8 LoopLICM  (loopopt.loop_licm.licm_module)

compiler.py imports these two names FROM HERE instead of from ivsr / licm2, so
every production loop transformation now runs through the framework
(LoopTransform + MutationTransaction + verification + rollback + shared analyses /
legality / descriptors). The behavioural cross-checks in ivsr_crosscheck.py (M9),
licm_crosscheck.py (M8) and pipeline_crosscheck.py (M10) prove the swapped
pipeline produces instruction-for-instruction identical IR.

Passes NOT covered here are OUT OF M10's integration scope because they have no
framework migration yet: licm.hoist_loop_invariants (the older ad-hoc load/address
LICM -- distinct from licm2) and loop_reg.promote_loop_counters. They remain the
legacy implementations in the pipeline, unchanged. LoopRotation (M6) is a
framework transform but is NOT part of the production pipeline (it never was), so
M10 does not insert it (that would be adding a pass).

Each adapter preserves the legacy pass's exact contract, including that the
CALLER's list is never mutated in place (the pipeline relies on its input IR
staying pristine): the framework passes mutate through the transaction in place,
so each adapter works on a private shallow copy and returns the result. The
module-global fresh-temp counter (ivsr._iv_n) is deliberately NOT reset here --
the framework path advances it identically to the legacy path (proven byte-equal
in M9), so tier-to-tier temp numbering across the pipeline is unchanged.
"""

import os

from .loop_ivsr import ivsr_module
from .loop_licm import licm_module


def induction_strength_reduce(instrs):
    """Drop-in for ivsr.induction_strength_reduce, executed through the M9
    LoopIVSR framework path. Returns the transformed instruction list; the input
    list is not mutated (matching the legacy contract)."""
    work = list(instrs)                          # protect the caller's list
    result, _stats, _report = ivsr_module(work)
    return result


def loop_invariant_code_motion(instrs):
    """Drop-in for licm2.loop_invariant_code_motion, executed through the M8
    LoopLICM framework path. Preserves licm2's exact opt-in gating so default
    production behaviour (APARA_LICM unset) is unchanged -- a no-op returning the
    input object -- while APARA_LICM enables the framework LICM (byte-identical to
    licm2 by M8's cross-check). APARA_NO_LICM force-disables, as in licm2."""
    if not os.environ.get('APARA_LICM') or os.environ.get('APARA_NO_LICM'):
        return instrs                            # opt-in gate: identical to licm2
    work = list(instrs)                          # protect the caller's list
    licm_module(work)                            # mutates `work` in place
    return work

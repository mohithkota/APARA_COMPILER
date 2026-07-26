"""
production_swp.py -- Production Software Pipelining Integration (Milestone R3.1).

Wires the ALREADY-VALIDATED software-pipelining framework (R2.5 modulo scheduling
-> R2.6 register promotion -> R2.7 register-aware SWP -> R2.8 modulo variable
expansion / compact kernel) into the production `compile_c_to_mcode()` path,
gated by the R3.0 oracle's profitability model and by the production compiler's
own zero-spill criterion, with per-function rollback.

NOTHING here is a new scheduler. It is purely integration + profitability +
validation + rollback:

  * PROFITABILITY -- the R3.0 oracle (`oracle_ilp.analyze_module`) says which
    innermost loops have enough exposed ILP that software pipelining is the
    top-ranked opportunity with an expected IPB gain over a threshold.
  * TRANSFORM -- the R2.8 driver (`pipeline_mve_module`) runs R2.5->R2.8 exactly
    as implemented (its own eligibility, scheduling, realisation, and internal
    structural/differential/compile gates + the codegen live-range invariant).
  * VALIDATION -- each pipelined FUNCTION is re-validated end-to-end against the
    original memory-backed IR with the existing clean-slot multi-seed differential
    (`loop_promote._promote_diff`); production REQUIRES a definite 'match' (a
    stricter bar than the standalone gate, which also accepts a clean-slot
    'unsupported' proof) before it will change production output.
  * SPILL AWARENESS -- a pipelined function is spliced into the production-
    optimized IR only if the whole program still compiles with ZERO spills, the
    exact criterion the production tier selector already uses. Any spill increase
    or compile failure rolls the function back to the production form.

Because a rolled-back or non-profitable loop leaves the production-optimized slice
untouched, generated output is BYTE-IDENTICAL to today's compiler except on loops
that were pipelined AND passed every gate. `APARA_NO_SWP=1` disables the pass.

Frozen and merely CONSUMED (never modified): the DependenceGraph, the
disambiguator, R2.5-R2.8, the oracle, the bundler, the register allocator, the
existing validator/rollback, and the spill-tier fallback.
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir_utils import func_slices                                      # noqa: E402
from loopopt.oracle_ilp import analyze_module as oracle_analyze_module  # noqa: E402
from loopopt.pipeline_mve import pipeline_mve_module                  # noqa: E402
from loopopt.loop_promote import _promote_diff                        # noqa: E402


# default profitability threshold: expected IPB gain from software pipelining
_DEFAULT_THRESHOLD = float(os.environ.get('APARA_SWP_THRESHOLD', '0.5'))


class ProfitabilityRecord:
    """Per-loop record for the profitability report."""
    __slots__ = ('func', 'header', 'label', 'original_ipb', 'oracle_ipb',
                 'expected_gain', 'form', 'accepted', 'rollback_reason')

    def __init__(self, func, header, label):
        self.func = func
        self.header = header
        self.label = label
        self.original_ipb = 0.0
        self.oracle_ipb = 0.0
        self.expected_gain = 0.0
        self.form = None
        self.accepted = False
        self.rollback_reason = None

    def __repr__(self):
        if self.accepted:
            return (f"SWP[{self.func}:'{self.label}'] {self.form}  "
                    f"IPB {self.original_ipb:.2f}->~{self.oracle_ipb:.2f} "
                    f"(+{self.expected_gain:.2f})  PIPELINED")
        return (f"SWP[{self.func}:'{self.label}'] rolled back: "
                f"{self.rollback_reason}")


class SWPSummary:
    def __init__(self):
        self.profitable_funcs = 0
        self.attempted = 0
        self.pipelined = 0
        self.rolled_back = 0
        self.static_before = self.static_after = 0
        self.bundles_before = self.bundles_after = 0
        self.spills_before = self.spills_after = 0
        self.changed = False


# ── helpers (reuse; no new analysis) ───────────────────────────────────────────

def _slice_bounds(ir, fname):
    for (lo, hi) in func_slices(ir):
        if getattr(ir[lo], 'name', None) == fname:
            return lo, hi
    return None


def _replace_function(ir, fname, new_slice):
    """Return `ir` with the function named `fname` replaced by `new_slice`."""
    b = _slice_bounds(ir, fname)
    if b is None:
        return ir
    lo, hi = b
    return ir[:lo] + list(new_slice) + ir[hi + 1:]


def _prog_metrics(ir, global_base):
    """(compiled_ok, spilled, static_instr, bundles) for a whole program. Reuses
    the production CodeGen + bundler; never mutates `ir`."""
    try:
        from codegen import CodeGen
        from bundler import bundle_mcode
        cg = CodeGen(global_base=global_base)
        body = cg.generate(copy.deepcopy(ir), global_base=global_base)
        _m, n, b = bundle_mcode(body, schedule=True)
        return True, bool(cg.spilled), n, b
    except Exception:
        return False, True, 0, 0


# ── profitability (the R3.0 oracle) ────────────────────────────────────────────

def profitable_functions(ir0, threshold):
    """Functions whose innermost loops the oracle marks software-pipelining-
    profitable (SWP is the top opportunity and its estimated IPB gain >=
    threshold). Returns {func: (best_gain, LoopILP)}. Pure analysis."""
    out = {}
    for r in oracle_analyze_module(ir0):
        gain = dict(r.opportunities).get('software-pipelining', 0.0)
        if r.top_opportunity == 'software-pipelining' and gain >= threshold:
            prev = out.get(r.func)
            if prev is None or gain > prev[0]:
                out[r.func] = (gain, r)
    return out


# ── the integration driver ─────────────────────────────────────────────────────

def apply_production_swp(ir0, prod_ir, global_base=0x400,
                         threshold=_DEFAULT_THRESHOLD, verbose=False):
    """Apply software pipelining to the production-optimized IR where the oracle
    says it is profitable and every gate passes. Returns (final_ir, [records],
    SWPSummary). `final_ir is prod_ir` (same object) when nothing changed, so the
    caller can cheaply detect a no-op and keep the proven output.

    Guarantees: a function is changed only if (a) the oracle marks it profitable,
    (b) R2.8 commits a pipeline for it, (c) the clean-slot multi-seed differential
    against the ORIGINAL confirms a definite 'match', and (d) the whole program
    still compiles with ZERO register spills. Otherwise the production slice is
    kept verbatim."""
    summary = SWPSummary()
    records = []

    prof = profitable_functions(ir0, threshold)
    summary.profitable_funcs = len(prof)
    if not prof:
        return prod_ir, records, summary            # no-op, identical output

    # run the frozen R2.5->R2.8 pipeline once (its own gates already validated it)
    swp_ir, _mstats, mreports = pipeline_mve_module(ir0)
    committed = {}
    for rep in mreports:
        if rep.committed:
            committed.setdefault(rep.func, rep)

    candidates = [f for f in prof if f in committed]
    if not candidates:
        return prod_ir, records, summary

    # baseline program metrics (the proven output)
    ok0, sp0, st0, bn0 = _prog_metrics(prod_ir, global_base)
    summary.spills_before = int(sp0)
    summary.static_before = st0
    summary.bundles_before = bn0

    working = prod_ir
    # attack the highest-expected-gain functions first
    candidates.sort(key=lambda f: -prof[f][0])
    for fname in candidates:
        gain, loop = prof[fname]
        rep = committed[fname]
        rec = ProfitabilityRecord(fname, loop.header, loop.label)
        rec.original_ipb = loop.achieved_ipb
        rec.oracle_ipb = loop.theoretical_ipb
        rec.expected_gain = gain
        rec.form = ('compact-' if getattr(rep, 'compacted', False) else 'full-') \
            + str(getattr(rep, 'form', '?'))
        summary.attempted += 1

        b = _slice_bounds(ir0, fname)
        if b is None:
            rec.rollback_reason = 'slice-not-found'
            records.append(rec)
            summary.rolled_back += 1
            continue
        lo, hi = b

        # VALIDATION: require a DEFINITE match against the original (stricter than
        # the standalone gate, which also accepts a clean-slot 'unsupported' proof)
        verdict = _promote_diff(ir0, swp_ir, lo, hi)
        if verdict != 'match':
            rec.rollback_reason = f'differential-{verdict}'
            records.append(rec)
            summary.rolled_back += 1
            continue

        swp_slice = swp_ir[_slice_bounds(swp_ir, fname)[0]:
                           _slice_bounds(swp_ir, fname)[1] + 1]
        candidate = _replace_function(working, fname, swp_slice)

        # SPILL AWARENESS: only accept if the whole program still has ZERO spills
        okc, spc, stc, bnc = _prog_metrics(candidate, global_base)
        if not okc:
            rec.rollback_reason = 'compile-failed'
            records.append(rec)
            summary.rolled_back += 1
            continue
        if spc:
            rec.rollback_reason = 'spill-increase'
            records.append(rec)
            summary.rolled_back += 1
            continue

        # accept: keep this splice and carry it forward
        working = candidate
        rec.accepted = True
        records.append(rec)
        summary.pipelined += 1
        if verbose:
            print(f"[swp] pipelined {fname}: {rec}")

    if working is prod_ir or summary.pipelined == 0:
        return prod_ir, records, summary            # nothing accepted -> identical

    ok1, sp1, st1, bn1 = _prog_metrics(working, global_base)
    summary.spills_after = int(sp1)
    summary.static_after = st1
    summary.bundles_after = bn1
    summary.changed = True
    return working, records, summary


def format_profitability(records, summary):
    """Human-readable profitability report."""
    L = []
    L.append(f"  software pipelining: {summary.pipelined} pipelined, "
             f"{summary.rolled_back} rolled back "
             f"(of {summary.profitable_funcs} profitable functions)")
    for r in records:
        tag = "PIPELINED" if r.accepted else f"rollback:{r.rollback_reason}"
        L.append(f"    {r.func:16.16} '{str(r.label):8.8}'  "
                 f"IPB {r.original_ipb:.2f} -> oracle {r.oracle_ipb:.2f} "
                 f"(exp +{r.expected_gain:.2f})  {r.form or '-':16.16} {tag}")
    if summary.changed:
        L.append(f"    program: static {summary.static_before}->{summary.static_after}"
                 f"  bundles {summary.bundles_before}->{summary.bundles_after}"
                 f"  spills {summary.spills_before}->{summary.spills_after}")
    return "\n".join(L)

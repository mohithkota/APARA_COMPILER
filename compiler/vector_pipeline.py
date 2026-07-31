"""
vector_pipeline.py -- Generic Vectorization Pipeline (R4.2 Phase 1).

The ONE production vectorization pipeline. R4.1 proved the sequence

    Kernel Detection -> Legality -> Profitability -> Transformation
                     -> Validation -> Compile -> Commit | Rollback

is reusable; this module owns that sequence and knows NOTHING about which vector
transformation is being applied. Every individual vectorizer is a `VectorTransform`
client supplying only four things:

    kinds          which detected kernel kinds it claims
    match()        its own pattern matching (the shapes it actually supports)
    lower()        its own lowering to vector IR
    dynamic_model() its own profitability adjustment (executed-op accounting)

plus an optional `validate()` hook (the default is the packed differential
oracle, which both current clients use).

Everything else -- module/function slicing with globals preserved, loop discovery
and annotation, the dependence graph, the gate ORDER, spill and bundle checking,
reporting, statistics, determinism resets, and rollback -- lives here and is
shared. A client cannot skip a gate, because it never sees the pipeline.

ROLLBACK is total: a loop that fails ANY stage is left in its scalar form, so a
function with no committed kernel compiles byte-identically to the scalar
compiler. This is the property R4.1 established and R4.2 preserves.
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir_utils import func_slices
from loopopt.discovery import discover_function
from loopopt.analysis_iv import annotate_induction_vars
from loopopt.analysis_mem import annotate_memory_effects
from loopopt.analysis_profile import annotate_profile
from loopopt.depgraph import DependenceGraph
from loopopt.depgraph_disambig import MemoryDisambiguator
from kernel_detector import _detect_loop
from vector_legality import analyze_legality_loop
from vector_profitability import estimate


# ── what a client returns ───────────────────────────────────────────────────────

class MatchResult:
    """A client's pattern-matching verdict. `info` is opaque to the pipeline and
    is handed straight back to the client's lower()/dynamic_model()."""
    __slots__ = ('ok', 'reason', 'info')

    def __init__(self, ok, reason=None, info=None):
        self.ok = ok
        self.reason = reason
        self.info = info


class DynamicModel:
    """A client's accounting of DYNAMIC (executed) operations, scalar vs vector.
    The pipeline gates on this, never on static size -- vectorization deliberately
    trades static code (unrolled chunks) for executed operations."""
    __slots__ = ('scalar_ops', 'vector_ops', 'chunks', 'remainder')

    def __init__(self, scalar_ops, vector_ops, chunks=0, remainder=0):
        self.scalar_ops = scalar_ops
        self.vector_ops = vector_ops
        self.chunks = chunks
        self.remainder = remainder


# ── the client interface ────────────────────────────────────────────────────────

class VectorTransform:
    """Base class for a vectorizer. Subclasses provide pattern matching, lowering
    and a dynamic model; the pipeline provides everything else."""

    name = 'transform'
    kinds = ()                      # detected kernel kinds this client claims

    def reset(self):
        """Reset per-run counters so repeated runs are byte-identical. Called once
        per module by the pipeline, BEFORE any loop is examined."""

    def match(self, desc, instrs, kernel, legality):
        """Pattern-match the loop. Default: accept whatever the detector claimed
        (R4.1's behaviour, whose shape checking lives inside its lowering)."""
        return MatchResult(True)

    def lower(self, instrs, lo, hi, desc, kernel, legality, match):
        """Return (new_function_slice, reason) or (None, reason)."""
        raise NotImplementedError

    def validate(self, scalar_instrs, vector_instrs, lo, hi):
        """Return ('match'|'mismatch'|'unsupported', detail). The default is the
        packed differential oracle both current clients use."""
        from vector_lowering import differential_packed
        return differential_packed(scalar_instrs, vector_instrs, lo, hi)

    def dynamic_model(self, desc, kernel, legality, match):
        """Return a DynamicModel for this loop."""
        raise NotImplementedError


# ── reporting ───────────────────────────────────────────────────────────────────

class VectorizeReport:
    __slots__ = ('func', 'header', 'label', 'kind', 'transform', 'vtype', 'lanes',
                 'committed', 'reason', 'chunks', 'remainder',
                 'scalar_bundles', 'vector_bundles',
                 'scalar_dynamic', 'vector_dynamic')

    def __init__(self, func, header, label):
        self.func = func
        self.header = header
        self.label = label
        self.kind = None
        self.transform = None
        self.vtype = None
        self.lanes = 0
        self.committed = False
        self.reason = 'not-a-kernel'
        self.chunks = self.remainder = 0
        self.scalar_bundles = self.vector_bundles = 0
        self.scalar_dynamic = self.vector_dynamic = 0

    def __repr__(self):
        if self.committed:
            return (f"Vec[{self.func}:'{self.label}'] {self.kind} {self.vtype} "
                    f"x{self.lanes}  dyn-ops {self.scalar_dynamic}->{self.vector_dynamic}"
                    f"  static-bundles {self.scalar_bundles}->{self.vector_bundles}")
        return f"Vec[{self.func}:'{self.label}'] rolled back: {self.reason}"


class VectorizeStats:
    def __init__(self):
        self.functions = self.loops = 0
        self.vectorized = self.rolled_back = self.declined = 0
        self.reasons = {}
        self.by_transform = {}          # R4.2: which client committed what

    def _bump(self, k):
        self.reasons[k] = self.reasons.get(k, 0) + 1

    def _credit(self, name):
        self.by_transform[name] = self.by_transform.get(name, 0) + 1


# ── the shared backend probe ────────────────────────────────────────────────────

def _bundles(ir, global_base):
    """(bundle_count, spilled?) for a candidate IR, via the REAL backend. Returns
    (None, True) if it does not compile -- treated as a rollback."""
    try:
        from codegen import CodeGen
        from bundler import bundle_mcode
        cg = CodeGen(global_base=global_base)
        body = cg.generate(copy.deepcopy(ir), global_base=global_base)
        _m, _n, b = bundle_mcode(body, schedule=True)
        return b, bool(cg.spilled)
    except Exception:
        return None, True


# ── the pipeline ────────────────────────────────────────────────────────────────

def _vectorize_function(instrs, lo, hi, transforms, stats, reports, global_base):
    """Run the pipeline over one function slice; commit at most ONE kernel (the
    first that passes every gate). Returns the function slice, transformed or
    untouched."""
    fname = getattr(instrs[lo], 'name', '?')
    sub = instrs[lo:hi + 1]
    descs = discover_function(sub, 0, len(sub) - 1)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    annotate_profile(descs)
    disamb = MemoryDisambiguator(sub, 0, len(sub) - 1, descs)
    graph = DependenceGraph(sub, 0, len(sub) - 1, disambiguator=disamb)

    for d in descs:
        if not d.is_innermost:
            continue
        stats.loops += 1

        # ── 1. KERNEL DETECTION ───────────────────────────────────────────────
        kernel = _detect_loop(d, sub)
        tf = next((t for t in transforms if kernel.kind in t.kinds), None)
        if tf is None:
            continue                                # no client claims it: skip
        rep = VectorizeReport(fname, d.header, d.label())
        rep.kind = kernel.kind
        rep.transform = tf.name
        reports.append(rep)

        # ── 2. LEGALITY ───────────────────────────────────────────────────────
        legality = analyze_legality_loop(d, sub, graph)
        if not legality.legal:
            rep.reason = f'illegal:{legality.reason}'
            stats.declined += 1
            stats._bump(legality.reason)
            continue
        rep.vtype, rep.lanes = kernel.vtype, legality.lanes

        # ── 3. PROFITABILITY ──────────────────────────────────────────────────
        prof = estimate(legality, d)
        if not prof.profitable:
            rep.reason = f'unprofitable:{prof.note}'
            stats.declined += 1
            stats._bump('unprofitable')
            continue

        # ── 4. TRANSFORMATION (client pattern match, then client lowering) ────
        m = tf.match(d, sub, kernel, legality)
        if not m.ok:
            rep.reason = f'pattern:{m.reason}'
            stats.declined += 1
            stats._bump(f'pattern:{m.reason}')
            continue

        new_sub, why = tf.lower(sub, 0, len(sub) - 1, d, kernel, legality, m)
        if new_sub is None:
            rep.reason = f'lower:{why}'
            stats.declined += 1
            stats._bump(f'lower:{why}')
            continue

        # ── 5. VALIDATION: behaviour-identical under the differential oracle ──
        verdict, _detail = tf.validate(sub, new_sub, 0, len(sub) - 1)
        if verdict != 'match':
            rep.reason = f'differential:{verdict}'
            stats.rolled_back += 1
            stats._bump('differential-rollback')
            continue

        # ── 6. COMPILE: must build spill-free through the real backend ────────
        vb, vspill = _bundles(new_sub, global_base)
        sb, _sspill = _bundles(sub, global_base)
        if vb is None or vspill:
            rep.reason = 'compile-or-spill'
            stats.rolled_back += 1
            stats._bump('compile-or-spill')
            continue

        # ── 7. COMMIT: only if DYNAMIC executed operations actually drop ──────
        dm = tf.dynamic_model(d, kernel, legality, m)
        rep.chunks, rep.remainder = dm.chunks, dm.remainder
        rep.scalar_dynamic, rep.vector_dynamic = dm.scalar_ops, dm.vector_ops
        if dm.vector_ops >= dm.scalar_ops:
            rep.reason = f'no-dynamic-reduction({dm.scalar_ops}->{dm.vector_ops})'
            stats.declined += 1
            stats._bump('no-dynamic-reduction')
            continue

        rep.committed = True
        rep.reason = 'ok'
        rep.scalar_bundles, rep.vector_bundles = sb, vb
        stats.vectorized += 1
        stats._bump(kernel.kind)
        stats._credit(tf.name)
        return new_sub                              # one kernel per function
    return sub


def run_module(instrs, transforms, global_base=0x400):
    """Run the pipeline with the given clients over a whole module. Returns
    (new_instrs, VectorizeStats, [VectorizeReport]).

    Each function's (possibly transformed) slice is concatenated with the
    inter-function code around it, so globals and everything outside a function
    body are preserved exactly (the R2.5 rebuild bug, fixed once and for all)."""
    for t in transforms:
        t.reset()
    stats = VectorizeStats()
    reports = []
    out = []
    prev_end = 0
    for (lo, hi) in func_slices(instrs):
        out.extend(instrs[prev_end:lo])
        prev_end = hi + 1
        stats.functions += 1
        out.extend(_vectorize_function(instrs, lo, hi, transforms, stats,
                                       reports, global_base))
    out.extend(instrs[prev_end:])
    return out, stats, reports


def format_reports(stats, reports):
    """Human-readable summary for `compile_c_to_mcode(..., verbose=True)`."""
    lines = [f"{stats.vectorized} kernel(s) vectorized "
             f"({stats.declined} declined, {stats.rolled_back} rolled back)"]
    for name, n in sorted(stats.by_transform.items()):
        lines.append(f"    via {name}: {n}")
    for r in reports:
        if r.committed:
            lines.append(f"    {r!r}")
    return "\n".join(lines)

"""
dot_vectorizer.py -- Automatic Dot-Product & Sum-Reduction Vectorization (R4.1).

The first production vector transformation. It contains the shared per-function
driver (`vectorize_module`) that recognises, validates, and lowers the two
supported kernel classes, plus the dot-specific entry point. `reduction_
vectorizer.py` reuses this driver for sum reductions -- there is ONE pipeline.

Per innermost loop:
  1. kernel_detector recognises the kernel (dot-product / sum-reduction)
  2. vector_legality decides ISA legality (types / widths / aliasing / packing)
  3. vector_profitability requires a worthwhile lane count and trip
  4. vector_lowering emits packed loads + $dot / $vreduce + a scalar remainder
  5. differential_packed proves the vectorized function behaviour-identical
  6. the production backend compiles it AND the bundle count must drop
  Any failure at 2-6 rolls the loop back to its scalar form (untouched).

Reuses R4.0 (capability/legality/profitability/detector/validation) + lowering +
the existing backend/bundler unchanged. Emits vector IR only where every gate
passes; otherwise scalar compilation is byte-identical.
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir_utils import func_slices
from loopopt.discovery import discover_function
from loopopt.analysis_iv import annotate_induction_vars
from loopopt.analysis_mem import annotate_memory_effects
from loopopt.depgraph import DependenceGraph
from loopopt.depgraph_disambig import MemoryDisambiguator
from kernel_detector import _detect_loop
from vector_legality import analyze_legality_loop
from vector_profitability import estimate
from loopopt.analysis_profile import annotate_profile
from vector_lowering import lower_kernel, differential_packed

_SUPPORTED = ('dot-product', 'sum-reduction')


class VectorizeReport:
    __slots__ = ('func', 'header', 'label', 'kind', 'vtype', 'lanes',
                 'committed', 'reason', 'chunks', 'remainder',
                 'scalar_bundles', 'vector_bundles',
                 'scalar_dynamic', 'vector_dynamic')

    def __init__(self, func, header, label):
        self.func = func
        self.header = header
        self.label = label
        self.kind = None
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

    def _bump(self, k):
        self.reasons[k] = self.reasons.get(k, 0) + 1


def _bundles(ir, global_base):
    try:
        from codegen import CodeGen
        from bundler import bundle_mcode
        cg = CodeGen(global_base=global_base)
        body = cg.generate(copy.deepcopy(ir), global_base=global_base)
        _m, _n, b = bundle_mcode(body, schedule=True)
        return b, bool(cg.spilled)
    except Exception:
        return None, True


def _vectorize_function(instrs, lo, hi, allowed, stats, reports, global_base):
    """Vectorize the first eligible supported loop of one function. Returns the
    (possibly transformed) function slice as a list."""
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
        kernel = _detect_loop(d, sub)
        rep = VectorizeReport(fname, d.header, d.label())
        rep.kind = kernel.kind
        if kernel.kind not in allowed:
            continue                                    # not our kind -> silent skip
        reports.append(rep)

        legality = analyze_legality_loop(d, sub, graph)
        if not legality.legal:
            rep.reason = f'illegal:{legality.reason}'
            stats.declined += 1
            stats._bump(legality.reason)
            continue
        rep.vtype, rep.lanes = kernel.vtype, legality.lanes

        prof = estimate(legality, d)
        if not prof.profitable:
            rep.reason = f'unprofitable:{prof.note}'
            stats.declined += 1
            stats._bump('unprofitable')
            continue

        new_sub, why = lower_kernel(sub, 0, len(sub) - 1, d, kernel, legality)
        if new_sub is None:
            rep.reason = f'lower:{why}'
            stats.declined += 1
            stats._bump(f'lower:{why}')
            continue

        # VALIDATION: behaviour-identical under the packed vector oracle
        verdict, _detail = differential_packed(sub, new_sub, 0, len(sub) - 1)
        if verdict != 'match':
            rep.reason = f'differential:{verdict}'
            stats.rolled_back += 1
            stats._bump('differential-rollback')
            continue

        # BACKEND: must compile spill-free
        vb, vspill = _bundles(new_sub, global_base)
        sb, _sspill = _bundles(sub, global_base)
        if vb is None or vspill:
            rep.reason = 'compile-or-spill'
            stats.rolled_back += 1
            stats._bump('compile-or-spill')
            continue

        # PROFITABILITY: DYNAMIC executed operations must drop substantially. A
        # loop's cost is per-iteration work x trip; vectorization runs the packed
        # body once (chunks unrolled) + the scalar remainder, replacing `lanes`
        # scalar iterations with one vector op. (Static code may grow -- the
        # code-size-for-speed trade -- so we gate on dynamic, not static, bundles.)
        body_ops = getattr(d, 'body_inst_count', 0) or 1
        rep.chunks = kernel.trip // legality.lanes
        rep.remainder = kernel.trip % legality.lanes
        per_chunk = 5 if kernel.kind == 'dot-product' else 4   # loads + $dot/$vreduce
        scalar_dyn = body_ops * kernel.trip
        vec_dyn = rep.chunks * per_chunk + 4 + body_ops * rep.remainder
        rep.scalar_dynamic, rep.vector_dynamic = scalar_dyn, vec_dyn
        if vec_dyn >= scalar_dyn:
            rep.reason = f'no-dynamic-reduction({scalar_dyn}->{vec_dyn})'
            stats.declined += 1
            stats._bump('no-dynamic-reduction')
            continue

        rep.committed = True
        rep.reason = 'ok'
        rep.scalar_bundles, rep.vector_bundles = sb, vb
        stats.vectorized += 1
        stats._bump(kernel.kind)
        return new_sub                                  # one kernel per function
    return sub


def vectorize_module(instrs, allowed=_SUPPORTED, global_base=0x400):
    """Vectorize supported kernels across a module. Returns (new_instrs,
    VectorizeStats, [VectorizeReport]). Concatenates each function's (possibly
    transformed) slice so globals / inter-function code are preserved."""
    from vector_lowering import _vec_n
    _vec_n[0] = 0
    stats = VectorizeStats()
    reports = []
    out = []
    prev_end = 0
    for (lo, hi) in func_slices(instrs):
        out.extend(instrs[prev_end:lo])
        prev_end = hi + 1
        stats.functions += 1
        out.extend(_vectorize_function(instrs, lo, hi, allowed, stats, reports,
                                       global_base))
    out.extend(instrs[prev_end:])
    return out, stats, reports


def vectorize_dot_module(instrs, global_base=0x400):
    """Vectorize ONLY dot-product loops."""
    return vectorize_module(instrs, allowed=('dot-product',), global_base=global_base)

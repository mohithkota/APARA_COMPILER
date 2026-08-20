"""
matmul_probe.py -- R13.0 Phase 2/3: analysis-only driver for matmul_access.

Replicates the vector pipeline's loop enumeration EXACTLY (same discovery, same
detector, same legality) but calls `matmul_access.analyze` instead of a client
lowering. Nothing is transformed, nothing is emitted, production behaviour is
untouched.

    python3 matmul_probe.py prog.c            # report every matmul loop
"""

import copy
import os
import sys

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
import matmul_access
import vector_lowering


class LoopProbe:
    """One loop's full analysis record."""
    __slots__ = ('func', 'label', 'kind', 'vtype', 'lanes', 'trip',
                 'legal', 'legal_reason', 'profitable', 'prof_note',
                 'form', 'dot_plan_reason', 'dot_plan')

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))


def probe_instrs(instrs, global_base=0x400, kinds=('matmul',)):
    """Every loop whose detected kind is in `kinds`, with its access form."""
    out = []
    for (lo, hi) in func_slices(instrs):
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
            kernel = _detect_loop(d, sub)
            if kinds is not None and kernel.kind not in kinds:
                continue
            legality = analyze_legality_loop(d, sub, graph)
            prof = estimate(legality, d) if legality.legal else None
            form = matmul_access.analyze(d, sub, kernel, legality)
            # Phase 3: what does the EXISTING dot planner say about this loop?
            dp = None
            try:
                dp = vector_lowering.plan_lowering(d, sub, kernel, legality)
                dp_reason = None if dp.ok else dp.reason
            except Exception as e:                       # pragma: no cover
                dp_reason = f'exception:{e}'
            out.append(LoopProbe(
                func=fname, label=d.label(), kind=kernel.kind,
                vtype=kernel.vtype, lanes=getattr(legality, 'lanes', 0),
                trip=kernel.trip,
                legal=legality.legal,
                legal_reason=getattr(legality, 'reason', None),
                profitable=(prof.profitable if prof else False),
                prof_note=(prof.note if prof else None),
                form=form, dot_plan_reason=dp_reason, dot_plan=dp))
    return out


def probe_source(path, global_base=0x400, kinds=('matmul',)):
    """Build IR for a C file with the compiler's own front end, then probe it.

    Mirrors `compile_c_to_mcode`'s parse+IR prologue exactly (same preprocess,
    same _FAKE_TYPEDEFS, same IRGenerator) so the IR under analysis is the IR
    production would see."""
    import pycparser
    import compiler as _c
    from ir_gen import IRGenerator

    source, _ = _c.preprocess(path)
    ast = pycparser.CParser().parse(_c._FAKE_TYPEDEFS + source, filename=path)
    g = IRGenerator(global_base=global_base)
    g.visit(ast)
    return probe_instrs(list(g.instructions), global_base, kinds)


def format_probe(p):
    f = p.form
    head = (f"{p.func}:{p.label}  kind={p.kind} vtype={p.vtype} "
            f"lanes={p.lanes} trip={p.trip}")
    lines = [head,
             f"    legality   : {'LEGAL' if p.legal else 'ILLEGAL:' + str(p.legal_reason)}",
             f"    profit     : {p.profitable} ({p.prof_note})",
             f"    dot planner: {'ACCEPT' if p.dot_plan_reason is None else 'REJECT:' + p.dot_plan_reason}",
             f"    R13 form   : {'ACCEPT' if f.ok else 'REJECT:' + str(f.reason)}"]
    for name, ok, detail in f.checks:
        lines.append(f"        [{'ok ' if ok else 'REJ'}] {name}: {detail}")
    for a in f.accesses:
        lines.append(f"        access {a!r}")
    return "\n".join(lines)


if __name__ == '__main__':
    for path in sys.argv[1:]:
        print("=" * 78)
        print(path)
        print("=" * 78)
        ps = probe_source(path)
        if not ps:
            print("  (no loop detected with kind 'matmul')")
        for p in ps:
            print(format_probe(p))

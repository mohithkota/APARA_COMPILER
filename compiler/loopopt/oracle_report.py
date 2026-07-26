"""
oracle_report.py -- human-readable reporting for the R3.0 Oracle ILP Bound
Analyzer. Formats the per-loop LoopILP records from oracle_ilp into a per-loop
detail view and a per-module summary. ANALYSIS ONLY -- it never mutates IR.

As a CLI it analyses one C source file:

    python3 compiler/loopopt/oracle_report.py path/to/file.c
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .oracle_ilp import analyze_module                              # noqa: E402


_BUCKETS = list(range(0, 9))          # 0..7, 8 == "8+"


def _hist_line(hist):
    total = sum(hist.values()) or 1
    parts = []
    for b in _BUCKETS:
        c = hist.get(b, 0)
        if c:
            lbl = f"{b}+" if b == 8 else str(b)
            parts.append(f"{lbl}:{c}")
    return " ".join(parts) if parts else "-"


def format_loop(r):
    """A detailed multi-line view of one loop's ILP analysis."""
    L = []
    L.append(f"  {r.func}  loop '{r.label}'  (B{r.header}"
             f"{', trip=' + str(r.trip) if r.trip is not None else ''})")
    L.append(f"    instructions {r.n_instr}   edges {r.n_edges} "
             f"(recurrence {r.n_recur_edges})   reg-pressure {r.reg_pressure}")
    L.append(f"    critical path {r.crit_path} (true {r.crit_path_true})   "
             f"depth avg/max {r.avg_depth:.1f}/{r.max_depth}   "
             f"longest recurrence {r.longest_recurrence}")
    L.append(f"    MII {r.mii}  = max(RecMII {r.rec_mii}"
             f"{' [memory]' if r.mem_recurrence else ''}, ResMII {r.res_mii}"
             f" [mem {r.mem_term} / width {r.width_term} / div {r.div_term}])")
    L.append(f"    ready-set  avg {r.avg_ready:.1f}  max {r.max_ready}   "
             f"histogram  {_hist_line(r.ready_hist)}")
    L.append(f"    IPB   theoretical {r.theoretical_ipb:.2f}   "
             f"local-ideal {r.local_ideal_ipb:.2f}   achieved {r.achieved_ipb:.2f}"
             f"   utilization {r.utilization:.0%}")
    L.append(f"    gap   total {r.total_gap:.2f}  = pipelining {r.pipelining_gap:.2f}"
             f" + scheduler/renaming {r.scheduler_gap:.2f}")
    L.append(f"    LIMITER  {r.limiter}")
    L.append(f"    OPPORTUNITY  " + ", ".join(f"{n} (+{g:.2f})"
                                              for n, g in r.opportunities))
    return "\n".join(L)


def summarize(results):
    """Aggregate one module's loops into a compact summary dict."""
    n = len(results)
    if not n:
        return {}
    theo = sum(r.theoretical_ipb for r in results) / n
    ach = sum(r.achieved_ipb for r in results) / n
    util = sum(r.utilization for r in results) / n
    limiters = {}
    opps = {}
    for r in results:
        limiters[r.limiter] = limiters.get(r.limiter, 0) + 1
        opps[r.top_opportunity] = opps.get(r.top_opportunity, 0) + 1
    return {'n': n, 'avg_theoretical': theo, 'avg_achieved': ach,
            'avg_utilization': util, 'limiters': limiters, 'opportunities': opps}


def report_module(instrs, title="module"):
    """Analyse + print every innermost loop in a module. Returns [LoopILP]."""
    results = analyze_module(instrs)
    print("=" * 80)
    print(f"  ORACLE ILP BOUND ANALYSIS -- {title}")
    print("=" * 80)
    if not results:
        print("  (no innermost loops)")
        return results
    for r in results:
        print(format_loop(r))
        print()
    s = summarize(results)
    print("-" * 80)
    print(f"  loops {s['n']}   avg IPB  theoretical {s['avg_theoretical']:.2f}  "
          f"achieved {s['avg_achieved']:.2f}  utilization {s['avg_utilization']:.0%}")
    print(f"  dominant limiters   : {dict(sorted(s['limiters'].items(), key=lambda x: -x[1]))}")
    print(f"  top opportunities   : {dict(sorted(s['opportunities'].items(), key=lambda x: -x[1]))}")
    print("=" * 80)
    return results


def _main(argv):
    import pycparser
    from compiler import preprocess, _FAKE_TYPEDEFS
    from ir import Temp
    from ir_gen import IRGenerator
    if len(argv) < 2:
        print("usage: oracle_report.py file.c")
        return 2
    f = argv[1]
    src, _ = preprocess(f)
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
    Temp.reset()
    g = IRGenerator(global_base=0x400)
    g.visit(ast)
    report_module(g.instructions, os.path.basename(f))
    return 0


if __name__ == '__main__':
    raise SystemExit(_main(sys.argv))

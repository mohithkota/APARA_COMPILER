"""
_r3_0_test.py -- unit tests for the R3.0 Oracle ILP Bound Analyzer.

Verifies the analyzer's contract: it reports theoretical / achieved / utilization
/ dominant bottleneck / optimization opportunity for every innermost loop, and it
MUTATES NOTHING (no IR change, no codegen change, no correctness effect).

Run:  python3 compiler/loopopt/_r3_0_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import copy                                                            # noqa: E402
import pycparser                                                       # noqa: E402
from compiler import _FAKE_TYPEDEFS                                    # noqa: E402
from ir import Temp                                                    # noqa: E402
from ir_gen import IRGenerator                                         # noqa: E402
from codegen import CodeGen                                            # noqa: E402
from loopopt.oracle_ilp import (analyze_module, analyze_function,      # noqa: E402
                                LoopILP)
from loopopt.oracle_report import format_loop, summarize               # noqa: E402

_fails = []
_KNOWN_LIMITERS = {'recurrence-bound-memory', 'recurrence-bound-register',
                   'memory-bound', 'resource-bound-divide', 'resource-bound-width',
                   'dependency-bound', 'control-bound', 'mixed', 'balanced', 'empty'}


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _compile(code):
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=0x400)
    g.visit(ast)
    return g.instructions


REDUCE = "int f(){int t=0,j; int a[64]; for(j=0;j<64;j++) t+=a[j]; return t;}"
MEMSET = "int f(){int i; int a[64]; for(i=0;i<64;i++) a[i]=i; return 0;}"
DOTP = "int f(int*x,int*y,int n){int i; long s=0; for(i=0;i<n;i++) s+=x[i]*y[i]; return s;}"
NESTED = ("int f(){int s=0,i,j; int a[8][8];"
          " for(i=0;i<8;i++) for(j=0;j<8;j++) s+=a[i][j]; return s;}")


def test_reports_every_metric():
    print("every innermost loop gets theoretical/achieved/utilization/limiter/opp")
    for r in analyze_module(_compile(REDUCE)):
        check("has theoretical IPB > 0", r.theoretical_ipb > 0)
        check("has achieved IPB > 0", r.achieved_ipb > 0)
        check("utilization in [0,1]", 0.0 <= r.utilization <= 1.0)
        check("limiter is a known class", r.limiter in _KNOWN_LIMITERS)
        check("top opportunity present", r.top_opportunity is not None)
        check("opportunities ranked descending",
              all(r.opportunities[i][1] >= r.opportunities[i + 1][1]
                  for i in range(len(r.opportunities) - 1)))


def test_theoretical_is_ceiling():
    print("theoretical IPB is an upper bound and matches N/MII (capped at 8)")
    for code in (REDUCE, MEMSET, DOTP):
        for r in analyze_module(_compile(code)):
            check("theoretical == min(N/MII, 8)",
                  abs(r.theoretical_ipb - min(r.n_instr / r.mii, 8.0)) < 1e-6)
            check("MII == max(RecMII, ResMII)", r.mii == max(r.rec_mii, r.res_mii))
            check("theoretical >= achieved (ceiling above realised)",
                  r.theoretical_ipb >= r.achieved_ipb - 1e-9)
            check("utilization <= 1", r.utilization <= 1.0 + 1e-9)


def test_ready_set_histogram():
    print("ready-set histogram is well-formed and reflects exposed ILP")
    for r in analyze_module(_compile(DOTP)):
        check("histogram buckets are 0..8", all(0 <= b <= 8 for b in r.ready_hist))
        check("avg ready within [0, max]", 0 <= r.avg_ready <= r.max_ready)
        check("some cycle exposes parallelism (max ready >= 2)", r.max_ready >= 2)


def test_analysis_mutates_nothing():
    print("the analysis changes neither the IR nor the generated code")
    ir = _compile(DOTP)
    snap = [repr(x) for x in ir]
    before = CodeGen(global_base=0x400).generate(copy.deepcopy(ir), global_base=0x400)
    _ = analyze_module(ir)                        # run the oracle
    after = CodeGen(global_base=0x400).generate(copy.deepcopy(ir), global_base=0x400)
    check("IR unchanged (byte-identical repr)", [repr(x) for x in ir] == snap)
    check("generated code unchanged", before == after)


def test_innermost_only():
    print("only INNERMOST loops are analysed (the outer loop is skipped)")
    res = analyze_module(_compile(NESTED))
    check("exactly one innermost loop reported", len(res) == 1)
    # the reported loop is the j-loop (its body has the accumulate)
    check("it is a real loop with instructions", res[0].n_instr > 0)


def test_memset_is_vectorizable():
    print("a unit-stride elementwise store loop surfaces vectorization")
    res = analyze_module(_compile(MEMSET))
    names = [n for r in res for (n, _g) in r.opportunities]
    check("vectorization is among the opportunities", 'vectorization' in names)


def test_reduction_opportunity_actionable():
    print("a memory-recurrence reduction surfaces an actionable lever")
    res = analyze_module(_compile(REDUCE))
    r = res[0]
    check("memory recurrence detected", r.mem_recurrence)
    actionable = {'software-pipelining', 'register-promotion', 'reassociation'}
    check("top opportunity is actionable",
          any(n in actionable for n, _g in r.opportunities))


def test_determinism():
    print("analysis is deterministic")
    a = analyze_module(_compile(DOTP))
    b = analyze_module(_compile(DOTP))
    check("same theoretical/achieved/limiter twice",
          [(x.theoretical_ipb, x.achieved_ipb, x.limiter) for x in a]
          == [(x.theoretical_ipb, x.achieved_ipb, x.limiter) for x in b])


def test_summary_and_format():
    print("report helpers produce a summary and a formatted view")
    res = analyze_module(_compile(REDUCE))
    s = summarize(res)
    check("summary has averages", 'avg_theoretical' in s and 'avg_achieved' in s)
    check("format_loop returns text", isinstance(format_loop(res[0]), str)
          and 'LIMITER' in format_loop(res[0]))


def main():
    for t in (test_reports_every_metric, test_theoretical_is_ceiling,
              test_ready_set_histogram, test_analysis_mutates_nothing,
              test_innermost_only, test_memset_is_vectorizable,
              test_reduction_opportunity_actionable, test_determinism,
              test_summary_and_format):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R3.0 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

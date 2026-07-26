"""
_r3_1_test.py -- unit tests for R3.1 Production Software Pipelining Integration.

Verifies the integration contract:
  * correctness unchanged -- every pipelined function is behaviour-identical to
    the original (IR differential), and non-pipelined output is untouched;
  * production automatically pipelines profitable loops;
  * rollback is reliable -- unsupported / spilling / unprofitable loops are left
    in the proven production form;
  * spill safety -- an accepted pipeline never introduces register spills;
  * the APARA_NO_SWP kill-switch reverts to byte-identical output;
  * determinism and the profitability report.

Run:  python3 compiler/_r3_1_test.py
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pycparser                                                       # noqa: E402
from compiler import _FAKE_TYPEDEFS, compile_c_to_mcode               # noqa: E402
from ir import Temp                                                    # noqa: E402
from ir_gen import IRGenerator                                         # noqa: E402
from ir_utils import func_slices                                       # noqa: E402
from codegen import CodeGen                                            # noqa: E402
from production_swp import (apply_production_swp, profitable_functions,  # noqa: E402
                            format_profitability)
from loopopt import ir_interp                                          # noqa: E402

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _ir(code):
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=0x400)
    g.visit(ast)
    return list(g.instructions)


def _spills(ir):
    cg = CodeGen(global_base=0x400)
    cg.generate(copy.deepcopy(ir), global_base=0x400)
    return cg.spilled


def _no_mismatch(ir0, final):
    for lo, hi in func_slices(ir0):
        v, _d = ir_interp.differential(ir0, final, lo, hi)
        if v == 'mismatch':
            return False
    return True


# programs that the corpus shows are pipeline-eligible
REDUCE = "int f(){int t=0,j; int a[32]; for(j=0;j<32;j++) a[j]=j; for(j=0;j<32;j++) t+=a[j]; return t;}"
DOT = "int f(){int i; int x[40],y[40]; long s=0; for(i=0;i<40;i++){x[i]=i;y[i]=i;} for(i=0;i<40;i++) s+=x[i]*y[i]; return s;}"
NOLOOP = "int f(int a,int b){return a*b+7;}"
CALLING = "int ext(int); int f(){int s=0,i; for(i=0;i<20;i++) s+=ext(i); return s;}"
CANDIDATES = [REDUCE, DOT]


def test_pipelines_profitable_loops():
    print("production automatically pipelines profitable loops")
    total = 0
    for code in CANDIDATES:
        ir0 = _ir(code)
        final, recs, summ = apply_production_swp(ir0, ir0, global_base=0x400)
        total += summ.pipelined
    check("at least one candidate pipelined", total >= 1)


def test_correctness_unchanged():
    print("every pipelined program is behaviour-identical to the original")
    for code in CANDIDATES:
        ir0 = _ir(code)
        final, recs, summ = apply_production_swp(ir0, ir0, global_base=0x400)
        check(f"{code[:14]}..: no differential mismatch", _no_mismatch(ir0, final))
        if final is not ir0:
            check("pipelined form is spill-free", not _spills(final))


def test_spill_safety():
    print("an accepted pipeline never introduces register spills")
    for code in CANDIDATES:
        ir0 = _ir(code)
        final, recs, summ = apply_production_swp(ir0, ir0, global_base=0x400)
        if summ.pipelined:
            check("accepted -> program compiles spill-free", not _spills(final))
        for r in recs:
            if not r.accepted:
                check("rollback reason recorded", r.rollback_reason is not None)


def test_rollback_unsupported_call():
    print("a loop with a call is not pipelined (rollback), output untouched")
    ir0 = _ir(CALLING)
    final, recs, summ = apply_production_swp(ir0, ir0, global_base=0x400)
    check("nothing pipelined", summ.pipelined == 0)
    check("output identical to baseline", final is ir0
          or [repr(x) for x in final] == [repr(x) for x in ir0])


def test_noloop_untouched():
    print("a loop-free program is returned unchanged (no oracle recommendation)")
    ir0 = _ir(NOLOOP)
    prof = profitable_functions(ir0, 0.5)
    check("no profitable functions", len(prof) == 0)
    final, recs, summ = apply_production_swp(ir0, ir0, global_base=0x400)
    check("final IS the input (no-op)", final is ir0)
    check("no records", len(recs) == 0)


def test_kill_switch_identity():
    print("APARA_NO_SWP produces byte-identical output (deterministic baseline)")
    import tempfile
    d = tempfile.mkdtemp()
    src = os.path.join(d, "k.c")
    with open(src, "w") as fh:
        fh.write(REDUCE.replace("int f()", "int main()"))
    os.environ['APARA_NO_SWP'] = '1'
    a = os.path.join(d, "a.mcode"); compile_c_to_mcode(src, output_file=a)
    b = os.path.join(d, "b.mcode"); compile_c_to_mcode(src, output_file=b)
    os.environ.pop('APARA_NO_SWP')
    with open(a) as fa, open(b) as fb:
        check("NO_SWP builds byte-identical", fa.read() == fb.read())


def test_determinism():
    print("the SWP integration is deterministic")
    ir0 = _ir(REDUCE)
    f1, _r1, _s1 = apply_production_swp(_ir(REDUCE), _ir(REDUCE), global_base=0x400)
    f2, _r2, _s2 = apply_production_swp(_ir(REDUCE), _ir(REDUCE), global_base=0x400)
    check("identical output twice", [repr(x) for x in f1] == [repr(x) for x in f2])


def test_profitability_report():
    print("profitability report records the required fields")
    ir0 = _ir(REDUCE)
    final, recs, summ = apply_production_swp(ir0, ir0, global_base=0x400)
    txt = format_profitability(recs, summ)
    check("report is a string with IPB/oracle fields",
          isinstance(txt, str) and 'oracle' in txt)
    for r in recs:
        check("record has original & oracle IPB + expected gain",
              hasattr(r, 'original_ipb') and hasattr(r, 'oracle_ipb')
              and hasattr(r, 'expected_gain'))


def main():
    for t in (test_pipelines_profitable_loops, test_correctness_unchanged,
              test_spill_safety, test_rollback_unsupported_call,
              test_noloop_untouched, test_kill_switch_identity,
              test_determinism, test_profitability_report):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R3.1 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

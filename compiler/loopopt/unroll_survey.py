"""
unroll_survey.py -- R1.1 corpus validation for the LoopUnroll infrastructure.

For every corpus program: analyze every loop (eligibility / legality /
profitability, NO mutation) and, separately, drive the LoopUnroll transform
through the M5 framework to prove it is a clean no-op (0 IR changes, 0 verifier
failures, 0 rollbacks). Prints the required corpus statistics.

Run:  python3 compiler/loopopt/unroll_survey.py
"""

import os
import sys
import glob

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_COMPILER)
sys.path.insert(0, _COMPILER)

import pycparser                                                    # noqa: E402
from compiler import preprocess, _FAKE_TYPEDEFS                     # noqa: E402
from ir import Temp                                                 # noqa: E402
from ir_gen import IRGenerator                                      # noqa: E402
from loopopt.loop_unroll import (analyze_module, drive_noop,       # noqa: E402
                                 UnrollSurveyReport)


def _gen(f):
    try:
        src, _ = preprocess(f)
        ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
        Temp.reset()
        g = IRGenerator(global_base=0x400)
        g.visit(ast)
        return g.instructions
    except Exception:
        return None


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    survey = UnrollSurveyReport()
    for f in files:
        instrs = _gen(f)
        if instrs is None:
            continue

        # 1. analysis (no mutation)
        reports = analyze_module(instrs)
        survey.add_program(reports)

        # 2. no-op framework drive -- must not change the IR and must not fail
        before = [repr(x) for x in instrs]
        stats = drive_noop(instrs)
        after = [repr(x) for x in instrs]
        survey.verifier_failures += stats.verifier_failures
        survey.rollbacks += stats.rollbacks
        if before != after:
            survey.ir_changes += 1

    print("=" * 72)
    print("  R1.1 LoopUnroll INFRASTRUCTURE -- CORPUS VALIDATION")
    print("=" * 72)
    print(survey.report())
    ok = (survey.ir_changes == 0 and survey.verifier_failures == 0
          and survey.rollbacks == 0)
    print("=" * 72)
    print("  RESULT:", "PASS (0 IR changes / 0 verifier failures / 0 rollbacks)"
          if ok else "FAIL")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())

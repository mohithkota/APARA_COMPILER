"""
ivsr_crosscheck.py -- prove the M9 LoopIVSR is behaviourally equal to ivsr.py.

ivsr.py is the SPECIFICATION. For every corpus program we generate one IR, make
two independent deep copies, and run:

    A = ivsr.induction_strength_reduce(copyA)   (the spec)
    B = ivsr_module(copyB)                        (the framework migration)

then compare the two resulting instruction sequences by their textual form
(repr per instruction). Both pipelines share ivsr's module-global fresh-temp
counter `ivsr._iv_n`; it is reset to 0 immediately before EACH run so the two see
identical temp numbering (the spec never resets it within a single compilation,
so both starting from the same value is faithful). Identical repr sequences prove
the two produce instruction-for-instruction identical IR.

Outcomes per program:
  MATCH     : the two IRs are identical (required for every file).
  MISMATCH  : any divergence -- printed with the first differing position and a
              small context window, and must be zero.

We also report, across the corpus, the total loops strength-reduced by the
framework pass, and the framework's verifier failures / rollbacks -- both of which
must be zero (a transform the spec keeps must never be rejected by the framework).

Usage: python3 compiler/loopopt/ivsr_crosscheck.py
"""

import os
import sys
import glob

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_COMPILER)
sys.path.insert(0, _COMPILER)

import copy                                                          # noqa: E402
import pycparser                                                    # noqa: E402
from compiler import preprocess, _FAKE_TYPEDEFS                     # noqa: E402
from ir import Temp                                                 # noqa: E402
from ir_gen import IRGenerator                                      # noqa: E402
import ivsr                                                         # noqa: E402
from loopopt import ivsr_module                                     # noqa: E402


def _gen(f):
    """Compile one C file to IR (or None if it does not build)."""
    try:
        src, _ = preprocess(f)
        ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
        Temp.reset()
        g = IRGenerator(global_base=0x400)
        g.visit(ast)
        return g.instructions
    except Exception:
        return None


def _first_diff(a, b):
    for k in range(min(len(a), len(b))):
        if a[k] != b[k]:
            return k
    if len(a) != len(b):
        return min(len(a), len(b))
    return None


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    n_files = n_match = 0
    mismatches = []
    tot_reduced = 0
    tot_verifier_failures = tot_rollbacks = 0

    for f in files:
        instrs = _gen(f)
        if instrs is None:
            continue
        n_files += 1

        # spec run -- reset the shared fresh counter so both runs number alike.
        ivsr._iv_n[0] = 0
        a = ivsr.induction_strength_reduce(copy.deepcopy(instrs))

        # framework run -- same starting counter value.
        ivsr._iv_n[0] = 0
        b, stats, _rep = ivsr_module(copy.deepcopy(instrs))

        tot_reduced += stats.commits
        tot_verifier_failures += stats.verifier_failures
        tot_rollbacks += stats.rollbacks

        ra = [repr(x) for x in a]
        rb = [repr(x) for x in b]
        d = _first_diff(ra, rb)
        if d is None:
            n_match += 1
        else:
            mismatches.append((os.path.relpath(f, _ROOT), d, ra, rb))

    print("=" * 72)
    print("  M9 LoopIVSR  vs  ivsr.py  -- BEHAVIOURAL CROSS-CHECK")
    print("=" * 72)
    print(f"  programs compared          : {n_files}")
    print(f"  MATCH (identical IR)       : {n_match}")
    print(f"  MISMATCH                   : {len(mismatches)}")
    print(f"  loops strength-reduced (M9): {tot_reduced}")
    print(f"  framework verifier failures: {tot_verifier_failures}")
    print(f"  framework rollbacks        : {tot_rollbacks}")
    if mismatches:
        print("\n  MISMATCHES (must be zero):")
        for fn, d, ra, rb in mismatches[:20]:
            print(f"    {fn}  first diff @ {d}  (len spec={len(ra)} m9={len(rb)})")
            lo = max(0, d - 2)
            for k in range(lo, min(max(len(ra), len(rb)), d + 3)):
                la = ra[k] if k < len(ra) else '<none>'
                lb = rb[k] if k < len(rb) else '<none>'
                mark = '  <<<' if k == d else ''
                print(f"        [{k}] spec: {la}")
                print(f"        [{k}]  m9 : {lb}{mark}")
    print("=" * 72)
    ok = (not mismatches and tot_verifier_failures == 0 and tot_rollbacks == 0)
    print("  RESULT:", "PASS" if ok else "FAIL")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())

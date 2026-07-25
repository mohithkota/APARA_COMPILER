"""
licm_crosscheck.py -- prove the M8 LoopLICM is behaviourally equal to licm2.py.

licm2.py is the SPECIFICATION. For every corpus program we generate one IR, make
two independent deep copies, and run:

    A = licm2.loop_invariant_code_motion(copyA)   (APARA_LICM forced on)
    B = licm_module(copyB)                         (the framework migration)

then compare the two resulting instruction sequences by their textual form
(repr per instruction). Because BOTH passes only MOVE existing instructions
(never create/delete), identical repr sequences prove the two pick the same
hoists and the same destinations -- i.e. produce instruction-for-instruction
identical IR.

Outcomes per program:
  MATCH     : the two IRs are identical (the required result for every file).
  MISMATCH  : any divergence -- printed with the first differing position and a
              small context window, and must be zero.

We also report, across the corpus, the total instructions hoisted by each pass
(these must be equal) and any verifier failures / rollbacks inside the framework
run (these must be zero -- a legal hoist must never be rejected by the verifier).

Usage: python3 compiler/loopopt/licm_crosscheck.py
"""

import os
import sys
import glob

# licm2 is gated by APARA_LICM (default OFF); force it ON for the comparison, and
# make sure the kill-switch is clear. Set before importing anything that reads it.
os.environ['APARA_LICM'] = '1'
os.environ.pop('APARA_NO_LICM', None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_COMPILER)
sys.path.insert(0, _COMPILER)

import copy                                                          # noqa: E402
import pycparser                                                    # noqa: E402
from compiler import preprocess, _FAKE_TYPEDEFS                     # noqa: E402
from ir import Temp                                                 # noqa: E402
from ir_gen import IRGenerator                                      # noqa: E402
import licm2                                                        # noqa: E402
from loopopt import licm_module                                     # noqa: E402


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
    """Index of the first differing element of two repr lists, or None if equal
    up to the shorter length AND same length."""
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
    tot_licm2 = tot_m8 = 0
    tot_verifier_failures = tot_rollbacks = 0

    for f in files:
        instrs = _gen(f)
        if instrs is None:
            continue
        n_files += 1

        a = licm2.loop_invariant_code_motion(copy.deepcopy(instrs))
        b = copy.deepcopy(instrs)
        stats, _rep = licm_module(b)

        tot_m8 += stats.commits
        tot_verifier_failures += stats.verifier_failures
        tot_rollbacks += stats.rollbacks
        # equivalence is decided by the repr-sequence comparison below; licm2's
        # own hoist count is gathered separately (via APARA_LICM_DEBUG) as an
        # independent check that both passes hoist the same number of instrs.
        ra = [repr(x) for x in a]
        rb = [repr(x) for x in b]

        d = _first_diff(ra, rb)
        if d is None:
            n_match += 1
        else:
            mismatches.append((os.path.relpath(f, _ROOT), d, ra, rb))

    # licm2 hoist total via APARA_LICM_DEBUG on a clean re-run of all files.
    tot_licm2 = _count_licm2_hoists(files)

    print("=" * 72)
    print("  M8 LoopLICM  vs  licm2.py  -- BEHAVIOURAL CROSS-CHECK")
    print("=" * 72)
    print(f"  programs compared          : {n_files}")
    print(f"  MATCH (identical IR)       : {n_match}")
    print(f"  MISMATCH                   : {len(mismatches)}")
    print(f"  instructions hoisted licm2 : {tot_licm2}")
    print(f"  instructions hoisted M8    : {tot_m8}")
    print(f"  framework verifier failures: {tot_verifier_failures}")
    print(f"  framework rollbacks        : {tot_rollbacks}")
    if mismatches:
        print("\n  MISMATCHES (must be zero):")
        for fn, d, ra, rb in mismatches[:20]:
            print(f"    {fn}  first diff @ {d}")
            lo = max(0, d - 2)
            for k in range(lo, min(max(len(ra), len(rb)), d + 3)):
                la = ra[k] if k < len(ra) else '<none>'
                lb = rb[k] if k < len(rb) else '<none>'
                mark = '  <<<' if k == d else ''
                print(f"        [{k}] licm2: {la}")
                print(f"        [{k}]  m8  : {lb}{mark}")
    print("=" * 72)
    ok = (not mismatches and tot_licm2 == tot_m8
          and tot_verifier_failures == 0 and tot_rollbacks == 0)
    print("  RESULT:", "PASS" if ok else "FAIL")
    print("=" * 72)
    return 0 if ok else 1


def _count_licm2_hoists(files):
    """Total hoists licm2 reports (APARA_LICM_DEBUG) over the corpus, for an
    independent hoist-count equality check against the framework's commit count."""
    import io
    import re
    import contextlib
    os.environ['APARA_LICM_DEBUG'] = '1'
    total = 0
    pat = re.compile(r'\[licm\] hoisted=(\d+)')
    for f in files:
        instrs = _gen(f)
        if instrs is None:
            continue
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            licm2.loop_invariant_code_motion(instrs)
        m = pat.search(buf.getvalue())
        if m:
            total += int(m.group(1))
    os.environ.pop('APARA_LICM_DEBUG', None)
    return total


if __name__ == '__main__':
    raise SystemExit(main())

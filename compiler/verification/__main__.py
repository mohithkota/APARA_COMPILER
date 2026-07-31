"""
__main__.py -- run the R6.2A verified regression suite on the real simulator.

    python3 -m verification                 # whole suite + negative controls
    python3 -m verification --quick         # one marker per family
    python3 -m verification --no-vectorize  # scalar baseline for comparison
    python3 -m verification --csv out.csv   # record the dynamic metrics

Every program is compiled, assembled, simulated and CHECKED against an
independent gcc reference. A program that cannot produce a reference is a
FAILURE, not a skip -- that is the whole point of the milestone.
"""

import argparse
import csv
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import harness
import suite as suite_mod


def negative_controls(workdir):
    """Prove the harness CANNOT report success when it has not really checked.

    Three ways a run used to look green while proving nothing, each reproduced
    deliberately. Every one of them must now come back FAIL."""
    out = []

    # (a) a wrong answer must be caught -- exit status is 0 either way
    src = suite_mod.axpy('vi8_t')
    v = harness.verify_program('neg_corrupt', src, 4,
                               os.path.join(workdir, 'neg_corrupt'), corrupt=0)
    out.append(('corrupted expected value', v,
                (not v.ok) and v.stage == 'clean' and len(v.errors) >= 1))

    # (b) a test with no results[] cannot produce a reference: the old flow
    #     wrote a placeholder and carried on, which is exactly the hole.
    src_nogold = ("int main(void){ int a[8]; int i; long long s=0;"
                  "for(i=0;i<8;i++) a[i]=i; for(i=0;i<8;i++) s+=a[i];"
                  "return (int)s; }\n")
    v = harness.verify_program('neg_nogolden', src_nogold, 4,
                               os.path.join(workdir, 'neg_nogolden'))
    out.append(('no golden reference available', v,
                (not v.ok) and v.stage == 'native'))

    # (c) declaring more comparisons than the reference contains must fail at
    #     the golden check, not pass by accident
    v = harness.verify_program('neg_count', suite_mod.dot('vi8_t'), 99,
                               os.path.join(workdir, 'neg_count'))
    out.append(('declared count exceeds reference', v,
                (not v.ok) and v.stage == 'golden'))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog='verification')
    ap.add_argument('--quick', action='store_true',
                    help='one marker (vi8_t) instead of all six')
    ap.add_argument('--no-vectorize', action='store_true',
                    help='compile with APARA_NO_VECTORIZE for a scalar baseline')
    ap.add_argument('--csv', help='write the dynamic simulator metrics here')
    ap.add_argument('--keep', action='store_true',
                    help='keep the build directory')
    ap.add_argument('--skip-negative', action='store_true')
    a = ap.parse_args(argv)

    if not harness.toolchain_available():
        print(f"ERROR: APARA toolchain not found in {harness.tools_dir()}")
        print("       export APARA_TOOLS=/path/to/engine_isp/assembler/bin")
        return 2

    markers = ['vi8_t'] if a.quick else None
    programs = suite_mod.build_suite(markers=markers)
    workdir = (os.path.join(_HERE, '_run') if a.keep
               else tempfile.mkdtemp(prefix='r62a_'))
    os.makedirs(workdir, exist_ok=True)
    print(f"toolchain : {harness.tools_dir()}")
    print(f"workdir   : {workdir}")
    print(f"programs  : {len(programs)}"
          f"{'  (vectorization DISABLED)' if a.no_vectorize else ''}\n")

    t0 = time.time()
    results, failures = [], []
    for (name, marker, fam, n, src) in programs:
        v = harness.verify_program(name, src, n, os.path.join(workdir, name.replace(' ', '_')),
                                   vectorize=not a.no_vectorize)
        v.vectorized = harness.is_vectorized(src) if not a.no_vectorize else False
        results.append((marker, fam, v))
        if not v.ok:
            failures.append(v)
        tag = 'vec' if v.vectorized else 'sca'
        if v.ok:
            print(f"  PASS  {name:22s} {tag}  {v.postconditions} checks  "
                  f"{v.ticks:6d} ticks  {v.non_null:6d} instr  "
                  f"ipb={v.dynamic_ipb:5.3f}  occ={v.dynamic_occupancy:5.1%}")
        else:
            print(f"  FAIL  {name:22s} {tag}  [{v.stage}] {v.reason}")

    print(f"\n{len(results) - len(failures)}/{len(results)} programs verified "
          f"in {time.time() - t0:.1f}s")

    neg_ok = True
    if not a.skip_negative:
        print("\nnegative controls (each MUST fail):")
        for (label, v, expected) in negative_controls(os.path.join(workdir, 'neg')):
            print(f"  {'ok  ' if expected else 'BAD '} {label:34s} -> "
                  f"{'FAIL[' + str(v.stage) + ']' if not v.ok else 'PASSED (!!)'}")
            neg_ok = neg_ok and expected

    if a.csv:
        with open(a.csv, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['program', 'marker', 'family', 'vectorized', 'ok',
                        'postconditions', 'ticks', 'non_null_instructions',
                        'null_instructions', 'issue_slots', 'dynamic_ipb',
                        'dynamic_occupancy', 'static_bundles',
                        'static_instructions'])
            for (marker, fam, v) in results:
                w.writerow([v.name, marker, fam, int(bool(v.vectorized)),
                            int(v.ok), v.postconditions, v.ticks, v.non_null,
                            v.null, v.issue_slots,
                            round(v.dynamic_ipb, 5) if v.dynamic_ipb else '',
                            round(v.dynamic_occupancy, 5) if v.dynamic_occupancy else '',
                            v.static_bundles, v.static_instructions])
        print(f"\nmetrics -> {a.csv}")

    if failures or not neg_ok:
        print(f"\nRESULT: FAIL  ({len(failures)} program failures"
              f"{', negative control did not fail' if not neg_ok else ''})")
        return 1
    print("\nRESULT: PASS  (every program verified against a real golden "
          "reference; every negative control rejected)")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

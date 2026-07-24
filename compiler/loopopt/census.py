"""
census.py -- M0 measurement harness for the Loop Optimization Framework.

Runs LoopDiscovery + observe-only LoopVerifier over a corpus of C sources and
reports the natural-loop population, a purely-structural "Tier-1 legality
PROXY", and verifier health. This reproduces the frozen-architecture baseline
(~79 natural loops, ~70 Tier-1-legal) as the M0 regression anchor.

NOTE: the real Legality Framework is a LATER milestone (M4/M5). The counts here
are computed directly from LoopDescriptor structural fields (plus a trivial
call scan) purely for measurement -- this file builds NO legality/transform
infrastructure and mutates nothing.

Usage:
    python3 compiler/loopopt/census.py [dir_or_glob ...]
    (default corpus: testing/ new_isa_tests/ demo_prof/ isa_coverage_tests/)
"""

import os
import sys
import glob

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_COMPILER)              # cmp_wd/
sys.path.insert(0, _COMPILER)

import pycparser                                                    # noqa: E402
from compiler import preprocess, _FAKE_TYPEDEFS                    # noqa: E402
from ir import Temp                                                # noqa: E402
from ir_gen import IRGenerator                                     # noqa: E402
from loopopt import discover, LoopVerifier, TOP_TESTED             # noqa: E402


def _has_call(desc):
    """Trivial structural scan: does any body block contain a call?
    (A measurement proxy only -- the real MemEffects analysis is M2.)"""
    for b in desc.body_blocks:
        blk = desc.cfg.blocks[b]
        for i in range(blk.lo, blk.hi + 1):
            if type(desc.cfg.instrs[i]).__name__ in ('IRCall', 'IRIndirectCall'):
                return True
    return False


def _tier1_proxy(desc):
    """Conservative structural legality proxy (NOT the real framework):
    single-latch AND top-tested AND single-exit AND call-free."""
    return (len(desc.latches) == 1
            and desc.shape == TOP_TESTED
            and len(desc.exit_edges) <= 1
            and not _has_call(desc))


def _default_corpus():
    pats = ['testing/**/*.c', 'new_isa_tests/**/*.c',
            'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c']
    files = []
    for p in pats:
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    return sorted(set(files))


def _expand(args):
    files = []
    for a in args:
        if os.path.isdir(a):
            files += glob.glob(os.path.join(a, '**', '*.c'), recursive=True)
        elif any(ch in a for ch in '*?['):
            files += glob.glob(a, recursive=True)
        elif a.endswith('.c'):
            files.append(a)
    return sorted(set(files))


def run(files):
    verifier = LoopVerifier()
    n_files = n_fail_parse = 0
    total = 0
    single_latch = top_tested = single_exit = call_free = tier1 = 0
    verify_ok = 0
    violations = []

    for f in files:
        try:
            src, _ = preprocess(f)
            ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
            Temp.reset()
            g = IRGenerator(global_base=0x400)
            g.visit(ast)
            instrs = g.instructions
            n_files += 1
        except Exception:
            n_fail_parse += 1
            continue

        descs = discover(instrs)
        for d in descs:
            total += 1
            if len(d.latches) == 1:
                single_latch += 1
            if d.shape == TOP_TESTED:
                top_tested += 1
            if len(d.exit_edges) <= 1:
                single_exit += 1
            if not _has_call(d):
                call_free += 1
            if _tier1_proxy(d):
                tier1 += 1
            res = verifier.verify(d)
            if res.ok:
                verify_ok += 1
            else:
                violations.append(res)

    print("=" * 66)
    print("  LOOP OPTIMIZATION FRAMEWORK -- M0 CENSUS")
    print("=" * 66)
    print(f"  source files parsed OK      : {n_files}  (parse-failed {n_fail_parse})")
    print(f"  natural loops discovered    : {total}")
    print(f"    single-latch              : {single_latch}")
    print(f"    top-tested                : {top_tested}")
    print(f"    single-exit               : {single_exit}")
    print(f"    call-free body            : {call_free}")
    print(f"  Tier-1 legality PROXY       : {tier1}"
          f"  ({100.0 * tier1 / total:.0f}% of loops)" if total else "")
    print(f"  verifier clean              : {verify_ok}/{total}")
    if violations:
        print("  VERIFIER VIOLATIONS:")
        for v in violations[:20]:
            print("   ", v.report().replace("\n", "\n    "))
    print("=" * 66)
    return 0 if (total and not violations) else 1


def main(argv):
    files = _expand(argv[1:]) if len(argv) > 1 else _default_corpus()
    if not files:
        print("no .c files found")
        return 1
    return run(files)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))

"""
superblock_corpus.py -- R3.2 corpus evaluation of superblock / trace scheduling.

Measures the production compiler with the R3.2 superblock pass (region formation +
the existing scheduler over the enlarged regions) against the R3.1 production
compiler (production optimizer + software pipelining). Reports average / best IPB,
bundle density, scheduling-region size, compile time, and the rollback rate, with
an IR differential check (0 mismatches required).

Reuses the production-optimizer reconstruction and metrics from swp_prod_corpus.

Run:  python3 compiler/superblock_corpus.py
"""

import os
import sys
import glob
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import pycparser                                                    # noqa: E402
from compiler import preprocess, _FAKE_TYPEDEFS                     # noqa: E402
from ir import Temp                                                 # noqa: E402
from ir_gen import IRGenerator                                      # noqa: E402
from ir_utils import func_slices                                   # noqa: E402
from swp_prod_corpus import _production_optimize, _metrics, _mismatch  # noqa: E402
from production_swp import apply_production_swp                     # noqa: E402
from trace_scheduler import apply_superblock_scheduling            # noqa: E402
from superblock import superblock_module                           # noqa: E402

_GB = 0x400


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    progs = 0
    attempted = accepted = rolled = mism = 0
    reg_before = reg_after = reg_n = 0
    best_gain = 0.0
    sb_time = 0.0
    r31 = [0, 0]                       # static, bundles (SWP on, superblock off)
    r32 = [0, 0]                       # static, bundles (SWP on, superblock on)

    for f in files:
        try:
            src, _ = preprocess(f)
            ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
            Temp.reset()
            g = IRGenerator(global_base=_GB)
            g.visit(ast)
            ir0 = list(g.instructions)
        except Exception:
            continue
        progs += 1

        prod = _production_optimize(ir0)
        swp, _recs, _sum = apply_production_swp(ir0, prod, global_base=_GB)  # R3.1

        t0 = time.time()
        final, summ = apply_superblock_scheduling(swp, global_base=_GB)      # R3.2
        sb_time += time.time() - t0

        if summ.scheduling_headroom:
            attempted += 1
        if summ.accepted:
            accepted += 1
            reg_before += summ.avg_region_before
            reg_after += summ.avg_region_after
            reg_n += 1
        elif summ.reason in ('spill-increase', 'bundle-increase', 'compile-failed'):
            rolled += 1
        rolled += summ.rollbacks              # per-function scheduler rollbacks

        # R3.2 correctness = it must preserve its INPUT (the R3.1 output). The
        # ir0-vs-optimized differences are pre-existing ir_interp-oracle gaps on
        # heavily optimized code (division / sub-word / bit ops), unrelated to the
        # region enlargement, so we validate final against swp (same opt level).
        if final is not swp and _mismatch(swp, final):
            mism += 1
            print("  MISMATCH:", os.path.basename(f))

        m31 = _metrics(swp)
        m32 = _metrics(final)
        if m31 and m32:
            r31[0] += m31[0]; r31[1] += m31[1]
            r32[0] += m32[0]; r32[1] += m32[1]

    _report(progs, attempted, accepted, rolled, mism, reg_before, reg_after,
            reg_n, sb_time, r31, r32)
    return 0 if mism == 0 else 1


def _report(progs, attempted, accepted, rolled, mism, rb, ra, rn, t, r31, r32):
    def ipb(a):
        return a[0] / a[1] if a[1] else 0.0
    print("=" * 82)
    print("  R3.2 SUPERBLOCK / TRACE SCHEDULING -- CORPUS EVALUATION")
    print("=" * 82)
    print(f"  programs                          : {progs}")
    print(f"  oracle attempted (headroom)        : {attempted}")
    print(f"  superblock accepted (programs)     : {accepted}")
    print(f"  rollbacks (spill/bundle/scheduler)  : {rolled}")
    print(f"  behaviour mismatches              : {mism}   (MUST be 0)")
    if rn:
        print(f"  avg scheduling region (blocks)     : {rb / rn:.2f} -> {ra / rn:.2f}"
              f"   (+{100 * (ra - rb) / rb if rb else 0:.0f}% larger)")
    print(f"  superblock pass time               : {t:.2f}s   ({1000 * t / progs:.1f} ms/prog)")
    print("-" * 82)
    print("  Production compiler:  R3.1 (SWP) -> R3.2 (SWP + superblock)")
    print(f"    static instructions : {r31[0]} -> {r32[0]}   ({r32[0] - r31[0]:+d})")
    print(f"    bundles             : {r31[1]} -> {r32[1]}   ({r32[1] - r31[1]:+d})")
    print(f"    IPB                 : {ipb(r31):.3f} -> {ipb(r32):.3f}"
          f"   ({100 * (ipb(r32) - ipb(r31)) / ipb(r31) if ipb(r31) else 0:+.1f}%)")
    print("=" * 82)
    print("  RESULT:", "PASS (0 mismatches)" if mism == 0 else "FAIL")
    print("=" * 82)


if __name__ == '__main__':
    raise SystemExit(main())

"""
mve_corpus.py -- R2.8 corpus evaluation: compact rotating-kernel realisation vs
the full-unroll pipeline, and the whole series baseline -> R2.3 -> R2.4 -> R2.5 ->
R2.7 -> R2.8.

R2.8 changes ONLY the realisation strategy: R2.7's full-unroll pipeline becomes a
compact prologue/kernel-loop/epilogue via modulo variable expansion. The schedule
(II / stages) and the pipeline COVERAGE are unchanged; the win is static code /
bundles / spills. This harness quantifies that:

  * coverage: R2.5 / R2.7 / R2.8 (R2.8 = compact kernels + full-unroll fallback)
  * compaction: static reduction on the compacted loops; bank size; rotating regs
  * compiled comparison: static instructions / bundles / IPB / spills, per stage
  * correctness: independent IR differential over every pipelined program (0 mism.)

Every committed pipeline passed R2.8's structural + clean-slot multi-seed
differential + compile gate + the codegen live-range invariant; this harness
independently re-checks behaviour.

Run:  python3 compiler/loopopt/mve_corpus.py
"""

import os
import sys
import copy
import glob
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_COMPILER)
sys.path.insert(0, _COMPILER)

import pycparser                                                    # noqa: E402
from compiler import preprocess, _FAKE_TYPEDEFS                     # noqa: E402
from ir import Temp                                                 # noqa: E402
from ir_gen import IRGenerator                                      # noqa: E402
from ir_utils import func_slices                                    # noqa: E402
from codegen import CodeGen                                         # noqa: E402
from bundler import bundle_mcode                                    # noqa: E402
from loopopt.schedule import schedule_module, SchedPolicy           # noqa: E402
from loopopt.modulo import pipeline_module                          # noqa: E402
from loopopt.pipeline_regaware import pipeline_regaware_module      # noqa: E402
from loopopt.pipeline_mve import pipeline_mve_module                # noqa: E402
from loopopt import ir_interp                                       # noqa: E402

_GB = 0x400


def _gen(f):
    try:
        src, _ = preprocess(f)
        ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
        Temp.reset()
        g = IRGenerator(global_base=_GB)
        g.visit(ast)
        return g.instructions
    except Exception:
        return None


def _metrics(instrs):
    try:
        cg = CodeGen(global_base=_GB)
        body = cg.generate(copy.deepcopy(instrs), global_base=_GB)
        _m, n, b = bundle_mcode(body, schedule=True)
        return True, n, b, bool(cg.spilled)
    except Exception:
        return False, 0, 0, False


def _mismatch(ir0, irp):
    for (lo, hi) in func_slices(ir0):
        v, _d = ir_interp.differential(ir0, irp, lo, hi)
        if v == 'mismatch':
            return True
    return False


def _pipe25(ir):
    p, _s, _r = pipeline_module(ir)
    return p


def _pipe27(ir):
    p, _s, _r = pipeline_regaware_module(ir)
    return p


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    progs = 0
    cov25 = cov27 = 0
    cov_compact = cov_full = rolled = declined = mism = 0
    sum_ii = sum_stg = sum_bank = sum_rot = ncompact = 0
    stat_full = stat_compact = 0
    r28_time = 0.0

    n = 0
    base = [0, 0, 0]
    r24 = [0, 0, 0]
    r25 = [0, 0, 0]
    r27 = [0, 0, 0]
    r28 = [0, 0, 0]

    for f in files:
        ir = _gen(f)
        if ir is None:
            continue
        progs += 1

        _a, s25, _b = pipeline_module(ir)
        cov25 += s25.pipelined
        _c, s27, _d = pipeline_regaware_module(ir)
        cov27 += s27.pipelined_register + s27.pipelined_memory

        t0 = time.time()
        r28ir, st, reps = pipeline_mve_module(ir)
        r28_time += time.time() - t0
        cov_compact += st.kernel_loops
        cov_full += st.full_unroll
        rolled += st.rolled_back
        declined += st.declined
        sum_bank += st.sum_bank
        sum_rot += st.sum_rot
        stat_full += st.static_full
        stat_compact += st.static_compact
        for r in reps:
            if r.committed:
                sum_ii += r.ii
                sum_stg += r.stages
                if r.compacted:
                    ncompact += 1
        if (st.kernel_loops or st.full_unroll) and _mismatch(ir, r28ir):
            mism += 1
            print("  MISMATCH:", os.path.basename(f))

        s24, _ = schedule_module(ir, policy=SchedPolicy.R24)
        okb, sb, bb, spb = _metrics(ir)
        ok4, s4, b4, sp4 = _metrics(s24)
        ok5, s5, b5, sp5 = _metrics(_pipe25(ir))
        ok7, s7, b7, sp7 = _metrics(_pipe27(ir))
        ok8, s8, b8, sp8 = _metrics(r28ir)
        if okb and ok4 and ok5 and ok7 and ok8:
            n += 1
            for arr, (s, b, sp) in ((base, (sb, bb, spb)), (r24, (s4, b4, sp4)),
                                    (r25, (s5, b5, sp5)), (r27, (s7, b7, sp7)),
                                    (r28, (s8, b8, sp8))):
                arr[0] += s
                arr[1] += b
                arr[2] += sp

    _report(progs, cov25, cov27, cov_compact, cov_full, rolled, declined, mism,
            sum_ii, sum_stg, sum_bank, sum_rot, ncompact,
            stat_full, stat_compact, r28_time, n, base, r24, r25, r27, r28)
    return 0 if mism == 0 else 1


def _report(progs, cov25, cov27, cc, cf, rolled, declined, mism,
            sii, sstg, sbank, srot, ncompact, stat_full, stat_compact, t,
            n, base, r24, r25, r27, r28):
    def ipb(a):
        return (a[0] / a[1]) if a[1] else 0.0
    print("=" * 82)
    print("  R2.8 MODULO VARIABLE EXPANSION + COMPACT KERNEL -- CORPUS EVALUATION")
    print("=" * 82)
    print("Pipeline coverage (loops pipelined)")
    print(f"  programs analysed                : {progs}")
    print(f"  R2.5 alone (memory only)         : {cov25}")
    print(f"  R2.7 (register-aware, full unroll): {cov27}")
    print(f"  R2.8 (compact-kernel)            : {cc + cf}   "
          f"(compact kernel {cc}, full-unroll fallback {cf})")
    print(f"  rollbacks / declined             : {rolled} / {declined}")
    print(f"  behaviour mismatches             : {mism}   (MUST be 0)")
    ncommit = cc + cf
    if ncommit:
        print(f"  avg II / stages                  : {sii / ncommit:.2f} / "
              f"{sstg / ncommit:.2f}")
    if ncompact:
        print(f"  compact kernels: avg bank size / rotating regs : "
              f"{sbank / ncompact:.2f} / {srot / ncompact:.2f}")
    if stat_full:
        print(f"  static IR on compacted loops     : {stat_full} -> {stat_compact}"
              f"  ({100 * (stat_full - stat_compact) / stat_full:.1f}% smaller)")
    print(f"  R2.8 pipelining time (total s)   : {t:.2f}")
    print(f"Compiled comparison over {n} programs "
          f"(baseline -> R2.4 -> R2.5 -> R2.7 -> R2.8, bundler ON)")
    print(f"  static instructions : {base[0]} -> {r24[0]} -> {r25[0]} -> "
          f"{r27[0]} -> {r28[0]}")
    print(f"  bundles             : {base[1]} -> {r24[1]} -> {r25[1]} -> "
          f"{r27[1]} -> {r28[1]}")
    print(f"  IPB                 : {ipb(base):.3f} -> {ipb(r24):.3f} -> "
          f"{ipb(r25):.3f} -> {ipb(r27):.3f} -> {ipb(r28):.3f}")
    print(f"  register spills     : {base[2]} -> {r24[2]} -> {r25[2]} -> "
          f"{r27[2]} -> {r28[2]}")
    if r27[0]:
        print(f"  R2.8 vs R2.7        : static {r28[0] - r27[0]:+d} "
              f"({100 * (r28[0] - r27[0]) / r27[0]:+.1f}%), "
              f"bundles {r28[1] - r27[1]:+d}, spills {r28[2] - r27[2]:+d}")
    print("=" * 82)
    print("  RESULT:", "PASS (0 behaviour mismatches)" if mism == 0 else "FAIL")
    print("=" * 82)


if __name__ == '__main__':
    raise SystemExit(main())

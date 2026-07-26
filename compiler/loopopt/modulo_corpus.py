"""
modulo_corpus.py -- R2.5 corpus evaluation + baseline->R2.3->R2.4->R2.5.

Runs the full software-pipelining driver over the corpus and reports the required
ledger (loop coverage, MII analysis, pipelined / declined / rolled-back counts,
prologue/kernel/epilogue sizes), then compiles baseline, R2.3-scheduled,
R2.4-scheduled and R2.5-pipelined IR through the SAME production CodeGen +
bundler and compares bundles / IPB / static instructions / spills / compile time.

Every committed pipeline has already passed structural + multi-seed differential +
compile validation inside pipeline_module; this harness independently re-checks
behaviour (original vs pipelined) with the differential oracle.

Run:  python3 compiler/loopopt/modulo_corpus.py
"""

import os
import sys
import copy
import glob
import time
from collections import Counter

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
from loopopt.modulo import pipeline_module, analyze_module          # noqa: E402
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


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    progs = 0
    loops = elig = 0
    rec_bound = res_bound = 0
    mii_hist = Counter()
    pipelined = declined = rolled = 0
    decline_reasons = Counter()
    sum_stages = sum_kern = sum_pro = sum_epi = 0
    mismatches = compile_fail = 0
    pl_time = 0.0

    n = 0
    base = [0, 0, 0]      # static, bundles, spills
    r23 = [0, 0, 0]
    r24 = [0, 0, 0]
    r25 = [0, 0, 0]

    for f in files:
        ir = _gen(f)
        if ir is None:
            continue
        progs += 1

        for m in analyze_module(ir):
            loops += 1
            if not m.eligible:
                continue
            elig += 1
            mii_hist[m.mii] += 1
            if m.rec_mii > m.res_mii:
                rec_bound += 1
            elif m.res_mii > m.rec_mii:
                res_bound += 1

        t0 = time.time()
        piped, pstats, _res = pipeline_module(ir)
        pl_time += time.time() - t0
        pipelined += pstats.pipelined
        declined += pstats.declined
        rolled += pstats.rolled_back
        decline_reasons.update(pstats.decline_reasons)
        sum_stages += pstats.sum_stages
        sum_kern += pstats.sum_kernel
        sum_pro += pstats.sum_prologue
        sum_epi += pstats.sum_epilogue

        if pstats.pipelined and _mismatch(ir, piped):
            mismatches += 1

        s23, _ = schedule_module(ir, policy=SchedPolicy.R23)
        s24, _ = schedule_module(ir, policy=SchedPolicy.R24)

        okb, sb, bb, spb = _metrics(ir)
        ok3, s3, b3, sp3 = _metrics(s23)
        ok4, s4, b4, sp4 = _metrics(s24)
        ok5, s5, b5, sp5 = _metrics(piped)
        if not ok5:
            compile_fail += 1
        if okb and ok3 and ok4 and ok5:
            n += 1
            for arr, (s, b, sp) in ((base, (sb, bb, spb)), (r23, (s3, b3, sp3)),
                                    (r24, (s4, b4, sp4)), (r25, (s5, b5, sp5))):
                arr[0] += s; arr[1] += b; arr[2] += sp

    _report(progs, loops, elig, rec_bound, res_bound, mii_hist, pipelined,
            declined, rolled, decline_reasons, sum_stages, sum_kern, sum_pro,
            sum_epi, mismatches, compile_fail, pl_time, n, base, r23, r24, r25)
    ok = mismatches == 0 and compile_fail == 0
    return 0 if ok else 1


def _report(progs, loops, elig, recb, resb, mii_hist, pipelined, declined,
            rolled, dreasons, stages, kern, pro, epi, mism, cfail, pltime, n,
            base, r23, r24, r25):
    def ipb(a):
        return (a[0] / a[1]) if a[1] else 0.0
    print("=" * 80)
    print("  R2.5 SOFTWARE PIPELINING (MODULO SCHEDULING) -- CORPUS EVALUATION")
    print("=" * 80)
    print("Phase 1 -- MII analysis (mutation-free)")
    print(f"  programs / loops / eligible : {progs} / {loops} / {elig}")
    print(f"  MII bound-by  Rec / Res     : {recb} / {resb}")
    print(f"  MII histogram               : {dict(sorted(mii_hist.items()))}")
    print("Phase 3-4 -- pipeline generation (structural + differential + compile gated)")
    print(f"  loops pipelined             : {pipelined}")
    print(f"  loops declined              : {declined}")
    print(f"  loops rolled back           : {rolled}")
    print(f"  decline/rollback reasons    : {dict(dreasons)}")
    print(f"  behaviour mismatches        : {mism}   (MUST be 0)")
    print(f"  compile failures            : {cfail}   (MUST be 0)")
    if pipelined:
        print(f"  avg stages / kernel / pro / epi : "
              f"{stages / pipelined:.1f} / {kern / pipelined:.0f} / "
              f"{pro / pipelined:.0f} / {epi / pipelined:.0f}")
    print(f"  pipelining time (total s)   : {pltime:.2f}")
    print(f"Measurements over {n} programs (baseline -> R2.3 -> R2.4 -> R2.5)")
    print(f"  static instructions        : {base[0]} -> {r23[0]} -> {r24[0]} -> {r25[0]}")
    print(f"  bundles                    : {base[1]} -> {r23[1]} -> {r24[1]} -> {r25[1]}")
    print(f"  IPB                        : {ipb(base):.3f} -> {ipb(r23):.3f} -> "
          f"{ipb(r24):.3f} -> {ipb(r25):.3f}")
    print(f"  register spills            : {base[2]} -> {r23[2]} -> {r24[2]} -> {r25[2]}")
    ok = mism == 0 and cfail == 0
    print("=" * 80)
    print("  RESULT:", "PASS (0 behaviour mismatches / 0 compile failures)"
          if ok else "FAIL")
    print("=" * 80)


if __name__ == '__main__':
    raise SystemExit(main())

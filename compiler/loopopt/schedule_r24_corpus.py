"""
schedule_r24_corpus.py -- R2.4 corpus evaluation + R2.3-vs-R2.4 comparison.

Schedules the whole corpus under BOTH the R2.3 policy (unit-latency height,
index-only tie-break) and the R2.4 policy (latency-weighted critical path +
register-pressure tie-break + bundle-fill tie-break), compiles the baseline and
both scheduled forms through the SAME production CodeGen + bundler, and reports
the correctness ledger plus every required measurement.

Measured: bundles (production bundler ON), IPB, schedule length (= bundles),
average dependency height, register-pressure estimate, spill count, instruction
movement, and scheduling (compile) time -- for R2.3 and R2.4.

Run:  python3 compiler/loopopt/schedule_r24_corpus.py
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
    """(ok, static_ops, bundles_on, spilled)."""
    try:
        cg = CodeGen(global_base=_GB)
        body = cg.generate(copy.deepcopy(instrs), global_base=_GB)
        _m, n_on, b_on = bundle_mcode(body, schedule=True)
        return True, n_on, b_on, bool(cg.spilled)
    except Exception:
        return False, 0, 0, False


def _mismatches(ir0, irs):
    n = 0
    for (lo, hi) in func_slices(ir0):
        v, _d = ir_interp.differential(ir0, irs, lo, hi)
        if v == 'mismatch':
            n += 1
    return n


class Side:
    def __init__(self):
        self.changed = self.blocks_reordered = self.instrs_reordered = 0
        self.rollbacks = self.structural = 0
        self.sec = 0.0
        self.static = self.bundles = self.spills = 0
        self.crit = self.press = self.move = self.mmax = 0
        self.mblocks = self.ready_sum = self.ready_steps = 0
        self.est_i = self.est_b = 0
        self.mism = 0


def _absorb_side(side, st, sec):
    side.changed += st.functions_changed
    side.blocks_reordered += st.blocks_reordered
    side.instrs_reordered += st.instrs_reordered
    side.rollbacks += st.rollbacks
    side.structural += st.structural_failures
    side.crit += st.crit_path_total
    side.press += st.pressure_peak_sum
    side.move += st.movement_sum
    side.mmax = max(side.mmax, st.movement_max)
    side.mblocks += st.metriced_blocks
    side.ready_sum += st.ready_size_sum
    side.ready_steps += st.ready_steps
    side.est_i += st.est_instrs
    side.est_b += st.est_bundles
    side.sec += sec


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    programs = 0
    base_static = base_bundles = base_spills = 0
    r23, r24 = Side(), Side()
    n_meas = 0

    for f in files:
        ir0 = _gen(f)
        if ir0 is None:
            continue
        programs += 1

        t0 = time.time()
        s23, st23 = schedule_module(ir0, policy=SchedPolicy.R23)
        _absorb_side(r23, st23, time.time() - t0)
        t0 = time.time()
        s24, st24 = schedule_module(ir0, policy=SchedPolicy.R24)
        _absorb_side(r24, st24, time.time() - t0)

        r23.mism += _mismatches(ir0, s23)
        r24.mism += _mismatches(ir0, s24)

        okb, sb, bb, spb = _metrics(ir0)
        ok3, s3, b3, sp3 = _metrics(s23)
        ok4, s4, b4, sp4 = _metrics(s24)
        if okb and ok3 and ok4:
            n_meas += 1
            base_static += sb; base_bundles += bb; base_spills += spb
            r23.static += s3; r23.bundles += b3; r23.spills += sp3
            r24.static += s4; r24.bundles += b4; r24.spills += sp4

    _report(programs, n_meas, base_static, base_bundles, base_spills, r23, r24)
    ok = (r23.mism == 0 and r24.mism == 0 and r23.rollbacks == 0
          and r24.rollbacks == 0 and r23.structural == 0 and r24.structural == 0)
    return 0 if ok else 1


def _report(programs, n, bs, bb, bsp, r23, r24):
    def ipb(s, b):
        return (s / b) if b else 0.0

    def avg(a, b):
        return (a / b) if b else 0.0
    print("=" * 80)
    print("  R2.4 SCHEDULER QUALITY -- CORPUS EVALUATION + R2.3 vs R2.4")
    print("=" * 80)
    print("Correctness ledger (both must be all-zero)")
    print(f"  programs analysed          : {programs}")
    print(f"                                 R2.3        R2.4")
    print(f"  functions changed          : {r23.changed:<11} {r24.changed}")
    print(f"  blocks reordered           : {r23.blocks_reordered:<11} {r24.blocks_reordered}")
    print(f"  instructions reordered     : {r23.instrs_reordered:<11} {r24.instrs_reordered}")
    print(f"  rollbacks (differential)   : {r23.rollbacks:<11} {r24.rollbacks}")
    print(f"  structural verifier fails  : {r23.structural:<11} {r24.structural}")
    print(f"  behaviour mismatches       : {r23.mism:<11} {r24.mism}")
    print(f"Measurements over {n} programs (baseline -> R2.3 -> R2.4, bundler ON)")
    print(f"  static instructions        : {bs} -> {r23.static} -> {r24.static}")
    print(f"  bundles (schedule length)  : {bb} -> {r23.bundles} -> {r24.bundles}")
    print(f"  IPB                        : {ipb(bs, bb):.3f} -> "
          f"{ipb(r23.static, r23.bundles):.3f} -> {ipb(r24.static, r24.bundles):.3f}")
    print(f"  register spills            : {bsp} -> {r23.spills} -> {r24.spills}")
    print("Scheduling statistics (R2.3 vs R2.4)")
    print(f"  avg dependency height      : {avg(r23.crit, r23.mblocks):.2f} (unit) vs "
          f"{avg(r24.crit, r24.mblocks):.2f} (latency-weighted)")
    print(f"  avg register-pressure est  : {avg(r23.press, r23.mblocks):.2f} vs "
          f"{avg(r24.press, r24.mblocks):.2f}  (peak live/block)")
    print(f"  avg ready-list size        : {avg(r23.ready_sum, r23.ready_steps):.2f} vs "
          f"{avg(r24.ready_sum, r24.ready_steps):.2f}")
    print(f"  instruction movement (sum) : {r23.move} vs {r24.move}   "
          f"(max {r23.mmax} vs {r24.mmax})")
    print(f"  est bundle utilisation     : {ipb(r23.est_i, r23.est_b):.3f} vs "
          f"{ipb(r24.est_i, r24.est_b):.3f}  (instrs/bundle, dep-free upper bound)")
    print(f"  scheduling time (total s)  : {r23.sec:.2f} vs {r24.sec:.2f}")
    ok = (r23.mism == 0 and r24.mism == 0 and r23.rollbacks == 0
          and r24.rollbacks == 0 and r23.structural == 0 and r24.structural == 0)
    print("=" * 80)
    print("  RESULT:", "PASS (0 mismatches / 0 rollbacks / 0 verifier failures, both "
          "policies)" if ok else "FAIL")
    print("=" * 80)


if __name__ == '__main__':
    raise SystemExit(main())

"""
regaware_corpus.py -- R2.7 corpus evaluation + baseline->R2.3->..->R2.7.

Runs register-aware software pipelining over the corpus and reports pipeline
coverage (register vs memory form), RecMII / II / stages, and the compiled
comparison (bundles / IPB / static / spills / compile time). It quantifies the
headline result: recovering (and exceeding) the pipeline coverage lost after R2.6
by exploiting the shorter REGISTER recurrence.

Coverage reference points:
  R2.5 alone : pipeline_module(original)          -- memory recurrences only
  R2.6 -> R2.5: pipeline_module(promote_module(original))  -- register loops rejected
  R2.7       : pipeline_regaware_module(original) -- both forms, register preferred

Every committed pipeline passed R2.7's structural + clean-slot multi-seed
differential + compile gate; this harness independently re-checks behaviour.

Run:  python3 compiler/loopopt/regaware_corpus.py
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
from loopopt.loop_promote import promote_module                     # noqa: E402
from loopopt.pipeline_regaware import pipeline_regaware_module      # noqa: E402
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
    cov_r25 = cov_r26 = cov_reg = cov_mem = 0
    rolled = mismatches = 0
    sum_ii = sum_rec = sum_stages = ncommit = 0
    r27_time = 0.0

    n = 0
    base = [0, 0, 0]
    r24 = [0, 0, 0]
    r25 = [0, 0, 0]
    r27 = [0, 0, 0]

    for f in files:
        ir = _gen(f)
        if ir is None:
            continue
        progs += 1

        # coverage reference points
        _a, s25, _b = pipeline_module(ir)
        cov_r25 += s25.pipelined
        prom, _pst, _pr = promote_module(ir)
        _c, s26, _d = pipeline_module(prom)
        cov_r26 += s26.pipelined

        t0 = time.time()
        r27ir, st, reps = pipeline_regaware_module(ir)
        r27_time += time.time() - t0
        cov_reg += st.pipelined_register
        cov_mem += st.pipelined_memory
        rolled += st.rolled_back
        for r in reps:
            if r.committed:
                ncommit += 1
                sum_ii += r.ii
                sum_rec += r.rec_mii
                sum_stages += r.stages
        if (st.pipelined_register or st.pipelined_memory) and _mismatch(ir, r27ir):
            mismatches += 1

        s24, _ = schedule_module(ir, policy=SchedPolicy.R24)
        okb, sb, bb, spb = _metrics(ir)
        ok4, s4, b4, sp4 = _metrics(s24)
        ok5, s5, b5, sp5 = _metrics(_gen_pipe(ir))
        ok7, s7, b7, sp7 = _metrics(r27ir)
        if okb and ok4 and ok5 and ok7:
            n += 1
            for arr, (s, b, sp) in ((base, (sb, bb, spb)), (r24, (s4, b4, sp4)),
                                    (r25, (s5, b5, sp5)), (r27, (s7, b7, sp7))):
                arr[0] += s; arr[1] += b; arr[2] += sp

    _report(progs, cov_r25, cov_r26, cov_reg, cov_mem, rolled, mismatches,
            sum_ii, sum_rec, sum_stages, ncommit, r27_time, n, base, r24, r25, r27)
    return 0 if mismatches == 0 else 1


def _gen_pipe(ir):
    p, _s, _r = pipeline_module(ir)
    return p


def _report(progs, r25, r26, creg, cmem, rolled, mism, sii, srec, sstg, ncommit,
            t, n, base, r24, r25m, r27):
    def ipb(a):
        return (a[0] / a[1]) if a[1] else 0.0
    print("=" * 80)
    print("  R2.7 REGISTER-AWARE SOFTWARE PIPELINING -- CORPUS EVALUATION")
    print("=" * 80)
    print("Pipeline coverage (loops pipelined)")
    print(f"  programs analysed                : {progs}")
    print(f"  R2.5 alone (memory only)         : {r25}")
    print(f"  R2.6 -> R2.5 (register rejected)  : {r26}")
    print(f"  R2.7 (register-aware)            : {creg + cmem}   "
          f"(register form {creg}, memory form {cmem})")
    print(f"  rollbacks                        : {rolled}")
    print(f"  behaviour mismatches             : {mism}   (MUST be 0)")
    if ncommit:
        print(f"  avg RecMII / II / stages         : {srec / ncommit:.2f} / "
              f"{sii / ncommit:.2f} / {sstg / ncommit:.2f}")
    print(f"  pipelining time (total s)        : {t:.2f}")
    print(f"Measurements over {n} programs (baseline -> R2.4 -> R2.5 -> R2.7, bundler ON)")
    print(f"  static instructions : {base[0]} -> {r24[0]} -> {r25m[0]} -> {r27[0]}")
    print(f"  bundles             : {base[1]} -> {r24[1]} -> {r25m[1]} -> {r27[1]}")
    print(f"  IPB                 : {ipb(base):.3f} -> {ipb(r24):.3f} -> {ipb(r25m):.3f}"
          f" -> {ipb(r27):.3f}")
    print(f"  register spills     : {base[2]} -> {r24[2]} -> {r25m[2]} -> {r27[2]}")
    print("=" * 80)
    print("  RESULT:", "PASS (0 behaviour mismatches)" if mism == 0 else "FAIL")
    print("=" * 80)


if __name__ == '__main__':
    raise SystemExit(main())

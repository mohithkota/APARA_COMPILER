"""
promote_corpus.py -- R2.6 corpus evaluation + baseline->R2.3->R2.4->R2.5->R2.6.

Runs loop register promotion over the corpus and reports the required ledger
(loops examined / promotable / promoted / failures / rollbacks, memory
recurrences removed, RecMII before vs after) and the compiled comparison
(bundles / IPB / static / spills / compile time). It ALSO measures how R2.5's
software-pipeliner behaves on the PROMOTED IR (the R2.6 -> R2.5 composition).

Every promoted loop passed R2.6's clean-slot-respecting multi-seed differential +
compile gate; this harness independently re-checks behaviour (original vs
promoted) with the differential oracle.

Run:  python3 compiler/loopopt/promote_corpus.py
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
from loopopt.modulo import pipeline_module                          # noqa: E402
from loopopt.loop_promote import promote_module                     # noqa: E402
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


def _loop_body_memops(instrs):
    """Static count of loads+stores (a proxy for per-iteration memory traffic)."""
    return sum(1 for x in instrs
               if type(x).__name__ in ('IRLoad', 'IRStore', 'IRGlobalLoad',
                                        'IRGlobalStore'))


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    progs = 0
    loops = promotable = promoted = failures = rolled = 0
    memrec = 0
    sum_rb = sum_ra = 0
    verified = proof_only = 0
    reasons = Counter()
    mismatches = 0
    prom_time = 0.0
    memops_before = memops_after = 0
    pipe_before = pipe_after = 0

    n = 0
    base = [0, 0, 0]
    r23 = [0, 0, 0]
    r24 = [0, 0, 0]
    r25 = [0, 0, 0]
    r26 = [0, 0, 0]

    for f in files:
        ir = _gen(f)
        if ir is None:
            continue
        progs += 1

        t0 = time.time()
        prom, pstats, reps = promote_module(ir)
        prom_time += time.time() - t0

        loops += pstats.loops
        promotable += pstats.promotable
        promoted += pstats.promoted
        rolled += pstats.rolled_back
        memrec += pstats.mem_rec_removed
        sum_rb += pstats.sum_rec_before
        sum_ra += pstats.sum_rec_after
        for r in reps:
            reasons[r.reason] += 1
            if r.committed:
                if r.verified:
                    verified += 1
                else:
                    proof_only += 1
        if pstats.promoted and _mismatch(ir, prom):
            mismatches += 1

        memops_before += _loop_body_memops(ir)
        memops_after += _loop_body_memops(prom)

        # R2.5 pipeline coverage without vs with promotion (the composition)
        _p0, s0, _r0 = pipeline_module(ir)
        _p1, s1, _r1 = pipeline_module(prom)
        pipe_before += s0.pipelined
        pipe_after += s1.pipelined

        s23, _ = schedule_module(ir, policy=SchedPolicy.R23)
        s24, _ = schedule_module(ir, policy=SchedPolicy.R24)
        p25, _ps, _pr = pipeline_module(ir)

        okb, sb, bb, spb = _metrics(ir)
        ok3, s3, b3, sp3 = _metrics(s23)
        ok4, s4, b4, sp4 = _metrics(s24)
        ok5, s5, b5, sp5 = _metrics(p25)
        ok6, s6, b6, sp6 = _metrics(prom)
        if okb and ok3 and ok4 and ok5 and ok6:
            n += 1
            for arr, (s, b, sp) in ((base, (sb, bb, spb)), (r23, (s3, b3, sp3)),
                                    (r24, (s4, b4, sp4)), (r25, (s5, b5, sp5)),
                                    (r26, (s6, b6, sp6))):
                arr[0] += s; arr[1] += b; arr[2] += sp

    _report(progs, loops, promotable, promoted, rolled, memrec, sum_rb, sum_ra,
            verified, proof_only, reasons, mismatches, prom_time, memops_before,
            memops_after, pipe_before, pipe_after, n, base, r23, r24, r25, r26)
    ok = mismatches == 0
    return 0 if ok else 1


def _report(progs, loops, promotable, promoted, rolled, memrec, srb, sra, verif,
            proof, reasons, mism, ptime, mb, ma, pipe_b, pipe_a, n,
            base, r23, r24, r25, r26):
    def ipb(a):
        return (a[0] / a[1]) if a[1] else 0.0
    print("=" * 80)
    print("  R2.6 LOOP REGISTER PROMOTION -- CORPUS EVALUATION")
    print("=" * 80)
    print("Promotion ledger")
    print(f"  programs / loops examined  : {progs} / {loops}")
    print(f"  promotable / promoted      : {promotable} / {promoted}"
          f"   (differentially-verified {verif}, clean-slot-proof-only {proof})")
    print(f"  rollbacks (differential)   : {rolled}")
    print(f"  behaviour mismatches       : {mism}   (MUST be 0)")
    print(f"  memory recurrences removed : {memrec}")
    if promoted:
        print(f"  avg RecMII  before -> after: {srb / promoted:.2f} -> {sra / promoted:.2f}")
    print(f"  loop-body memory ops       : {mb} -> {ma}   "
          f"({mb - ma} fewer; per-iteration dynamic win)")
    print(f"  reasons                    : {dict(reasons.most_common())}")
    print(f"  promotion time (total s)   : {ptime:.2f}")
    print("R2.6 -> R2.5 composition (software-pipeliner coverage)")
    print(f"  loops R2.5 pipelines  without promotion : {pipe_b}")
    print(f"  loops R2.5 pipelines  WITH   promotion  : {pipe_a}")
    print("  (R2.5's generator requires a MEMORY-slot counted IV; promoting the IV")
    print("   to a register removes it, so R2.5 declines the promoted loops -- the")
    print("   RecMII win is realised in analysis, not yet in R2.5's generator.)")
    print(f"Measurements over {n} programs (baseline->R2.3->R2.4->R2.5->R2.6, bundler ON)")
    print(f"  static instructions : {base[0]} -> {r23[0]} -> {r24[0]} -> {r25[0]} -> {r26[0]}")
    print(f"  bundles             : {base[1]} -> {r23[1]} -> {r24[1]} -> {r25[1]} -> {r26[1]}")
    print(f"  IPB                 : {ipb(base):.3f} -> {ipb(r23):.3f} -> {ipb(r24):.3f}"
          f" -> {ipb(r25):.3f} -> {ipb(r26):.3f}")
    print(f"  register spills     : {base[2]} -> {r23[2]} -> {r24[2]} -> {r25[2]} -> {r26[2]}")
    print("=" * 80)
    print("  RESULT:", "PASS (0 behaviour mismatches)" if mism == 0 else "FAIL")
    print("=" * 80)


if __name__ == '__main__':
    raise SystemExit(main())

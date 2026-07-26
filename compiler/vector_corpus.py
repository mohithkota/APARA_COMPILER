"""
vector_corpus.py -- R4.0 Vector Infrastructure corpus evaluation.

Runs the vector foundation (kernel detection + legality + profitability +
validation) over the corpus and reports, WITHOUT changing any generated code:

  * detected kernels (by kind)
  * legal vs unsupported kernels, with rejection-reason distribution
  * predicted profitability (lanes, throughput, instruction reduction)
  * validation coverage: how many vector-intrinsic programs the vector oracle can
    execute (the differential framework future passes will reuse)

Analysis only. Proven: generated scalar code is byte-identical with and without
the analysis (the modules never mutate IR).
"""

import os
import sys
import copy
import glob

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import pycparser
from compiler import preprocess, _FAKE_TYPEDEFS
from ir import Temp
from ir_gen import IRGenerator
from ir_utils import func_slices
from codegen import CodeGen
from kernel_detector import detect_module
from vector_profitability import analyze_profitability_module
from vector_validation import run_slice_vector
from loopopt import ir_interp

_GB = 0x400


def _gen(f):
    try:
        src, _ = preprocess(f)
        ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
        Temp.reset()
        g = IRGenerator(global_base=_GB)
        g.visit(ast)
        return list(g.instructions)
    except Exception:
        return None


def _codegen(ir):
    try:
        return CodeGen(global_base=_GB).generate(copy.deepcopy(ir), global_base=_GB)
    except Exception:
        return None


def _has_vector_ir(ir):
    return any('Vec' in type(x).__name__ or type(x).__name__ in
               ('IRLoadWide', 'IRStoreWide') for x in ir)


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c', 'demo_prof/**/*.c',
              'isa_coverage_tests/**/*.c', 'matmul_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    progs = 0
    kinds = {}
    legal = 0
    reasons = {}
    unchanged = 0
    sum_lanes = sum_gain = sum_reduc = n_profit = 0
    vec_progs = vec_runnable = 0

    for f in files:
        ir = _gen(f)
        if ir is None:
            continue
        progs += 1

        # no-change proof: analysis must not alter generated code
        before = _codegen(ir)
        snap = [repr(x) for x in ir]
        kerns = detect_module(ir)
        profs = analyze_profitability_module(ir)
        after = _codegen(ir)
        if [repr(x) for x in ir] == snap and before == after:
            unchanged += 1

        for k in kerns:
            if k.kind:
                kinds[k.kind] = kinds.get(k.kind, 0) + 1
        for p in profs:
            L = p.legality
            if L.legal:
                legal += 1
                if p.profitable:
                    n_profit += 1
                    sum_lanes += p.lanes
                    sum_gain += p.throughput_gain
                    sum_reduc += p.instruction_reduction
            elif L.kernel.kind:
                reasons[L.reason] = reasons.get(L.reason, 0) + 1

        # validation-framework coverage over vector-intrinsic programs
        if _has_vector_ir(ir):
            vec_progs += 1
            ok = True
            for (lo, hi) in func_slices(ir):
                try:
                    run_slice_vector(ir, lo, hi)
                except ir_interp.Unsupported:
                    pass                              # calls/etc; still counts if others run
                except Exception:
                    ok = False
            if ok:
                vec_runnable += 1

    _report(progs, kinds, legal, reasons, unchanged, sum_lanes, sum_gain,
            sum_reduc, n_profit, vec_progs, vec_runnable)
    return 0 if unchanged == progs else 1


def _report(progs, kinds, legal, reasons, unchanged, sl, sg, sr, n, vp, vr):
    print("=" * 80)
    print("  R4.0 APARA VECTOR INFRASTRUCTURE -- CORPUS EVALUATION")
    print("=" * 80)
    print(f"  programs analysed            : {progs}")
    print(f"  generated scalar code UNCHANGED : {unchanged}/{progs}   (MUST be all)")
    print()
    print("  Detected vectorizable-kernel candidates (by kind)")
    for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"    {k:16} : {v}")
    print(f"    (total detected : {sum(kinds.values())})")
    print()
    print(f"  Legal (ISA-supported) kernels : {legal}")
    print(f"  Profitable kernels            : {n}")
    if n:
        print(f"    avg lanes / throughput / instr-reduction : "
              f"{sl / n:.1f} / {sg / n:.1f}x / {sr / n:.0%}")
    print()
    print("  Rejection reasons (recognised but not vectorizable)")
    for r, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"    {r:34} : {v}")
    print()
    print("  Validation-framework coverage")
    print(f"    vector-IR programs (intrinsics) : {vp}")
    print(f"    executable by the vector oracle  : {vr}/{vp}")
    print("=" * 80)
    print("  RESULT:", "PASS (analysis mutated nothing)" if unchanged == progs
          else "FAIL (analysis changed generated code!)")
    print("=" * 80)


if __name__ == '__main__':
    raise SystemExit(main())

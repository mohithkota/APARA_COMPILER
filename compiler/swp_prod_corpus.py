"""
swp_prod_corpus.py -- R3.1 corpus evaluation of PRODUCTION software pipelining.

Measures the production compiler WITH the R3.1 SWP integration against (a) the
current production compiler (SWP off) and (b) the standalone R2.8 pipeline. It
reconstructs the production-optimized IR exactly as `compile_c_to_mcode()` does
(the IVSR+LICM+loop-reg primary tier with the spill fallback), then applies
`apply_production_swp` and re-measures.

Reports: pipeline coverage, oracle utilization, rollback rate, production IPB /
bundles / static size / spills, compile-time overhead, and correctness (IR
differential, 0 mismatches required).

Run:  python3 compiler/swp_prod_corpus.py
"""

import os
import sys
import copy
import glob
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import pycparser                                                    # noqa: E402
from compiler import preprocess, _FAKE_TYPEDEFS                     # noqa: E402
from ir import Temp                                                 # noqa: E402
from ir_gen import IRGenerator                                      # noqa: E402
from codegen import CodeGen                                         # noqa: E402
from bundler import bundle_mcode                                    # noqa: E402
from ir_utils import func_slices                                   # noqa: E402

# the exact production passes (reused, not reimplemented)
from loopopt.pipeline import (induction_strength_reduce,            # noqa: E402
                              loop_invariant_code_motion)
from strength_reduce import strength_reduce                        # noqa: E402
from licm import hoist_loop_invariants                             # noqa: E402
from loop_reg import promote_loop_counters                         # noqa: E402
from copyprop import copy_propagate                                # noqa: E402
from coalesce import copy_coalesce                                 # noqa: E402
from dce import dead_code_eliminate                                # noqa: E402
from sccp import sparse_conditional_constant_propagation           # noqa: E402
from gvn import global_value_numbering                             # noqa: E402
from mem2reg import mem2reg                                        # noqa: E402

from production_swp import apply_production_swp                     # noqa: E402
from loopopt.pipeline_mve import pipeline_mve_module               # noqa: E402
from loopopt.oracle_ilp import analyze_module as oracle            # noqa: E402
from loopopt import ir_interp                                      # noqa: E402

_GB = 0x400


def _clean(x):
    return dead_code_eliminate(copy_coalesce(copy_propagate(x)))


def _cp(x):
    x = _clean(x)
    x = dead_code_eliminate(sparse_conditional_constant_propagation(x))
    x = global_value_numbering(x)
    x = mem2reg(x)
    x = loop_invariant_code_motion(x)
    return _clean(x)


def _sr(x):
    return strength_reduce(x)[0]


def _production_optimize(ir0):
    """Reconstruct the production-optimized IR: the IVSR+LICM+loop-reg primary
    tier, falling back to lighter tiers on spill (as compile_c_to_mcode does)."""
    base = _sr(list(ir0))
    tiers = [
        lambda: _cp(promote_loop_counters(hoist_loop_invariants(_sr(induction_strength_reduce(list(ir0)))))),
        lambda: _cp(promote_loop_counters(_sr(induction_strength_reduce(list(ir0))))),
        lambda: _cp(_sr(induction_strength_reduce(list(ir0)))),
        lambda: _cp(promote_loop_counters(hoist_loop_invariants(list(base)))),
        lambda: _cp(hoist_loop_invariants(list(base))),
        lambda: _cp(promote_loop_counters(list(base))),
    ]
    for build in tiers:
        try:
            instrs = build()
            cg = CodeGen(global_base=_GB)
            cg.generate(copy.deepcopy(instrs), global_base=_GB)
            if not cg.spilled:
                return instrs
        except Exception:
            continue
    return base


def _metrics(ir):
    try:
        cg = CodeGen(global_base=_GB)
        body = cg.generate(copy.deepcopy(ir), global_base=_GB)
        _m, n, b = bundle_mcode(body, schedule=True)
        return n, b, bool(cg.spilled)
    except Exception:
        return None


def _mismatch(ir0, final):
    for lo, hi in func_slices(ir0):
        v, _d = ir_interp.differential(ir0, final, lo, hi)
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
    oracle_reco = 0                 # loops the oracle recommends for SWP
    pipelined = rolled = mism = 0
    standalone_cov = 0
    swp_time = 0.0
    off = [0, 0, 0]                 # static, bundles, spill-progs
    on = [0, 0, 0]

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

        # oracle recommendation
        for r in oracle(ir0):
            if r.top_opportunity == 'software-pipelining' \
                    and dict(r.opportunities).get('software-pipelining', 0) >= 0.5:
                oracle_reco += 1

        # standalone R2.8 coverage (reference)
        try:
            _s, st, _r = pipeline_mve_module(ir0)
            standalone_cov += st.kernel_loops + st.full_unroll
        except Exception:
            pass

        prod = _production_optimize(ir0)
        m_off = _metrics(prod)
        t0 = time.time()
        final, recs, summ = apply_production_swp(ir0, prod, global_base=_GB)
        swp_time += time.time() - t0
        pipelined += summ.pipelined
        rolled += summ.rolled_back
        if final is not prod and _mismatch(ir0, final):
            mism += 1
            print("  MISMATCH:", os.path.basename(f))
        m_on = _metrics(final)
        if m_off and m_on:
            off[0] += m_off[0]; off[1] += m_off[1]; off[2] += int(m_off[2])
            on[0] += m_on[0]; on[1] += m_on[1]; on[2] += int(m_on[2])

    _report(progs, oracle_reco, pipelined, rolled, mism, standalone_cov,
            swp_time, off, on)
    return 0 if mism == 0 else 1


def _report(progs, reco, pipe, rolled, mism, standalone, t, off, on):
    def ipb(a):
        return a[0] / a[1] if a[1] else 0.0
    print("=" * 82)
    print("  R3.1 PRODUCTION SOFTWARE PIPELINING -- CORPUS EVALUATION")
    print("=" * 82)
    print(f"  programs                          : {progs}")
    print(f"  oracle SWP recommendations (loops) : {reco}")
    print(f"  standalone R2.8 coverage (loops)   : {standalone}")
    print(f"  PRODUCTION pipelined (accepted)    : {pipe}")
    print(f"  production rollbacks               : {rolled}")
    print(f"  oracle utilization (prod/reco)     : {100 * pipe / reco if reco else 0:.0f}%")
    print(f"  behaviour mismatches              : {mism}   (MUST be 0)")
    print(f"  SWP pass time (total, 124 progs)   : {t:.2f}s   ({1000 * t / progs:.1f} ms/prog)")
    print("-" * 82)
    print("  Production compiler:  SWP off -> SWP on")
    print(f"    static instructions : {off[0]} -> {on[0]}   ({on[0] - off[0]:+d})")
    print(f"    bundles             : {off[1]} -> {on[1]}   ({on[1] - off[1]:+d})")
    print(f"    IPB                 : {ipb(off):.3f} -> {ipb(on):.3f}")
    print(f"    programs that spill  : {off[2]} -> {on[2]}")
    print("=" * 82)
    print("  RESULT:", "PASS (0 mismatches, spill-safe)"
          if mism == 0 and on[2] <= off[2] else "CHECK")
    print("=" * 82)


if __name__ == '__main__':
    raise SystemExit(main())

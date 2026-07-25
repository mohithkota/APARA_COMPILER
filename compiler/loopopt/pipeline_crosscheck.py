"""
pipeline_crosscheck.py -- end-to-end proof that the M10-integrated optimization
pipeline is behaviourally identical to the legacy pipeline.

The production loop-opt stage (compiler.py) builds a fixed set of optimization
TIERS and picks the first that codegens without spilling. M10 changed exactly two
pass functions inside that stage: induction_strength_reduce and (opt-in)
loop_invariant_code_motion now execute through the LoopTransform framework. Every
other pass (power-of-two strength reduction, licm.hoist_loop_invariants,
loop_reg.promote_loop_counters, the copy-prop/coalesce/DCE/SCCP/GVN/mem2reg
cleanup) is unchanged.

This harness reconstructs that exact tier pipeline (mirroring compiler.py's
_clean/_cp/_sr/_ivsr and the _tiers list) parameterized by the IVSR / LICM
implementations, then runs it TWICE per corpus program -- once with the LEGACY
passes, once with the FRAMEWORK passes -- and compares:

  * per-tier IR                 (instruction-by-instruction, via repr)
  * per-tier generated code     (CodeGen body + spill status)
  * the selected tier           (first non-spilling), i.e. the final IR + code

It also aggregates the framework passes' verifier-failure and rollback counts.
Both runs reset ivsr._iv_n to the same value first, and evaluate every tier in the
same order, so fresh-temp numbering is directly comparable.

Run (default: LICM opt-in gate OFF, as in production):
    python3 compiler/loopopt/pipeline_crosscheck.py
Also exercise the LICM swap explicitly:
    APARA_LICM=1 python3 compiler/loopopt/pipeline_crosscheck.py
"""

import os
import sys
import glob

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_COMPILER)
sys.path.insert(0, _COMPILER)

import pycparser                                                    # noqa: E402
from compiler import preprocess, _FAKE_TYPEDEFS                     # noqa: E402
from ir import Temp                                                 # noqa: E402
from ir_gen import IRGenerator                                      # noqa: E402
from codegen import CodeGen                                         # noqa: E402

# passes that are UNCHANGED by M10 (shared by both runs)
from strength_reduce import strength_reduce                         # noqa: E402
from copyprop import copy_propagate                                 # noqa: E402
from coalesce import copy_coalesce                                  # noqa: E402
from dce import dead_code_eliminate                                 # noqa: E402
from sccp import sparse_conditional_constant_propagation           # noqa: E402
from gvn import global_value_numbering                             # noqa: E402
from mem2reg import mem2reg                                         # noqa: E402
from licm import hoist_loop_invariants                             # noqa: E402
from loop_reg import promote_loop_counters                         # noqa: E402

# the two swappable passes: LEGACY vs FRAMEWORK
import ivsr                                                         # noqa: E402
import licm2                                                        # noqa: E402
import mem2reg as _mem2reg_mod                                      # noqa: E402
import loop_reg as _loop_reg_mod                                    # noqa: E402
from loopopt.loop_ivsr import ivsr_module                           # noqa: E402
from loopopt.loop_licm import licm_module                          # noqa: E402

_GB = 0x400


def _reset_pass_counters():
    """Reset every pass module's global fresh-temp counter to its initial state.

    In production only ONE pipeline runs per compilation, so these counters start
    fresh. This harness, however, builds the legacy AND the framework tier sets
    back-to-back in one process, and passes with a module-global counter
    (ivsr._iv_n, mem2reg._m2r_n, loop_reg._lr_n) would otherwise carry over and
    make the SECOND run number its temps higher -- a harness artifact, not a
    pipeline difference (register-allocated code is name-insensitive and already
    matches). Resetting before each build models production exactly and makes the
    IR comparison name-for-name faithful."""
    ivsr._iv_n[0] = 0
    _mem2reg_mod._m2r_n[0] = 0
    _loop_reg_mod._lr_n = 0


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


def _build_tiers(ir0, ivsr_fn, licm_fn):
    """Reconstruct compiler.py's loop-opt tiers (order preserved EXACTLY), with
    `ivsr_fn` / `licm_fn` supplied for the two M10-swappable passes. Returns the
    list of (name, IR) tiers, all evaluated (no early break, so the fresh counter
    advances identically to a full comparison)."""
    def _sr(x):
        if os.environ.get('APARA_NO_STRENGTH_REDUCE'):
            return x
        return strength_reduce(x)[0]

    def _ivsr(x):
        return x if os.environ.get('APARA_NO_IVSR') else ivsr_fn(x)

    def _clean(x):
        return dead_code_eliminate(copy_coalesce(copy_propagate(x)))

    def _cp(x):
        x = _clean(x)
        x = dead_code_eliminate(sparse_conditional_constant_propagation(x))
        x = global_value_numbering(x)
        x = mem2reg(x)
        x = licm_fn(x)                       # the (opt-in) LICM in the cleanup stage
        x = _clean(x)
        return x

    _base = _sr(list(ir0))
    return [
        ("IVSR+LICM+loop-reg", _cp(promote_loop_counters(hoist_loop_invariants(_sr(_ivsr(list(ir0))))))),
        ("IVSR+loop-reg",      _cp(promote_loop_counters(_sr(_ivsr(list(ir0)))))),
        ("IVSR only",          _cp(_sr(_ivsr(list(ir0))))),
        ("LICM+loop-reg",      _cp(promote_loop_counters(hoist_loop_invariants(list(_base))))),
        ("LICM only",          _cp(hoist_loop_invariants(list(_base)))),
        ("loop-reg only",      _cp(promote_loop_counters(list(_base)))),
    ]


def _codegen(instrs):
    """(spilled, body) for one tier IR, or (True, None) on a codegen exception
    (register exhaustion etc.) -- matching how compiler.py treats a failed tier."""
    try:
        cg = CodeGen(global_base=_GB)
        body = cg.generate(instrs, global_base=_GB)
        return cg.spilled, body
    except Exception:
        return True, None


def _select(tiers):
    """Index of the first tier that codegens without spilling (compiler.py's tier
    selection), plus the per-tier (spilled, body). None if all spill/fail."""
    cg = [_codegen(ir) for _n, ir in tiers]
    sel = next((i for i, (sp, b) in enumerate(cg) if (not sp and b is not None)), None)
    return sel, cg


# framework pass wrappers that ALSO accumulate verifier-failure / rollback stats.
_ACC = {'vf': 0, 'rb': 0}


def _fw_ivsr(x):
    result, stats, _rep = ivsr_module(list(x))
    _ACC['vf'] += stats.verifier_failures
    _ACC['rb'] += stats.rollbacks
    return result


def _fw_licm(x):
    if not os.environ.get('APARA_LICM') or os.environ.get('APARA_NO_LICM'):
        return x
    work = list(x)
    stats, _rep = licm_module(work)
    _ACC['vf'] += stats.verifier_failures
    _ACC['rb'] += stats.rollbacks
    return work


def _legacy_licm(x):
    return licm2.loop_invariant_code_motion(x)


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    n_files = 0
    ir_match_files = 0
    code_match_files = 0
    sel_match_files = 0
    tiers_compared = 0
    ir_mismatch = []
    code_mismatch = []
    sel_mismatch = []

    for f in files:
        ir = _gen(f)
        if ir is None:
            continue
        n_files += 1

        _reset_pass_counters()
        legacy = _build_tiers(ir, ivsr.induction_strength_reduce, _legacy_licm)
        _reset_pass_counters()
        fw = _build_tiers(ir, _fw_ivsr, _fw_licm)

        # per-tier IR comparison
        ir_ok = True
        for (ln, lir), (fn, fir) in zip(legacy, fw):
            tiers_compared += 1
            if [repr(x) for x in lir] != [repr(x) for x in fir]:
                ir_ok = False
                if len(ir_mismatch) < 15:
                    ir_mismatch.append((os.path.relpath(f, _ROOT), ln))
        if ir_ok:
            ir_match_files += 1

        # per-tier generated-code + selected-tier comparison
        lsel, lcg = _select(legacy)
        fsel, fcg = _select(fw)
        code_ok = all((lsp == fsp) and (lb == fb) for (lsp, lb), (fsp, fb) in zip(lcg, fcg))
        if code_ok:
            code_match_files += 1
        elif len(code_mismatch) < 15:
            code_mismatch.append((os.path.relpath(f, _ROOT),))
        if lsel == fsel:
            sel_match_files += 1
        elif len(sel_mismatch) < 15:
            sel_mismatch.append((os.path.relpath(f, _ROOT), lsel, fsel))

    licm_state = 'ON (APARA_LICM=1)' if os.environ.get('APARA_LICM') else 'OFF (default)'
    print("=" * 72)
    print("  M10 PIPELINE CROSS-CHECK  --  legacy vs framework loop-opt pipeline")
    print(f"  (opt-in LICM gate: {licm_state})")
    print("=" * 72)
    print(f"  programs compared              : {n_files}")
    print(f"  tiers compared                 : {tiers_compared}")
    print(f"  MATCH per-tier IR (all tiers)  : {ir_match_files}")
    print(f"  MATCH per-tier generated code  : {code_match_files}")
    print(f"  MATCH selected tier            : {sel_match_files}")
    print(f"  IR MISMATCH files              : {len(ir_mismatch)}")
    print(f"  CODE MISMATCH files            : {len(code_mismatch)}")
    print(f"  SELECTED-TIER MISMATCH files   : {len(sel_mismatch)}")
    print(f"  framework verifier failures    : {_ACC['vf']}")
    print(f"  framework rollbacks            : {_ACC['rb']}")
    for fn, tn in ir_mismatch:
        print(f"    IR   mismatch: {fn}  tier={tn}")
    for (fn,) in code_mismatch:
        print(f"    CODE mismatch: {fn}")
    for fn, a, b in sel_mismatch:
        print(f"    SEL  mismatch: {fn}  legacy={a} fw={b}")
    ok = (not ir_mismatch and not code_mismatch and not sel_mismatch
          and _ACC['vf'] == 0 and _ACC['rb'] == 0
          and ir_match_files == n_files == code_match_files == sel_match_files)
    print("=" * 72)
    print("  RESULT:", "PASS" if ok else "FAIL")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())

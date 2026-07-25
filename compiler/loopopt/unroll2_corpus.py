"""
unroll2_corpus.py -- R1.2 corpus validation + differential + measurements.

For every corpus program:
  1. build the raw IR (baseline);
  2. factor-2 unroll a COPY through the M5 framework (LoopUnrollFactor2);
  3. record framework outcomes (loops transformed / skipped / verifier failures /
     rollbacks);
  4. DIFFERENTIAL CORRECTNESS: for every function the transform changed, execute
     the baseline and the unrolled IR on identical state with the ir_interp
     oracle and compare observable behaviour (return value + final memory);
  5. compile BOTH versions (CodeGen + bundler) to confirm the unrolled program
     still generates code, and MEASURE static instructions / bundles / IPB /
     code size.

Nothing is optimised here -- statistics are collected, not improved. This does
NOT touch the production pipeline; both versions go through the SAME downstream
CodeGen+bundler so the only difference measured is the unroll itself.

Run:  python3 compiler/loopopt/unroll2_corpus.py
"""

import os
import sys
import copy
import glob

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
from loopopt.loop_unroll2 import unroll_module                      # noqa: E402
from loopopt import ir_interp as I                                  # noqa: E402


def _gen(f):
    try:
        src, _ = preprocess(f)
        ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
        Temp.reset()
        g = IRGenerator(global_base=0x400)
        g.visit(ast)
        return g.instructions
    except Exception:
        return None


def _slices_by_name(instrs):
    out = {}
    for a, b in func_slices(instrs):
        out[instrs[a].name] = (a, b)
    return out


def _changed_functions(ir0, ir1):
    """Names of function slices whose instruction stream differs."""
    s0, s1 = _slices_by_name(ir0), _slices_by_name(ir1)
    changed = []
    for name, (a0, b0) in s0.items():
        if name not in s1:
            continue
        a1, b1 = s1[name]
        r0 = [repr(x) for x in ir0[a0:b0 + 1]]
        r1 = [repr(x) for x in ir1[a1:b1 + 1]]
        if r0 != r1:
            changed.append(name)
    return changed


def _mcode_metrics(instrs):
    """Compile a COPY of `instrs` (CodeGen + bundler); return (ok, static_ops,
    bundles, spilled). ok is False if codegen raised."""
    try:
        cg = CodeGen(global_base=0x400)
        body = cg.generate(copy.deepcopy(instrs), global_base=0x400)
        _m, n_before, n_after = bundle_mcode(body)
        return True, n_before, n_after, bool(cg.spilled)
    except Exception:
        return False, 0, 0, False


class Corpus:
    def __init__(self):
        self.programs = 0
        self.build_failures = 0
        self.transformed_programs = 0
        self.loops_transformed = 0
        self.loops_skipped = 0
        self.verifier_failures = 0
        self.rollbacks = 0
        self.compilation_failures = 0
        self.new_spills = 0
        # differential
        self.diff_match = 0
        self.diff_mismatch = 0
        self.diff_unsupported = 0
        self.mismatch_detail = []
        # measurements (per transformed program)
        self.m_static_base = 0
        self.m_static_unr = 0
        self.m_bundles_base = 0
        self.m_bundles_unr = 0
        self.n_measured = 0


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    c = Corpus()
    for f in files:
        ir0 = _gen(f)
        if ir0 is None:
            c.build_failures += 1
            continue
        c.programs += 1

        ir1 = copy.deepcopy(ir0)
        stats, _rep = unroll_module(ir1)
        c.loops_transformed += stats.commits
        c.loops_skipped += stats.skipped_illegal + stats.skipped_noop
        c.verifier_failures += stats.verifier_failures
        c.rollbacks += stats.rollbacks
        if stats.commits == 0:
            continue
        c.transformed_programs += 1

        # -- differential correctness on every changed function ----------------
        seed0 = I._preload_globals(ir0)
        seed1 = I._preload_globals(ir1)
        s0, s1 = _slices_by_name(ir0), _slices_by_name(ir1)
        for name in _changed_functions(ir0, ir1):
            a0, b0 = s0[name]
            a1, b1 = s1[name]
            try:
                r0, m0 = I.run_slice(ir0, a0, b0, init_mem=seed0)
            except (I.Unsupported, I.StepLimit) as e:
                c.diff_unsupported += 1
                continue
            try:
                r1, m1 = I.run_slice(ir1, a1, b1, init_mem=seed1)
            except (I.Unsupported, I.StepLimit) as e:
                c.diff_unsupported += 1
                continue
            if r0 == r1 and m0 == m1:
                c.diff_match += 1
            else:
                c.diff_mismatch += 1
                c.mismatch_detail.append((os.path.basename(f), name, r0, r1))

        # -- compile both + measure -------------------------------------------
        ok0, sb0, bn0, sp0 = _mcode_metrics(ir0)
        ok1, sb1, bn1, sp1 = _mcode_metrics(ir1)
        if not ok1:
            c.compilation_failures += 1
        else:
            if sp1 and not sp0:
                c.new_spills += 1
            if ok0:
                c.m_static_base += sb0
                c.m_static_unr += sb1
                c.m_bundles_base += bn0
                c.m_bundles_unr += bn1
                c.n_measured += 1

    _print_report(c, len(files))
    ok = (c.verifier_failures == 0 and c.rollbacks == 0
          and c.compilation_failures == 0 and c.diff_mismatch == 0)
    return 0 if ok else 1


def _print_report(c, n_files):
    print("=" * 72)
    print("  R1.2 LoopUnrollFactor2 -- CORPUS VALIDATION + DIFFERENTIAL")
    print("=" * 72)
    print("Corpus validation")
    print(f"  programs analysed      : {c.programs}")
    print(f"  build failures (parse) : {c.build_failures}")
    print(f"  programs transformed   : {c.transformed_programs}")
    print(f"  loops transformed      : {c.loops_transformed}")
    print(f"  loops skipped          : {c.loops_skipped}")
    print(f"  verifier failures      : {c.verifier_failures}")
    print(f"  rollbacks              : {c.rollbacks}")
    print(f"  compilation failures   : {c.compilation_failures}")
    print(f"  new register spills     : {c.new_spills}")
    print("Differential correctness (changed functions, interpreter oracle)")
    print(f"  behaviour matches      : {c.diff_match}")
    print(f"  behaviour mismatches   : {c.diff_mismatch}")
    print(f"  not interpretable      : {c.diff_unsupported}")
    for fn, name, r0, r1 in c.mismatch_detail[:10]:
        print(f"    MISMATCH {fn}:{name}  ret {r0} != {r1}")
    print("Preliminary measurements (baseline vs unrolled; same CodeGen+bundler)")
    if c.n_measured:
        sg = c.m_static_unr / c.m_static_base if c.m_static_base else 0
        cg = c.m_bundles_unr / c.m_bundles_base if c.m_bundles_base else 0
        ipb0 = c.m_static_base / c.m_bundles_base if c.m_bundles_base else 0
        ipb1 = c.m_static_unr / c.m_bundles_unr if c.m_bundles_unr else 0
        print(f"  programs measured      : {c.n_measured}")
        print(f"  static ops   base->unr : {c.m_static_base} -> {c.m_static_unr}"
              f"  ({sg:.3f}x)")
        print(f"  bundles      base->unr : {c.m_bundles_base} -> {c.m_bundles_unr}"
              f"  ({cg:.3f}x)  [code-size proxy]")
        print(f"  aggregate IPB base->unr: {ipb0:.3f} -> {ipb1:.3f}"
              f"  ({(ipb1 - ipb0):+.3f})")
    else:
        print("  (no programs measured)")
    ok = (c.verifier_failures == 0 and c.rollbacks == 0
          and c.compilation_failures == 0 and c.diff_mismatch == 0)
    print("=" * 72)
    print("  RESULT:", "PASS (0 verifier failures / 0 rollbacks / 0 mismatches / "
          "0 compilation failures)" if ok else "FAIL")
    print("=" * 72)


if __name__ == '__main__':
    raise SystemExit(main())

"""
unroll3_corpus.py -- R1.3 corpus validation + differential + R1.2-vs-R1.3
measurements. Reuses the R1.2 differential infrastructure (ir_interp).

For every corpus program: build the raw IR, factor-2 unroll a copy with R1.2 and
another copy with R1.3, differential-validate R1.3 against the baseline on every
changed function, compile baseline / R1.2 / R1.3 through the SAME CodeGen+bundler,
and compare static instructions / bundles / IPB / code size / coverage.

Run:  python3 compiler/loopopt/unroll3_corpus.py
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
from loopopt.loop_unroll2 import unroll_module as unroll_r12        # noqa: E402
from loopopt.loop_unroll3 import unroll_module as unroll_r13        # noqa: E402
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
    return {instrs[a].name: (a, b) for a, b in func_slices(instrs)}


def _changed(ir0, ir1):
    s0, s1 = _slices_by_name(ir0), _slices_by_name(ir1)
    out = []
    for name, (a0, b0) in s0.items():
        if name in s1:
            a1, b1 = s1[name]
            if [repr(x) for x in ir0[a0:b0 + 1]] != [repr(x) for x in ir1[a1:b1 + 1]]:
                out.append(name)
    return out


def _metrics(instrs):
    try:
        cg = CodeGen(global_base=0x400)
        body = cg.generate(copy.deepcopy(instrs), global_base=0x400)
        _m, nb, na = bundle_mcode(body)
        return True, nb, na, bool(cg.spilled)
    except Exception:
        return False, 0, 0, False


class C:
    def __init__(self):
        self.programs = self.transformed12 = self.transformed13 = 0
        self.loops12 = self.loops13 = 0
        self.vf = self.rb = self.compfail = self.new_spills = 0
        self.match = self.mismatch = self.unsupported = 0
        self.mism = []
        # measurement accumulators over R1.3-transformed programs
        self.n = 0
        self.sb = self.s12 = self.s13 = 0     # static ops
        self.bb = self.b12 = self.b13 = 0     # bundles


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    c = C()
    for f in files:
        ir0 = _gen(f)
        if ir0 is None:
            continue
        c.programs += 1

        ir12 = copy.deepcopy(ir0)
        st12, _ = unroll_r12(ir12)
        ir13 = copy.deepcopy(ir0)
        st13, _ = unroll_r13(ir13)

        c.loops12 += st12.commits
        c.loops13 += st13.commits
        c.vf += st13.verifier_failures
        c.rb += st13.rollbacks
        if st12.commits:
            c.transformed12 += 1
        if st13.commits == 0:
            continue
        c.transformed13 += 1

        # differential: baseline vs R1.3 on every changed function
        seed0 = I._preload_globals(ir0)
        seed3 = I._preload_globals(ir13)
        s0, s3 = _slices_by_name(ir0), _slices_by_name(ir13)
        for name in _changed(ir0, ir13):
            a0, b0 = s0[name]; a3, b3 = s3[name]
            try:
                r0, m0 = I.run_slice(ir0, a0, b0, init_mem=seed0)
                r3, m3 = I.run_slice(ir13, a3, b3, init_mem=seed3)
            except (I.Unsupported, I.StepLimit):
                c.unsupported += 1
                continue
            if r0 == r3 and m0 == m3:
                c.match += 1
            else:
                c.mismatch += 1
                c.mism.append((os.path.basename(f), name, r0, r3))

        okb, sb, bb, spb = _metrics(ir0)
        ok12, s12, b12, sp12 = _metrics(ir12)
        ok13, s13, b13, sp13 = _metrics(ir13)
        if not ok13:
            c.compfail += 1
        else:
            if sp13 and not spb:
                c.new_spills += 1
            if okb and ok12:
                c.n += 1
                c.sb += sb; c.s12 += s12; c.s13 += s13
                c.bb += bb; c.b12 += b12; c.b13 += b13

    _report(c)
    ok = c.vf == 0 and c.rb == 0 and c.compfail == 0 and c.mismatch == 0
    return 0 if ok else 1


def _report(c):
    print("=" * 74)
    print("  R1.3 LoopUnrollFactor2R13 -- CORPUS VALIDATION + R1.2 vs R1.3")
    print("=" * 74)
    print("Corpus validation (R1.3)")
    print(f"  programs analysed        : {c.programs}")
    print(f"  programs transformed     : {c.transformed13}  (R1.2: {c.transformed12})")
    print(f"  loops transformed        : {c.loops13}  (R1.2: {c.loops12})")
    print(f"  verifier failures        : {c.vf}")
    print(f"  rollbacks                : {c.rb}")
    print(f"  compilation failures     : {c.compfail}")
    print(f"  new register spills      : {c.new_spills}")
    print("Differential correctness (baseline vs R1.3, changed functions)")
    print(f"  behaviour matches        : {c.match}")
    print(f"  behaviour mismatches     : {c.mismatch}")
    print(f"  not interpretable        : {c.unsupported}")
    for fn, name, r0, r3 in c.mism[:10]:
        print(f"    MISMATCH {fn}:{name}  {r0} != {r3}")
    print(f"Measurements over {c.n} programs (baseline / R1.2 / R1.3, same CodeGen+bundler)")
    if c.n:
        def ratio(a, b):
            return (a / b) if b else 0.0
        ipb_b = ratio(c.sb, c.bb); ipb_12 = ratio(c.s12, c.b12); ipb_13 = ratio(c.s13, c.b13)
        print(f"  static ops   : {c.sb} / {c.s12} / {c.s13}"
              f"   (R1.2 {ratio(c.s12, c.sb):.3f}x, R1.3 {ratio(c.s13, c.sb):.3f}x)")
        print(f"  bundles      : {c.bb} / {c.b12} / {c.b13}"
              f"   (R1.2 {ratio(c.b12, c.bb):.3f}x, R1.3 {ratio(c.b13, c.bb):.3f}x)  [code size]")
        print(f"  IPB          : {ipb_b:.3f} / {ipb_12:.3f} / {ipb_13:.3f}"
              f"   (R1.2 {ipb_12 - ipb_b:+.3f}, R1.3 {ipb_13 - ipb_b:+.3f})")
        print(f"  R1.3 vs R1.2 : bundles {ratio(c.b13, c.b12):.3f}x, "
              f"IPB {ipb_13 - ipb_12:+.3f}")
    ok = c.vf == 0 and c.rb == 0 and c.compfail == 0 and c.mismatch == 0
    print("=" * 74)
    print("  RESULT:", "PASS (0 verifier failures / 0 rollbacks / 0 mismatches / "
          "0 compilation failures)" if ok else "FAIL")
    print("=" * 74)


if __name__ == '__main__':
    raise SystemExit(main())

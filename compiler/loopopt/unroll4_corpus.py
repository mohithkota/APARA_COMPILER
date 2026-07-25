"""
unroll4_corpus.py -- R1.4 corpus validation + differential + baseline/R1.2/R1.3/
R1.4 measurement comparison. Reuses the R1.2 differential oracle (ir_interp).

Per program: build raw IR; unroll copies with R1.2, R1.3, R1.4; differential-
validate R1.4 vs baseline on every changed function; compile all four through the
SAME CodeGen+bundler; compare static ops / bundles / IPB / coverage and report
factor usage.

Run:  python3 compiler/loopopt/unroll4_corpus.py
"""

import os
import sys
import copy
import glob
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
from loopopt.loop_unroll2 import unroll_module as u2                # noqa: E402
from loopopt.loop_unroll3 import unroll_module as u3                # noqa: E402
from loopopt.loop_unroll4 import unroll_module as u4                # noqa: E402
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


def _by_name(instrs):
    return {instrs[a].name: (a, b) for a, b in func_slices(instrs)}


def _changed(ir0, ir1):
    s0, s1 = _by_name(ir0), _by_name(ir1)
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
        self.programs = 0
        self.t2 = self.t3 = self.t4 = 0          # programs transformed
        self.l2 = self.l3 = self.l4 = 0          # loops transformed
        self.vf = self.rb = self.compfail = self.spills = 0
        self.match = self.mismatch = self.unsup = 0
        self.mism = []
        self.factors = Counter()
        self.n = 0
        self.sb = self.s2 = self.s3 = self.s4 = 0
        self.bb = self.b2 = self.b3 = self.b4 = 0


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
        ir2 = copy.deepcopy(ir0); s2, _ = u2(ir2)
        ir3 = copy.deepcopy(ir0); s3, _ = u3(ir3)
        ir4 = copy.deepcopy(ir0); s4, r4 = u4(ir4)
        c.l2 += s2.commits; c.l3 += s3.commits; c.l4 += s4.commits
        c.vf += s4.verifier_failures; c.rb += s4.rollbacks
        for fac, k in r4.factors.items():
            c.factors[fac] += k
        if s2.commits:
            c.t2 += 1
        if s3.commits:
            c.t3 += 1
        if s4.commits == 0:
            continue
        c.t4 += 1

        seed0 = I._preload_globals(ir0); seed4 = I._preload_globals(ir4)
        s0m, s4m = _by_name(ir0), _by_name(ir4)
        for name in _changed(ir0, ir4):
            a0, b0 = s0m[name]; a4, b4 = s4m[name]
            try:
                r0, m0 = I.run_slice(ir0, a0, b0, init_mem=seed0)
                rr, mm = I.run_slice(ir4, a4, b4, init_mem=seed4)
            except (I.Unsupported, I.StepLimit):
                c.unsup += 1
                continue
            if r0 == rr and m0 == mm:
                c.match += 1
            else:
                c.mismatch += 1
                c.mism.append((os.path.basename(f), name, r0, rr))

        okb, sb, bb, spb = _metrics(ir0)
        ok2, s2v, b2v, _ = _metrics(ir2)
        ok3, s3v, b3v, _ = _metrics(ir3)
        ok4, s4v, b4v, sp4 = _metrics(ir4)
        if not ok4:
            c.compfail += 1
        else:
            if sp4 and not spb:
                c.spills += 1
            if okb and ok2 and ok3:
                c.n += 1
                c.sb += sb; c.s2 += s2v; c.s3 += s3v; c.s4 += s4v
                c.bb += bb; c.b2 += b2v; c.b3 += b3v; c.b4 += b4v

    _report(c)
    ok = c.vf == 0 and c.rb == 0 and c.compfail == 0 and c.mismatch == 0
    return 0 if ok else 1


def _report(c):
    def r(a, b):
        return (a / b) if b else 0.0
    print("=" * 76)
    print("  R1.4 LoopUnrollFactorN -- CORPUS VALIDATION + baseline/R1.2/R1.3/R1.4")
    print("=" * 76)
    print("Corpus validation (R1.4)")
    print(f"  programs analysed        : {c.programs}")
    print(f"  programs transformed     : {c.t4}  (R1.3: {c.t3}, R1.2: {c.t2})")
    print(f"  loops transformed        : {c.l4}  (R1.3: {c.l3}, R1.2: {c.l2})")
    print(f"  factor-2 / -4 / -8 usage : {c.factors.get(2,0)} / "
          f"{c.factors.get(4,0)} / {c.factors.get(8,0)}")
    print(f"  verifier failures        : {c.vf}")
    print(f"  rollbacks                : {c.rb}")
    print(f"  compilation failures     : {c.compfail}")
    print(f"  new register spills      : {c.spills}")
    print("Differential correctness (baseline vs R1.4, changed functions)")
    print(f"  behaviour matches        : {c.match}")
    print(f"  behaviour mismatches     : {c.mismatch}")
    print(f"  not interpretable        : {c.unsup}")
    for fn, nm, a, b in c.mism[:10]:
        print(f"    MISMATCH {fn}:{nm}  {a} != {b}")
    print(f"Measurements over {c.n} programs (baseline / R1.2 / R1.3 / R1.4)")
    if c.n:
        ib = r(c.sb, c.bb); i2 = r(c.s2, c.b2); i3 = r(c.s3, c.b3); i4 = r(c.s4, c.b4)
        print(f"  static ops : {c.sb} / {c.s2} / {c.s3} / {c.s4}"
              f"   (R1.4 {r(c.s4, c.sb):.3f}x base)")
        print(f"  bundles    : {c.bb} / {c.b2} / {c.b3} / {c.b4}"
              f"   (R1.4 {r(c.b4, c.bb):.3f}x base, {r(c.b4, c.b3):.3f}x R1.3)")
        print(f"  IPB        : {ib:.3f} / {i2:.3f} / {i3:.3f} / {i4:.3f}"
              f"   (R1.4 {i4 - ib:+.3f} vs base, {i4 - i3:+.3f} vs R1.3)")
    ok = c.vf == 0 and c.rb == 0 and c.compfail == 0 and c.mismatch == 0
    print("=" * 76)
    print("  RESULT:", "PASS (0 verifier failures / 0 rollbacks / 0 mismatches / "
          "0 compilation failures)" if ok else "FAIL")
    print("=" * 76)


if __name__ == '__main__':
    raise SystemExit(main())

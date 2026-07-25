"""
schedule_corpus.py -- R2.3 corpus evaluation + R2.2-vs-R2.3 measurement.

Runs the dependence-aware IR scheduler over the full corpus and reports the
required correctness ledger (programs / blocks / reordered / verifier / rollbacks
/ compile failures / behaviour mismatches), then compiles both the ORIGINAL (=
R2.2 baseline: R2.2 changed no IR) and the SCHEDULED IR through the SAME
production CodeGen + bundler and compares.

Because the bundler ALREADY list-schedules at the assembly level (schedule=True),
measurement is reported in TWO modes so the IR scheduler's effect is visible:

  * bundler scheduler ON  (production default) -- what ships; the bundler
    re-derives an order, so IR order mostly only affects register allocation.
  * bundler scheduler OFF (schedule=False)     -- the bundler packs the order it
    is GIVEN, so this isolates the IR scheduler's own effect on bundle density.

Metrics: static instruction count, bundle count, IPB (instructions/bundle),
register spills, plus the structural context (average dependency height =
critical path, average schedule length = block size).

Run:  python3 compiler/loopopt/schedule_corpus.py
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
from loopopt.schedule import schedule_module, schedule_function_order  # noqa: E402
from loopopt.depgraph import DependenceGraph                        # noqa: E402
from loopopt.depgraph_disambig import (MemoryDisambiguator,          # noqa: E402
                                       _function_descs)
from loopopt import ir_interp                                       # noqa: E402

_GB = 0x400
_LEADER = {'IRLabel', 'IRFuncBegin'}
_TRAILER = {'IRJump', 'IRCondJump', 'IRReturn', 'IRHalt', 'IRFuncEnd'}


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
    """(ok, static_ops, bundles_sched_on, bundles_sched_off, spilled)."""
    try:
        cg = CodeGen(global_base=_GB)
        body = cg.generate(copy.deepcopy(instrs), global_base=_GB)
        _m, n_on, b_on = bundle_mcode(body, schedule=True)
        _m2, n_off, b_off = bundle_mcode(body, schedule=False)
        return True, n_on, b_on, b_off, bool(cg.spilled)
    except Exception:
        return False, 0, 0, 0, False


def _structural_context(instrs):
    """Average dependency height (critical path) and schedule length (block size)
    over all schedulable blocks, from the R2.2 graph. Scheduler-invariant."""
    heights, lengths, nblocks = 0, 0, 0
    for (lo, hi) in func_slices(instrs):
        descs = _function_descs(instrs).get((lo, hi), [])
        disamb = MemoryDisambiguator(instrs, lo, hi, descs)
        g = DependenceGraph(instrs, lo, hi, disambiguator=disamb)
        for b in g.cfg.blocks:
            idxs = list(range(b.lo, b.hi + 1))
            f, t = 0, len(idxs)
            while f < t and type(instrs[idxs[f]]).__name__ in _LEADER:
                f += 1
            while t > f and type(instrs[idxs[t - 1]]).__name__ in _TRAILER:
                t -= 1
            sched = set(idxs[f:t])
            if len(sched) <= 1:
                continue
            succ = {n: set() for n in sched}
            for e in g.edges:
                if (not e.carried and e.src in sched and e.dst in sched):
                    succ[e.src].add(e.dst)
            hh = {}
            for n in sorted(sched, reverse=True):
                hh[n] = max((hh[s] + 1 for s in succ[n]), default=0)
            heights += (max(hh.values()) + 1)          # critical path in "steps"
            lengths += len(sched)
            nblocks += 1
    return heights, lengths, nblocks


class Stats:
    def __init__(self):
        self.programs = 0
        self.functions = self.functions_changed = 0
        self.blocks = self.blocks_reordered = self.instrs_reordered = 0
        self.verified = self.unverified = self.rollbacks = self.structural = 0
        self.compile_fail = 0
        self.mism = self.match = self.unsup = 0
        self.mism_list = []
        # measurement (only over programs where BOTH compile)
        self.n = 0
        self.s_base = self.s_sched = 0                  # static ops
        self.bon_base = self.bon_sched = 0              # bundles, bundler-sched ON
        self.boff_base = self.boff_sched = 0            # bundles, bundler-sched OFF
        self.sp_base = self.sp_sched = 0                # spills
        self.height = self.length = self.cblocks = 0    # structural context


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    st = Stats()
    for f in files:
        ir0 = _gen(f)
        if ir0 is None:
            continue
        st.programs += 1
        irs, sst = schedule_module(ir0)

        st.functions += sst.functions
        st.functions_changed += sst.functions_changed
        st.blocks += sst.blocks
        st.blocks_reordered += sst.blocks_reordered
        st.instrs_reordered += sst.instrs_reordered
        st.verified += sst.verified
        st.unverified += sst.unverified
        st.rollbacks += sst.rollbacks
        st.structural += sst.structural_failures

        # behaviour check per function (original vs scheduled)
        for (lo, hi) in func_slices(ir0):
            v, d = ir_interp.differential(ir0, irs, lo, hi)
            if v == 'match':
                st.match += 1
            elif v == 'mismatch':
                st.mism += 1
                st.mism_list.append((os.path.relpath(f, _ROOT), ir0[lo].name, d))
            else:
                st.unsup += 1

        okb, sb, bonb, boffb, spb = _metrics(ir0)
        oks, ss, bons, boffs, sps = _metrics(irs)
        if not oks:
            st.compile_fail += 1
        if okb and oks:
            st.n += 1
            st.s_base += sb; st.s_sched += ss
            st.bon_base += bonb; st.bon_sched += bons
            st.boff_base += boffb; st.boff_sched += boffs
            st.sp_base += spb; st.sp_sched += sps
            h, ln, cb = _structural_context(ir0)
            st.height += h; st.length += ln; st.cblocks += cb

    _report(st)
    ok = (st.mism == 0 and st.rollbacks == 0 and st.structural == 0
          and st.compile_fail == 0)
    return 0 if ok else 1


def _report(st):
    def ipb(s, b):
        return (s / b) if b else 0.0
    print("=" * 78)
    print("  R2.3 DEPENDENCE-AWARE IR SCHEDULER -- CORPUS EVALUATION + R2.2 vs R2.3")
    print("=" * 78)
    print("Correctness ledger")
    print(f"  programs analysed          : {st.programs}")
    print(f"  functions / changed        : {st.functions} / {st.functions_changed}")
    print(f"  basic blocks scheduled     : {st.blocks}")
    print(f"  blocks reordered           : {st.blocks_reordered}")
    print(f"  instructions reordered     : {st.instrs_reordered}")
    print(f"  verifier failures (struct) : {st.structural}")
    print(f"  rollbacks (differential)   : {st.rollbacks}")
    print(f"  compilation failures       : {st.compile_fail}")
    print(f"  behaviour mismatches       : {st.mism}")
    print(f"  differential verified      : {st.match}  "
          f"(unsupported/legal-by-constr: {st.unsup})")
    for fn, name, d in st.mism_list[:10]:
        print(f"    MISMATCH {fn}:{name}  {d}")
    print(f"Measurements over {st.n} programs (R2.2 baseline vs R2.3 scheduled)")
    if st.n:
        print(f"  static instructions        : {st.s_base} -> {st.s_sched}")
        print(f"  register spills            : {st.sp_base} -> {st.sp_sched}")
        print("  -- bundler scheduler ON  (production default) --")
        print(f"     bundles                 : {st.bon_base} -> {st.bon_sched}"
              f"   ({st.bon_sched - st.bon_base:+d})")
        print(f"     IPB                     : {ipb(st.s_base, st.bon_base):.3f}"
              f" -> {ipb(st.s_sched, st.bon_sched):.3f}")
        print("  -- bundler scheduler OFF (isolates IR scheduler) --")
        print(f"     bundles                 : {st.boff_base} -> {st.boff_sched}"
              f"   ({st.boff_sched - st.boff_base:+d})")
        print(f"     IPB                     : {ipb(st.s_base, st.boff_base):.3f}"
              f" -> {ipb(st.s_sched, st.boff_sched):.3f}")
        print("  -- structural context (scheduler-invariant) --")
        print(f"     avg dependency height   : {ipb(st.height, st.cblocks):.2f}"
              f"  (critical path, steps)")
        print(f"     avg schedule length     : {ipb(st.length, st.cblocks):.2f}"
              f"  (schedulable instrs/block)")
    ok = (st.mism == 0 and st.rollbacks == 0 and st.structural == 0
          and st.compile_fail == 0)
    print("=" * 78)
    print("  RESULT:", "PASS (0 mismatches / 0 rollbacks / 0 verifier failures / "
          "0 compile failures)" if ok else "FAIL")
    print("=" * 78)


if __name__ == '__main__':
    raise SystemExit(main())

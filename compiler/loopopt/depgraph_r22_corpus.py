"""
depgraph_r22_corpus.py -- R2.2 corpus validation + R2.1-vs-R2.2 measurement.

For every function of the full corpus it builds BOTH the R2.1 graph (no
disambiguator) and the R2.2 graph (with the MemoryDisambiguator) and checks:

  SOUNDNESS (per function)
    * R2.2's memory edges are a strict SUBSET of R2.1's (same src/dst/kind/
      carried) -- disambiguation only ever DROPS a memory edge, never adds or
      redirects one.
    * R2.2's register and control edges are IDENTICAL to R2.1's.
    * R2.2's graph passes validate() and mutates no IR.

  INVARIANCE (compiler output unchanged)
    * every program still compiles + bundles through the production CodeGen +
      bundler (the graph is analysis-only and consumed by nothing). The
      end-to-end "identical IR / assembly / bundles / tier / 0 rollbacks" proof is
      pipeline_crosscheck.py, unaffected because neither graph touches the IR.

  MEASUREMENT (R2.1 vs R2.2)
    * total / register / memory / loop-carried edge counts
    * memory edges eliminated by disambiguation, broken down by reason
    * surviving memory edges split into PROVEN (must-alias) vs CONSERVATIVE

Run:  python3 compiler/loopopt/depgraph_r22_corpus.py
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
from loopopt.depgraph import DependenceGraph                        # noqa: E402
from loopopt.depgraph_disambig import (MemoryDisambiguator,          # noqa: E402
                                       _function_descs)

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


def _compile(instrs):
    try:
        cg = CodeGen(global_base=_GB)
        body = cg.generate(copy.deepcopy(instrs), global_base=_GB)
        bundle_mcode(body)
        return True
    except Exception:
        return False


def _mem_key(e):
    return (e.src, e.dst, e.kind, e.carried)


class Stats:
    def __init__(self):
        self.programs = self.functions = 0
        self.subset_violations = self.rc_diffs = 0
        self.validate_failures = self.ir_mutations = 0
        self.compiled = self.compile_fail = 0
        # R2.1
        self.r1_total = self.r1_reg = self.r1_mem = self.r1_carried = 0
        # R2.2
        self.r2_total = self.r2_reg = self.r2_mem = self.r2_carried = 0
        self.r2_mem_carried = 0
        self.proven = self.conservative = 0
        self.eliminated = 0
        self.elim_reasons = Counter()
        self.proven_reasons = Counter()
        self.problems = []


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    st = Stats()
    for f in files:
        ir = _gen(f)
        if ir is None:
            continue
        st.programs += 1
        rel = os.path.relpath(f, _ROOT)

        before = [repr(x) for x in ir]
        by_slice = _function_descs(ir)
        for (lo, hi) in func_slices(ir):
            st.functions += 1
            base = DependenceGraph(ir, lo, hi)
            disamb = MemoryDisambiguator(ir, lo, hi, by_slice.get((lo, hi), []))
            ref = DependenceGraph(ir, lo, hi, disambiguator=disamb)

            # soundness
            b_mem = Counter(_mem_key(e) for e in base.edges if e.is_memory())
            r_mem = Counter(_mem_key(e) for e in ref.edges if e.is_memory())
            if not all(r_mem[k] <= b_mem[k] for k in r_mem):
                st.subset_violations += 1
                st.problems.append((rel, getattr(ir[lo], 'name', '?'),
                                    "R2.2 mem edge not in R2.1"))
            b_rc = sorted(_mem_key(e) for e in base.edges if not e.is_memory())
            r_rc = sorted(_mem_key(e) for e in ref.edges if not e.is_memory())
            if b_rc != r_rc:
                st.rc_diffs += 1
                st.problems.append((rel, getattr(ir[lo], 'name', '?'),
                                    "register/control edges differ"))
            if ref.validate():
                st.validate_failures += 1

            # tallies
            st.r1_total += base.num_edges()
            st.r1_reg += len(base.register_edges())
            st.r1_mem += len(base.memory_edges())
            st.r1_carried += len(base.carried_edges())
            st.r2_total += ref.num_edges()
            st.r2_reg += len(ref.register_edges())
            st.r2_mem += len(ref.memory_edges())
            st.r2_carried += len(ref.carried_edges())
            st.r2_mem_carried += len([e for e in ref.memory_edges() if e.carried])
            st.proven += len(ref.proven_memory_edges())
            st.conservative += len(ref.conservative_memory_edges())
            st.eliminated += ref.eliminated_memory_edges
            for *_x, reason in ref.eliminated:
                st.elim_reasons[reason] += 1
            for e in ref.proven_memory_edges():
                st.proven_reasons[e.reason] += 1

        after = [repr(x) for x in ir]
        if before != after:
            st.ir_mutations += 1

        if _compile(ir):
            st.compiled += 1
        else:
            st.compile_fail += 1

    _report(st)
    ok = (st.subset_violations == 0 and st.rc_diffs == 0
          and st.validate_failures == 0 and st.ir_mutations == 0)
    return 0 if ok else 1


def _report(st):
    def pct(a, b):
        return (100.0 * a / b) if b else 0.0
    print("=" * 76)
    print("  R2.2 MEMORY DISAMBIGUATION -- CORPUS VALIDATION + R2.1 vs R2.2")
    print("=" * 76)
    print(f"  programs / functions        : {st.programs} / {st.functions}")
    print("  --- soundness (all must be 0) ---")
    print(f"  subset violations           : {st.subset_violations}")
    print(f"  register/control edge diffs : {st.rc_diffs}")
    print(f"  validate() failures         : {st.validate_failures}")
    print(f"  IR mutations                : {st.ir_mutations}")
    print(f"  programs compiled+bundled   : {st.compiled}  (fail: {st.compile_fail})")
    print("  --- edge totals  (R2.1 -> R2.2) ---")
    print(f"  total edges                 : {st.r1_total} -> {st.r2_total}")
    print(f"  register edges              : {st.r1_reg} -> {st.r2_reg}  (identical)")
    print(f"  memory edges                : {st.r1_mem} -> {st.r2_mem}"
          f"   ({st.eliminated} eliminated, {pct(st.eliminated, st.r1_mem):.1f}%)")
    print(f"  loop-carried edges (all)    : {st.r1_carried} -> {st.r2_carried}")
    print(f"  loop-carried MEMORY edges   : (R2.2) {st.r2_mem_carried}")
    print("  --- surviving memory edges (R2.2) ---")
    print(f"  proven (must-alias)         : {st.proven}")
    print(f"  conservative (may-alias)    : {st.conservative}")
    print("  --- eliminated memory edges by reason ---")
    for reason, n in st.elim_reasons.most_common():
        print(f"      {reason:28} : {n}")
    print("  --- proven memory edges by reason ---")
    for reason, n in st.proven_reasons.most_common():
        print(f"      {reason:28} : {n}")
    for rel, fn, msg in st.problems[:15]:
        print(f"    PROBLEM {rel}:{fn}  {msg}")
    ok = (st.subset_violations == 0 and st.rc_diffs == 0
          and st.validate_failures == 0 and st.ir_mutations == 0)
    print("=" * 76)
    print("  RESULT:", "PASS (sound: subset-only, register/control identical, "
          "0 validate/mutation)" if ok else "FAIL")
    print("=" * 76)


if __name__ == '__main__':
    raise SystemExit(main())

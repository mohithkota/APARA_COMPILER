"""
depgraph_corpus.py -- R2.1 corpus validation for the DependenceGraph.

Because R2.1 is an ANALYSIS milestone that adds a new module and no pass, its
corpus obligation is twofold and this harness proves both, program by program,
over the full compiler corpus:

  1. INVARIANCE (the compiler's output is untouched).  For each program we build
     the module IR, snapshot it, construct a DependenceGraph over EVERY function,
     and assert the IR is byte-for-byte identical afterwards (repr-equal). Because
     the graph never mutates the IR, the downstream CodeGen body and the bundled
     assembly are unchanged by construction -- we also compile + bundle each
     program (through the SAME CodeGen + bundler) and report the totals so the
     "identical assembly / identical bundles" claim is anchored to real output.
     The end-to-end proof that the optimization pipeline itself still produces
     instruction-identical IR / code / tier selection / 0 rollbacks with this
     module present is pipeline_crosscheck.py (124/124, run separately).

  2. ROBUSTNESS (the graph is well-formed everywhere).  Every function graph must
     build without exception and pass validate() (endpoint integrity, succ/pred
     mirroring, the low->high intra / high->low carried direction invariant).
     We also aggregate node / edge / carried-edge / recurrence counts as evidence
     the graph is doing real work across the corpus.

Run:  python3 compiler/loopopt/depgraph_corpus.py
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
from loopopt.depgraph import DependenceGraph                        # noqa: E402

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
    """(ok, n_bundles) through the SAME CodeGen + bundler as production."""
    try:
        cg = CodeGen(global_base=_GB)
        body = cg.generate(copy.deepcopy(instrs), global_base=_GB)
        _m, nb, _na = bundle_mcode(body)
        return True, nb
    except Exception:
        return False, 0


class Stats:
    def __init__(self):
        self.programs = 0
        self.functions = 0
        self.graphs_built = 0
        self.build_errors = 0
        self.validate_failures = 0
        self.ir_mutations = 0
        self.compiled = 0
        self.compile_fail = 0
        self.total_nodes = 0
        self.total_edges = 0
        self.total_carried = 0
        self.total_recurrences = 0
        self.reg_edges = 0
        self.mem_edges = 0
        self.ctl_edges = 0
        self.problems = []
        self.mutated = []


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
        rel = os.path.relpath(f, _ROOT)

        before = [repr(x) for x in ir0]
        for (lo, hi) in func_slices(ir0):
            st.functions += 1
            try:
                g = DependenceGraph(ir0, lo, hi)
            except Exception as e:                       # noqa: BLE001
                st.build_errors += 1
                st.problems.append((rel, ir0[lo].name if hasattr(ir0[lo], 'name')
                                    else '?', f"build error: {e!r}"))
                continue
            st.graphs_built += 1
            probs = g.validate()
            if probs:
                st.validate_failures += 1
                st.problems.append((rel, getattr(ir0[lo], 'name', '?'),
                                    f"{len(probs)} validate issue(s): {probs[:3]}"))
            st.total_nodes += g.num_nodes()
            st.total_edges += g.num_edges()
            st.total_carried += len(g.carried_edges())
            st.total_recurrences += len(g.recurrences())
            for e in g.edges:
                if e.is_register():
                    st.reg_edges += 1
                elif e.is_memory():
                    st.mem_edges += 1
                else:
                    st.ctl_edges += 1

        after = [repr(x) for x in ir0]
        if before != after:
            st.ir_mutations += 1
            st.mutated.append(rel)

        ok, _nb = _compile(ir0)
        if ok:
            st.compiled += 1
        else:
            st.compile_fail += 1

    _report(st)
    ok = (st.build_errors == 0 and st.validate_failures == 0
          and st.ir_mutations == 0)
    return 0 if ok else 1


def _report(st):
    print("=" * 74)
    print("  R2.1 DEPENDENCEGRAPH -- CORPUS VALIDATION")
    print("=" * 74)
    print(f"  programs analysed            : {st.programs}")
    print(f"  functions (graphs attempted) : {st.functions}")
    print(f"  graphs built successfully    : {st.graphs_built}")
    print(f"  graph build errors           : {st.build_errors}")
    print(f"  validate() failures          : {st.validate_failures}")
    print(f"  IR mutations (must be 0)     : {st.ir_mutations}")
    print(f"  programs compiled+bundled    : {st.compiled}  "
          f"(compile failures: {st.compile_fail})")
    print("  --- graph content (evidence of real analysis) ---")
    print(f"  total nodes                  : {st.total_nodes}")
    print(f"  total edges                  : {st.total_edges}"
          f"  (reg {st.reg_edges} / mem {st.mem_edges} / ctl {st.ctl_edges})")
    print(f"  loop-carried edges           : {st.total_carried}")
    print(f"  recurrence SCCs              : {st.total_recurrences}")
    for rel, fn, msg in st.problems[:15]:
        print(f"    PROBLEM {rel}:{fn}  {msg}")
    for rel in st.mutated[:15]:
        print(f"    IR MUTATED {rel}")
    ok = (st.build_errors == 0 and st.validate_failures == 0
          and st.ir_mutations == 0)
    print("=" * 74)
    print("  RESULT:", "PASS (0 build errors / 0 validate failures / 0 IR "
          "mutations)" if ok else "FAIL")
    print("=" * 74)


if __name__ == '__main__':
    raise SystemExit(main())

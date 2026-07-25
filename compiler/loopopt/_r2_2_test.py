"""
_r2_2_test.py -- unit tests for R2.2 Memory Dependence Disambiguation.

Each test compiles a small C fixture, builds the R2.1 graph (no disambiguator)
and the R2.2 graph (with MemoryDisambiguator), and asserts the memory edges are
refined correctly AND soundly:

  * affine self-index accesses      (a[i] vs a[i]: intra kept, carried removed)
  * constant-offset accesses        (a[0] vs a[1]: disjoint)
  * induction-variable + clean slot  (sumn: clean-slot-vs-computed removed; IV
                                       recurrences preserved and marked proven)
  * disjoint stack slots             (two local arrays: distinct-local-objects)
  * aliasing pointers                (p[i] vs q[i]: conservatively KEPT)
  * conservative fallbacks           (unknown base / different scale: kept)
  * loop-carried accesses            (global accumulator: real carried dep kept,
                                       marked proven)
  * repeated identical accesses      (same address: proven MUST-alias)
  * regression compatibility         (R2.2 memory edges are a strict SUBSET of
                                       R2.1's; register/control edges identical;
                                       validate clean; no IR mutation)

SOUNDNESS is checked structurally in every test: R2.2 only ever DROPS memory
edges relative to R2.1 -- it never adds or redirects one.

Run:  python3 compiler/loopopt/_r2_2_test.py
"""

import os
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pycparser                                                       # noqa: E402
from compiler import _FAKE_TYPEDEFS                                    # noqa: E402
from ir import Temp                                                    # noqa: E402
from ir_gen import IRGenerator                                         # noqa: E402
from ir_utils import func_slices                                       # noqa: E402
from loopopt.depgraph import DependenceGraph                           # noqa: E402
from loopopt.depgraph_disambig import disambiguate_function            # noqa: E402

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _compile(code):
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=0x400)
    g.visit(ast)
    return g.instructions


def _graphs(code, fn):
    """(R2.1 graph, R2.2 graph) for function `fn` of `code`."""
    ir = _compile(code)
    for lo, hi in func_slices(ir):
        if ir[lo].name == fn:
            return DependenceGraph(ir, lo, hi), disambiguate_function(ir, lo, hi)
    raise AssertionError(f"function {fn} not found")


def _mem_key(e):
    return (e.src, e.dst, e.kind, e.carried)


def _elim_reasons(ref):
    from collections import Counter
    return Counter(r for *_, r in ref.eliminated)


def _soundness(base, ref):
    """R2.2 must ONLY drop memory edges: its memory edges are a subset of R2.1's,
    and its register/control edges are identical."""
    b_mem = {_mem_key(e) for e in base.edges if e.is_memory()}
    r_mem = {_mem_key(e) for e in ref.edges if e.is_memory()}
    subset = r_mem <= b_mem
    b_rc = sorted(_mem_key(e) for e in base.edges if not e.is_memory())
    r_rc = sorted(_mem_key(e) for e in ref.edges if not e.is_memory())
    return subset, (b_rc == r_rc), (len(b_mem) - len(r_mem))


# ── fixtures ───────────────────────────────────────────────────────────────────

SUMN = "int sumn(int*p,int n){int s=0,i; for(i=0;i<n;i++) s+=p[i]; return s;}"
SELF = "void self(int*a,int n){int i; for(i=0;i<n;i++){ a[i]=a[i]+1; }}"
TWOARR = ("int twoarr(int n){int a[8],b[8],i,s; s=0; "
          "for(i=0;i<n;i++){ a[i]=i; s+=b[i]; } return s;}")
PTRS = "void ptrs(int*p,int*q,int n){int i; for(i=0;i<n;i++){ p[i]=q[i]; }}"
CONSTOFF = "int constoff(){int a[4]; a[0]=1; a[1]=2; return a[0];}"
GACC = "int gacc; int accum(int*a,int n){int i; gacc=0; for(i=0;i<n;i++) gacc+=a[i]; return gacc;}"


# ── 1. affine self-index ───────────────────────────────────────────────────────

def test_self_index():
    print("affine self-index (a[i] vs a[i]): intra kept, carried removed")
    base, ref = _graphs(SELF, 'self')
    subset, rc_same, dropped = _soundness(base, ref)
    check("soundness: R2.2 mem edges subset of R2.1", subset)
    check("register/control edges identical", rc_same)
    check("some edges eliminated", dropped > 0)
    reasons = _elim_reasons(ref)
    check("siv-self-index-no-carry elimination present",
          reasons.get('siv-self-index-no-carry', 0) >= 1)
    # the a[i] intra access is a proven MUST-alias (same affine address)
    check("intra self-index kept as proven same-affine-address",
          any(e.reason == 'same-affine-address' and e.proven
              for e in ref.memory_edges()))
    # no carried memory edge survives between the two computed a[i] accesses
    comp_carried = [e for e in ref.memory_edges()
                    if e.carried and e.reason not in ('same-stack-slot',)]
    check("no surviving computed carried self-index edge",
          all(e.reason != 'same-affine-address' for e in comp_carried))
    check("validate clean", ref.validate() == [])


# ── 2. constant offsets ────────────────────────────────────────────────────────

def test_const_offsets():
    print("constant offsets (a[0] vs a[1]): distinct-const-offset")
    base, ref = _graphs(CONSTOFF, 'constoff')
    subset, rc_same, dropped = _soundness(base, ref)
    check("soundness subset", subset)
    check("register/control identical", rc_same)
    check("distinct-const-offset elimination present",
          _elim_reasons(ref).get('distinct-const-offset', 0) >= 1)
    check("validate clean", ref.validate() == [])


# ── 3. IV + clean slot (sumn) ──────────────────────────────────────────────────

def test_sumn_clean_slot():
    print("IV + clean slot (sumn): clean-slot-vs-computed removed, IV recur proven")
    base, ref = _graphs(SUMN, 'sumn')
    subset, rc_same, dropped = _soundness(base, ref)
    check("soundness subset", subset)
    check("register/control identical", rc_same)
    check("clean-slot-vs-computed eliminations present",
          _elim_reasons(ref).get('clean-slot-vs-computed', 0) >= 1)
    # the IV / accumulator recurrences survive and are proven same-stack-slot
    proven_carried = [e for e in ref.memory_edges(proven=True) if e.carried]
    check("IV/accumulator carried recurrences preserved as proven",
          len(proven_carried) >= 1)
    check("every surviving memory edge is on a stack slot (computed pruned)",
          all(e.reason == 'same-stack-slot' for e in ref.memory_edges()))
    check("validate clean", ref.validate() == [])


# ── 4. disjoint stack slots (two local arrays) ─────────────────────────────────

def test_distinct_local_arrays():
    print("disjoint stack slots (two local arrays): distinct-local-objects")
    base, ref = _graphs(TWOARR, 'twoarr')
    subset, rc_same, dropped = _soundness(base, ref)
    check("soundness subset", subset)
    check("register/control identical", rc_same)
    check("distinct-local-objects elimination present",
          _elim_reasons(ref).get('distinct-local-objects', 0) >= 1)
    check("validate clean", ref.validate() == [])


# ── 5. aliasing pointers (conservative) ────────────────────────────────────────

def test_aliasing_pointers():
    print("aliasing pointers (p[i] vs q[i]): conservatively KEPT")
    base, ref = _graphs(PTRS, 'ptrs')
    subset, rc_same, dropped = _soundness(base, ref)
    check("soundness subset", subset)
    check("register/control identical", rc_same)
    # the p[i] / q[i] cross dependence must remain (they may alias): a kept
    # memory edge tagged 'distinct-base' between two computed accesses.
    kept = [e for e in ref.memory_edges() if e.reason == 'distinct-base']
    check("p[i]/q[i] cross edge conservatively kept", len(kept) >= 1)
    check("kept p/q edge is NOT marked proven", all(not e.proven for e in kept))
    check("validate clean", ref.validate() == [])


# ── 6. loop-carried real dependence preserved (global accumulator) ─────────────

def test_carried_preserved():
    print("loop-carried global accumulator: real carried dep kept + proven")
    base, ref = _graphs(GACC, 'accum')
    subset, rc_same, dropped = _soundness(base, ref)
    check("soundness subset", subset)
    check("register/control identical", rc_same)
    # gacc += a[i]: the global accumulator has a genuine loop-carried RAW on the
    # same constant address -- it must survive and be proven.
    proven_global_carried = [
        e for e in ref.memory_edges(proven=True)
        if e.carried and e.reason == 'same-const-address']
    check("global accumulator carried dep preserved as proven",
          len(proven_global_carried) >= 1)
    check("validate clean", ref.validate() == [])


# ── 7. repeated identical access -> proven must-alias ──────────────────────────

def test_repeated_identical():
    print("repeated identical access: proven MUST-alias classification")
    base, ref = _graphs(SELF, 'self')
    # a[i] read and a[i] write in the same iteration are the identical address
    check("a proven same-affine-address edge exists",
          any(e.proven and e.reason == 'same-affine-address'
              for e in ref.memory_edges()))


# ── 8. regression compatibility (default path unchanged, no mutation) ──────────

def test_regression_default_unchanged():
    print("regression: R2.1 default graph and IR are unchanged")
    ir = _compile(SUMN)
    before = [repr(x) for x in ir]
    lo, hi = next((a, b) for a, b in func_slices(ir) if ir[a].name == 'sumn')
    g_default = DependenceGraph(ir, lo, hi)             # no disambiguator
    # default path: no eliminations, no proven/reason tags
    check("default graph eliminates nothing", g_default.eliminated_memory_edges == 0)
    check("default memory edges untagged",
          all(e.reason is None and not e.proven for e in g_default.memory_edges()))
    # building either graph never mutates the IR
    _ = disambiguate_function(ir, lo, hi)
    after = [repr(x) for x in ir]
    check("IR identical before/after both builds", before == after)


def test_all_fixtures_sound_and_valid():
    print("regression: every fixture is sound + valid")
    for code, fn in ((SUMN, 'sumn'), (SELF, 'self'), (TWOARR, 'twoarr'),
                     (PTRS, 'ptrs'), (CONSTOFF, 'constoff'), (GACC, 'accum')):
        base, ref = _graphs(code, fn)
        subset, rc_same, _ = _soundness(base, ref)
        check(f"{fn}: subset+identical-rc+valid",
              subset and rc_same and ref.validate() == [])


def main():
    for t in (test_self_index, test_const_offsets, test_sumn_clean_slot,
              test_distinct_local_arrays, test_aliasing_pointers,
              test_carried_preserved, test_repeated_identical,
              test_regression_default_unchanged, test_all_fixtures_sound_and_valid):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R2.2 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

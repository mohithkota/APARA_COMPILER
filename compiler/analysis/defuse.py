"""
defuse.py -- Def-Use analysis for one function slice.

Computes, over a single function slice [lo, hi] (inclusive), the definition
sites and use sites of every temporary, and the single-definition map. Built in
one linear pass; treat the result as an immutable snapshot -- if the IR is
mutated, discard it and construct a fresh DefUse.

WHY per-slice: temp names restart per function (Temp.reset() at each
IRFuncBegin), so a whole-program def/use map would conflate two functions'
identically-named temps. Constructing DefUse over one slice makes that class of
bug structurally impossible.

PRECISION (Milestone 1): full def/use *sites* for every name, and exact
single-definition facts. Precise per-definition def-use / use-def chains for
MULTIPLY-defined temporaries require reaching-definitions (a CFG-based
dataflow) and are intentionally NOT offered here -- they arrive with the CFG
analysis in a later milestone. No placeholder methods are exposed for them.
"""

from ir_utils import dest_names, src_names


class DefUse:
    """Def-Use analysis over instrs[lo..hi] (inclusive)."""

    def __init__(self, instrs, lo, hi):
        self._lo = lo
        self._hi = hi
        defs = {}   # name -> [def indices], in program order
        uses = {}   # name -> [use indices], in program order
        for k in range(lo, hi + 1):
            ins = instrs[k]
            for d in dest_names(ins):
                defs.setdefault(d, []).append(k)
            for u in src_names(ins):
                uses.setdefault(u, []).append(k)
        self._defs = defs
        self._uses = uses

    # ── definition / use sites ────────────────────────────────────────────────

    def def_sites(self, name):
        """All indices that DEFINE `name` in this slice (program order)."""
        return self._defs.get(name, [])

    def use_sites(self, name):
        """All indices that USE `name` in this slice (program order)."""
        return self._uses.get(name, [])

    # ── single-definition facts ───────────────────────────────────────────────

    def is_single_def(self, name):
        """True iff `name` has exactly one definition in this slice."""
        return len(self._defs.get(name, ())) == 1

    def is_multi_def(self, name):
        """True iff `name` has more than one definition in this slice."""
        return len(self._defs.get(name, ())) > 1

    def single_def(self, name):
        """The unique defining index of `name`, or None if it is not defined
        exactly once (undefined, or multiply-defined). For a single-def name
        this is also its unambiguous reaching definition at every use."""
        d = self._defs.get(name)
        return d[0] if d is not None and len(d) == 1 else None

    def single_defs(self):
        """{name: def_index} for every single-def name in this slice."""
        return {n: idx[0] for n, idx in self._defs.items() if len(idx) == 1}

    def multi_names(self):
        """The set of names with more than one definition in this slice."""
        return frozenset(n for n, idx in self._defs.items() if len(idx) > 1)

"""
dominators.py -- Dominator analysis (Milestone 4, analysis only).

Definitions:
  * Block A DOMINATES block B if every path from entry to B passes through A.
    (A dominates itself.)
  * A STRICTLY dominates B if A dominates B and A != B.
  * The IMMEDIATE dominator idom(B) is the unique strict dominator of B that is
    dominated by every other strict dominator of B -- i.e. B's closest strict
    dominator. The idom edges form the DOMINATOR TREE.

Algorithm: the classic iterative set data-flow formulation

    Dom(entry) = {entry}
    Dom(n)     = {n} ∪ ( ∩ over predecessors p of Dom(p) )

iterated over reverse post-order until a fixpoint. Complexity: O(N^2) set work
per sweep, O(d) sweeps where d is the loop-connectedness (a few in practice);
overall near O(N^2) for the sizes here -- deliberately simple, not the
Lengauer-Tarjan near-linear algorithm (which would be over-engineering now).
idom is then read off as the strict dominator with the largest dominator set.
"""

from .cfg import reverse_post_order


class Dominators:
    def __init__(self, dom, idom, entry):
        self._dom = dom          # block id -> frozenset of dominators
        self._idom = idom        # block id -> immediate dominator id (entry -> None)
        self.entry = entry

    def dominators(self, b):
        return self._dom.get(b, frozenset())

    def dominates(self, a, b):
        """True if a dominates b (a on every entry->b path)."""
        return a in self._dom.get(b, ())

    def strictly_dominates(self, a, b):
        return a != b and self.dominates(a, b)

    def immediate_dominator(self, b):
        """idom(b), or None for the entry / unreachable blocks."""
        return self._idom.get(b)

    def tree_children(self):
        """dominator-tree children: idom -> [blocks]."""
        kids = {}
        for b, d in self._idom.items():
            if d is not None:
                kids.setdefault(d, []).append(b)
        return kids


def compute_dominators(cfg):
    rpo = reverse_post_order(cfg)
    if not rpo:
        return Dominators({}, {}, cfg.entry_id)
    reachable = set(rpo)
    entry = cfg.entry_id

    dom = {b: set(reachable) for b in reachable}
    dom[entry] = {entry}

    changed = True
    while changed:
        changed = False
        for b in rpo:
            if b == entry:
                continue
            preds = [p for p in cfg.blocks[b].preds if p in reachable]
            if preds:
                it = iter(preds)
                inter = set(dom[next(it)])
                for p in it:
                    inter &= dom[p]
            else:
                inter = set()
            new = inter | {b}
            if new != dom[b]:
                dom[b] = new
                changed = True

    # immediate dominators: the strict dominator closest to b == the one with
    # the largest dominator set.
    idom = {entry: None}
    for b in rpo:
        if b == entry:
            continue
        sdoms = dom[b] - {b}
        idom[b] = max(sdoms, key=lambda d: len(dom[d])) if sdoms else None

    dom_frozen = {b: frozenset(s) for b, s in dom.items()}
    return Dominators(dom_frozen, idom, entry)

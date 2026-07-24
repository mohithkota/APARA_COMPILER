"""
verify.py -- LoopVerifier (Loop Optimization Framework, Milestone M0).

OBSERVE-ONLY. The verifier checks that a set of LoopDescriptors is internally
consistent with the CFG / dominator / loop-nesting facts they were built from.
It NEVER mutates the IR or the descriptors -- it only collects violations.

In later milestones the transaction-based transforms will run this same
verifier after each edit (and roll back on any violation). In M0 it is used to
validate discovery itself: every descriptor produced over the whole corpus must
verify clean, which is the M0 correctness gate.

Invariants checked (all read from borrowed analyses, no recomputation of CFG):

  cfg-integrity     every succ edge is mirrored by a pred edge and vice-versa
  header            the header block belongs to the loop body
  header-dominance  the header dominates every body block (natural-loop core)
  latch             each back-edge tail is in the body and targets the header
  reducibility      the header dominates every latch (back edge into a dominator)
  preheader         if present, it is outside the body and the UNIQUE external
                    predecessor of the header
  reachability      every body block is reachable from the function entry
  nesting           each child's body is a subset of this loop's body and the
                    child's parent link points back here; depth is consistent
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import reverse_post_order                            # noqa: E402


class VerifyResult:
    """Accumulated verification outcome. `ok` is True iff no violations."""

    __slots__ = ('violations',)

    def __init__(self):
        self.violations = []            # list of (descriptor, invariant, detail)

    @property
    def ok(self):
        return not self.violations

    def add(self, desc, invariant, detail):
        self.violations.append((desc, invariant, detail))

    def extend(self, other):
        self.violations.extend(other.violations)

    def report(self):
        if self.ok:
            return "LoopVerifier: OK (no violations)"
        lines = [f"LoopVerifier: {len(self.violations)} violation(s):"]
        for desc, inv, detail in self.violations:
            lines.append(f"  [{inv}] {desc!r}: {detail}")
        return "\n".join(lines)


class LoopVerifier:
    """Observe-only structural verifier for LoopDescriptors."""

    def verify(self, desc):
        """Verify one descriptor. Returns a VerifyResult (never raises for a
        mere violation; only genuine internal errors would raise)."""
        r = VerifyResult()
        cfg, dom = desc.cfg, desc.dom
        body = desc.body_blocks

        # cfg-integrity: succ/pred symmetry across the whole slice CFG
        for b in cfg.blocks:
            for s in b.succs:
                if b.id not in cfg.blocks[s].preds:
                    r.add(desc, 'cfg-integrity',
                          f'edge B{b.id}->B{s} missing from B{s}.preds')
            for p in b.preds:
                if b.id not in cfg.blocks[p].succs:
                    r.add(desc, 'cfg-integrity',
                          f'edge B{p}->B{b.id} missing from B{p}.succs')

        # header must be in its own body
        if desc.header not in body:
            r.add(desc, 'header', f'header B{desc.header} not in body')

        # header dominates every body block (defining property of a natural loop)
        for b in body:
            if not dom.dominates(desc.header, b):
                r.add(desc, 'header-dominance',
                      f'header B{desc.header} does not dominate body block B{b}')

        # latches / reducibility
        for (tail, head) in desc.back_edges:
            if head != desc.header:
                r.add(desc, 'latch', f'back-edge B{tail}->B{head} head is not the header')
            if tail not in body:
                r.add(desc, 'latch', f'latch B{tail} not in body')
            if not dom.dominates(desc.header, tail):
                r.add(desc, 'reducibility',
                      f'header B{desc.header} does not dominate latch B{tail}')
        for lt in desc.latches:
            if lt not in body:
                r.add(desc, 'latch', f'latch B{lt} not in body')

        # preheader: if known, outside body and unique external pred of header
        if desc.preheader is not None:
            if desc.preheader in body:
                r.add(desc, 'preheader', f'preheader B{desc.preheader} is inside the body')
            ext = [p for p in cfg.preds(desc.header) if p not in body]
            if ext != [desc.preheader]:
                r.add(desc, 'preheader',
                      f'preheader B{desc.preheader} is not the unique external '
                      f'predecessor of the header (external preds={ext})')

        # reachability: every body block reachable from entry
        reachable = set(reverse_post_order(cfg))
        for b in body:
            if b not in reachable:
                r.add(desc, 'reachability', f'body block B{b} unreachable from entry')

        # reducible flag consistency
        if not desc.reducible:
            r.add(desc, 'reducibility', 'descriptor marked non-reducible')

        # nesting: child body subset of parent body; back-links consistent
        for c in desc.children:
            if not c.body_blocks <= body:
                r.add(desc, 'nesting',
                      f'child (hdr B{c.header}) body is not a subset of parent body')
            if c.parent is not desc:
                r.add(desc, 'nesting',
                      f'child (hdr B{c.header}).parent does not point back to parent')
            if c.depth < desc.depth:
                r.add(desc, 'nesting',
                      f'child (hdr B{c.header}) depth {c.depth} < parent depth {desc.depth}')
        return r

    def verify_all(self, descs):
        """Verify a list of descriptors; aggregate all violations."""
        agg = VerifyResult()
        for d in descs:
            agg.extend(self.verify(d))
        return agg

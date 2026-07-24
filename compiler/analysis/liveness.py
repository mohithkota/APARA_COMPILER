r"""
liveness.py -- Live-variable analysis (Milestone 4, analysis only).

Classic backward data-flow over the CFG. A temp is LIVE at a point if it may be
read later before being redefined. Per basic block we compute:

    use[B]  = temps read in B before any (re)definition of them in B
              (upward-exposed uses)
    def[B]  = temps defined (written) anywhere in B

    LiveOut[B] = ∪ over successors S of  LiveIn[S]
    LiveIn[B]  = use[B] ∪ ( LiveOut[B] \ def[B] )

Iterated to a fixpoint (sets only grow, so it converges). We sweep in reverse
reverse-post-order (i.e. from exits toward entry), which speeds convergence for
backward flow. Reuses the shared ir_utils def/use primitives that DefUse is
built on -- the same notion of a Temp definition and use.

This is analysis ONLY: it computes LiveIn/LiveOut and changes nothing about
register allocation, spilling, coloring, or interference. Per function slice
(temp names are function-local).
"""

from ir_utils import dest_names, src_names
from .cfg import reverse_post_order


class Liveness:
    def __init__(self, live_in, live_out, use, defs):
        self._in = live_in       # block id -> frozenset live-in
        self._out = live_out      # block id -> frozenset live-out
        self.use = use            # block id -> frozenset upward-exposed uses
        self.defs = defs          # block id -> frozenset defined

    def live_in(self, b):
        return self._in.get(b, frozenset())

    def live_out(self, b):
        return self._out.get(b, frozenset())


def _block_use_def(cfg, b):
    """(use, def) for one block: upward-exposed uses and all definitions."""
    used, defined = set(), set()
    for i in range(b.lo, b.hi + 1):
        ins = cfg.instrs[i]
        for s in src_names(ins):
            if s not in defined:
                used.add(s)               # read before any local def -> exposed
        for d in dest_names(ins):
            defined.add(d)
    return used, defined


def compute_liveness(cfg):
    """Backward live-variable data-flow. Returns a Liveness object with
    per-block LiveIn / LiveOut."""
    use, defs = {}, {}
    for b in cfg.blocks:
        u, d = _block_use_def(cfg, b)
        use[b.id] = u
        defs[b.id] = d

    live_in = {b.id: set() for b in cfg.blocks}
    live_out = {b.id: set() for b in cfg.blocks}

    order = list(reversed(reverse_post_order(cfg)))    # exits first, toward entry
    ids = [b.id for b in cfg.blocks]
    if not order:
        order = ids

    changed = True
    while changed:
        changed = False
        for bid in order:
            out = set()
            for s in cfg.blocks[bid].succs:
                out |= live_in[s]
            inn = use[bid] | (out - defs[bid])
            if out != live_out[bid] or inn != live_in[bid]:
                live_out[bid] = out
                live_in[bid] = inn
                changed = True

    return Liveness({b: frozenset(s) for b, s in live_in.items()},
                    {b: frozenset(s) for b, s in live_out.items()},
                    {b: frozenset(s) for b, s in use.items()},
                    {b: frozenset(s) for b, s in defs.items()})

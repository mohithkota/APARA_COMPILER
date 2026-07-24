"""
loopinfo.py -- Natural loop detection (Milestone 4, analysis only).

Natural loop definition:
  * A BACK EDGE is a CFG edge u -> v whose head v DOMINATES its tail u.
  * The NATURAL LOOP of back edge u -> v is v (the HEADER) plus every block that
    can reach u without passing through v -- i.e. {v} together with all nodes
    from which u is reachable in the graph with v removed.
  * Back edges that share a header are merged into one loop (its body is the
    union, its back edges the set of tails).

For each loop we expose its header, body blocks, and back edges. Loop NESTING
depth of a block is the number of loop bodies that contain it; the loop tree is
implied by body containment (loop A is nested in B when A's header lies in B's
body and A != B). LICM and software pipelining will reuse this.

Requires a CFG and its Dominators (from cfg.py / dominators.py).
"""


class Loop:
    __slots__ = ('header', 'body', 'back_edges')

    def __init__(self, header):
        self.header = header
        self.body = {header}         # set of block ids
        self.back_edges = []         # list of (tail, header)

    def __repr__(self):
        return f"Loop(header=B{self.header}, |body|={len(self.body)})"


class LoopInfo:
    def __init__(self, loops, depth, max_depth):
        self.loops = loops                       # list of Loop
        self._depth = depth                      # block id -> loop-nesting depth
        self.max_depth = max_depth

    def depth(self, block_id):
        """How many loops contain this block (0 = not in any loop)."""
        return self._depth.get(block_id, 0)

    def num_loops(self):
        return len(self.loops)


def _natural_loop_body(cfg, header, tail):
    """{header} plus all blocks that reach `tail` without going through
    `header` (standard backward flood from tail, stopping at header)."""
    body = {header}
    if tail != header:
        body.add(tail)
        stack = [tail]
        while stack:
            b = stack.pop()
            for p in cfg.blocks[b].preds:
                if p not in body:
                    body.add(p)
                    stack.append(p)
    return body


def build_loop_info(cfg, dom):
    """Detect natural loops from back edges (edge u->v with v dominating u),
    merging by header. Returns a LoopInfo."""
    by_header = {}
    for b in cfg.blocks:
        for s in b.succs:
            if dom.dominates(s, b.id):            # s is header, b is tail
                loop = by_header.get(s)
                if loop is None:
                    loop = Loop(s)
                    by_header[s] = loop
                loop.back_edges.append((b.id, s))
                loop.body |= _natural_loop_body(cfg, s, b.id)

    loops = list(by_header.values())

    # nesting depth = number of loop bodies containing a block
    depth = {}
    for loop in loops:
        for blk in loop.body:
            depth[blk] = depth.get(blk, 0) + 1
    max_depth = max(depth.values(), default=0)

    return LoopInfo(loops, depth, max_depth)

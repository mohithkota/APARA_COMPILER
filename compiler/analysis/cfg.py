"""
cfg.py -- Control Flow Graph (Milestone 4, analysis only).

Builds an explicit CFG over one function's instruction slice: basic blocks as
nodes, with predecessor/successor edges. Pure analysis -- it never mutates the
IR, and nothing in the compiler consumes it yet.

CFG invariants:
  * Every block is a maximal straight-line run beginning at a LEADER. A leader
    is: the first instruction of the slice, any IRLabel (a branch target), or
    the instruction immediately after a terminator.
  * A terminator is IRJump / IRCondJump / IRReturn / IRHalt. Calls are NOT
    terminators here -- control returns to the next instruction, so a call is an
    ordinary mid-block instruction (this is the standard CFG treatment, and is
    intentionally different from ir_utils.basic_blocks, which splits at calls
    for a stricter block-local partition).
  * Successor edges: IRJump -> its target; IRCondJump -> {true target, false
    target or fall-through}; IRReturn/IRHalt -> none; anything else -> the next
    block (fall-through, e.g. a block ending just before a label).
  * Blocks are numbered by program order; block 0 (the slice's first) is entry.

This is a per-FUNCTION graph (temp names and labels are function-local); build
one CFG per function slice.
"""

_TERMINATORS = ('IRJump', 'IRCondJump', 'IRReturn', 'IRHalt')


class Block:
    __slots__ = ('id', 'lo', 'hi', 'label', 'succs', 'preds')

    def __init__(self, bid, lo, hi, label=None):
        self.id = bid
        self.lo = lo            # first instruction index (inclusive)
        self.hi = hi            # last  instruction index (inclusive)
        self.label = label      # label name if this block starts with an IRLabel
        self.succs = []         # successor block ids
        self.preds = []         # predecessor block ids

    def __repr__(self):
        return f"B{self.id}[{self.lo}..{self.hi}]"


class CFG:
    """Control flow graph for instrs[lo..hi] (one function)."""

    def __init__(self, instrs, lo, hi, blocks, label_to_block):
        self.instrs = instrs
        self.lo = lo
        self.hi = hi
        self.blocks = blocks                    # list indexed by block id
        self.entry_id = 0 if blocks else None
        self.label_to_block = label_to_block

    def block_instrs(self, bid):
        b = self.blocks[bid]
        return self.instrs[b.lo:b.hi + 1]

    def succs(self, bid):
        return self.blocks[bid].succs

    def preds(self, bid):
        return self.blocks[bid].preds

    def dump(self):
        """Human-readable CFG (debug only)."""
        lines = [f"CFG [{self.lo}..{self.hi}]  ({len(self.blocks)} blocks)"]
        for b in self.blocks:
            tag = f" ({b.label}:)" if b.label else ""
            lines.append(f"  B{b.id}{tag}  instrs {b.lo}..{b.hi}"
                         f"  succs={b.succs}  preds={b.preds}")
        return "\n".join(lines)


def build_cfg(instrs, lo=None, hi=None):
    """Build a CFG for instrs[lo..hi] (defaults to the whole list). Returns a
    CFG object. O(n) in the slice length."""
    if lo is None:
        lo = 0
    if hi is None:
        hi = len(instrs) - 1
    if hi < lo:
        return CFG(instrs, lo, hi, [], {})

    # 1. leaders
    leaders = {lo}
    for i in range(lo, hi + 1):
        c = type(instrs[i]).__name__
        if c == 'IRLabel':
            leaders.add(i)
        if c in _TERMINATORS and i + 1 <= hi:
            leaders.add(i + 1)
    starts = sorted(leaders)

    # 2. blocks + label map
    blocks = []
    label_to_block = {}
    for idx, s in enumerate(starts):
        e = (starts[idx + 1] - 1) if idx + 1 < len(starts) else hi
        label = instrs[s].name if type(instrs[s]).__name__ == 'IRLabel' else None
        b = Block(idx, s, e, label)
        blocks.append(b)
        if label is not None:
            label_to_block[label] = idx

    # 3. successor edges
    def tgt(name):
        return label_to_block.get(name)

    for b in blocks:
        last = instrs[b.hi]
        c = type(last).__name__
        succ = []
        if c == 'IRJump':
            t = tgt(last.label)
            if t is not None:
                succ = [t]
        elif c == 'IRCondJump':
            t = tgt(last.true_label)
            if t is not None:
                succ.append(t)
            if last.false_label is not None:
                f = tgt(last.false_label)
                if f is not None and f not in succ:
                    succ.append(f)
            elif b.id + 1 < len(blocks) and (b.id + 1) not in succ:
                succ.append(b.id + 1)          # false path falls through
        elif c in ('IRReturn', 'IRHalt'):
            succ = []
        else:
            if b.id + 1 < len(blocks):
                succ = [b.id + 1]              # fall-through
        b.succs = succ

    # 4. predecessors (invert)
    for b in blocks:
        for s in b.succs:
            blocks[s].preds.append(b.id)

    return CFG(instrs, lo, hi, blocks, label_to_block)


def reverse_post_order(cfg):
    """Reverse post-order of blocks reachable from entry. RPO visits a node
    before its non-back-edge successors -- the standard order for forward
    data-flow (dominators). Iterative DFS to avoid recursion limits."""
    if cfg.entry_id is None:
        return []
    visited = set()
    post = []
    # stack holds (block_id, child_iterator_index)
    stack = [(cfg.entry_id, 0)]
    visited.add(cfg.entry_id)
    while stack:
        bid, ci = stack[-1]
        succs = cfg.blocks[bid].succs
        if ci < len(succs):
            stack[-1] = (bid, ci + 1)
            s = succs[ci]
            if s not in visited:
                visited.add(s)
                stack.append((s, 0))
        else:
            post.append(bid)
            stack.pop()
    post.reverse()
    return post

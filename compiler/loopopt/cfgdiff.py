"""
cfgdiff.py -- CFG differencing utility (developer/debugging tool for M4).

This is NOT part of the optimization pipeline. It is the observability tool the
milestone requires: given a before/after snapshot of the CFG around an IR
mutation, it reports exactly what changed structurally. Every canonicalization
test uses it so that every IR mutation is observable and justified.

    diff_cfg(before, after)                 -> CFGDiff  (blocks/edges added/removed)
    diff_loop(before_desc, after_desc)      -> LoopDiff (header/latch/exit/preheader)

Block identity across a diff is by LABEL. A loop's structurally significant
blocks -- header, latch, preheader, exit targets -- are all branch targets and
therefore carry an IRLabel, and the LoopCanonicalizer only ADDS labeled blocks
and REWRITES branch-target labels: it never renames or deletes an existing
label and never edits a non-terminator instruction. So label identity is stable
exactly where it matters.

Label-less straight-line blocks have no stable name (block ids renumber the
moment an instruction is spliced in), so they are keyed by a CONTENT SIGNATURE
-- the tuple of their instruction type names. This keeps the diff total and
makes it robust to renumbering; it is a debugging heuristic, not a semantic
identity, and it is never used to describe a loop's canonical structure (that
part is driven entirely off labels via diff_loop).
"""


# ── block identity ────────────────────────────────────────────────────────────

def _block_key(cfg, bid):
    """A rename/renumber-stable key for a block: its label if it has one, else a
    content signature over its instruction type names (see module docstring)."""
    b = cfg.blocks[bid]
    if b.label is not None:
        return f"L:{b.label}"
    types = tuple(type(cfg.instrs[i]).__name__ for i in range(b.lo, b.hi + 1))
    return "~:" + ",".join(types)


def _lbl(cfg, bid):
    """Readable name of a block for loop-structure reporting (label, or ~Bn)."""
    if bid is None:
        return None
    lb = cfg.blocks[bid].label
    return lb if lb is not None else f"~B{bid}"


def _keyed_edges(cfg):
    """Set of (src_key, dst_key) edges over the whole CFG."""
    out = set()
    for b in cfg.blocks:
        sk = _block_key(cfg, b.id)
        for s in b.succs:
            out.add((sk, _block_key(cfg, s)))
    return out


# ── whole-CFG diff ────────────────────────────────────────────────────────────

class CFGDiff:
    """Structural delta between two CFGs. `empty` iff nothing changed."""

    __slots__ = ('blocks_added', 'blocks_removed', 'edges_added', 'edges_removed')

    def __init__(self, blocks_added, blocks_removed, edges_added, edges_removed):
        self.blocks_added = blocks_added        # sorted list of block keys
        self.blocks_removed = blocks_removed
        self.edges_added = edges_added          # sorted list of (src_key, dst_key)
        self.edges_removed = edges_removed

    @property
    def empty(self):
        return not (self.blocks_added or self.blocks_removed
                    or self.edges_added or self.edges_removed)

    def report(self):
        if self.empty:
            return "CFGDiff: (no structural change)"
        lines = ["CFGDiff:"]
        for k in self.blocks_added:
            lines.append(f"  + block {k}")
        for k in self.blocks_removed:
            lines.append(f"  - block {k}")
        for (a, b) in self.edges_added:
            lines.append(f"  + edge  {a} -> {b}")
        for (a, b) in self.edges_removed:
            lines.append(f"  - edge  {a} -> {b}")
        return "\n".join(lines)

    def __repr__(self):
        return (f"CFGDiff(+{len(self.blocks_added)}/-{len(self.blocks_removed)} blocks, "
                f"+{len(self.edges_added)}/-{len(self.edges_removed)} edges)")


def diff_cfg(before, after):
    """Compare two CFGs and return a CFGDiff (blocks/edges added & removed).

    Both CFGs must be for the SAME function slice (before/after one mutation);
    comparing CFGs of different functions is meaningless because labels/keys are
    function-local."""
    b_keys = {_block_key(before, b.id) for b in before.blocks}
    a_keys = {_block_key(after, b.id) for b in after.blocks}
    b_edges = _keyed_edges(before)
    a_edges = _keyed_edges(after)
    return CFGDiff(
        blocks_added=sorted(a_keys - b_keys),
        blocks_removed=sorted(b_keys - a_keys),
        edges_added=sorted(a_edges - b_edges),
        edges_removed=sorted(b_edges - a_edges),
    )


# ── per-loop structural diff ──────────────────────────────────────────────────

class LoopDiff:
    """Change in one loop's canonical anchors (header / preheader / latches /
    exits), reported by LABEL so it survives block renumbering. Also carries the
    whole-slice CFGDiff for context."""

    __slots__ = ('cfg_diff', 'header_change', 'preheader_change',
                 'latch_change', 'exit_change')

    def __init__(self, cfg_diff, header_change, preheader_change,
                 latch_change, exit_change):
        self.cfg_diff = cfg_diff
        self.header_change = header_change          # (before_lbl, after_lbl) or None
        self.preheader_change = preheader_change    # (before_lbl, after_lbl) or None
        self.latch_change = latch_change            # (before_set, after_set) or None
        self.exit_change = exit_change              # (before_set, after_set) or None

    @property
    def identity_preserved(self):
        """A canonicalization must NEVER move a loop's header."""
        return self.header_change is None

    @property
    def structurally_changed(self):
        return not self.cfg_diff.empty

    def report(self):
        lines = [self.cfg_diff.report()]
        if self.header_change:
            lines.append(f"  header:    {self.header_change[0]} -> {self.header_change[1]}")
        if self.preheader_change:
            lines.append(f"  preheader: {self.preheader_change[0]} -> {self.preheader_change[1]}")
        if self.latch_change:
            lines.append(f"  latches:   {sorted(self.latch_change[0])} -> {sorted(self.latch_change[1])}")
        if self.exit_change:
            lines.append(f"  exits:     {sorted(self.exit_change[0])} -> {sorted(self.exit_change[1])}")
        return "\n".join(lines)

    def __repr__(self):
        return (f"LoopDiff(changed={self.structurally_changed}, "
                f"identity_preserved={self.identity_preserved})")


def _latch_labels(desc):
    return frozenset(_lbl(desc.cfg, lt) for lt in desc.latches)


def _exit_labels(desc):
    return frozenset((_lbl(desc.cfg, b), _lbl(desc.cfg, s)) for (b, s) in desc.exit_edges)


def diff_loop(before_desc, after_desc):
    """Structural diff between two descriptors of the SAME loop (same header
    identity), one built before a mutation and one after. Reports header /
    preheader / latch / exit changes by label plus the underlying CFGDiff."""
    cfg_diff = diff_cfg(before_desc.cfg, after_desc.cfg)

    hb = _lbl(before_desc.cfg, before_desc.header)
    ha = _lbl(after_desc.cfg, after_desc.header)
    header_change = (hb, ha) if hb != ha else None

    pb = _lbl(before_desc.cfg, before_desc.preheader)
    pa = _lbl(after_desc.cfg, after_desc.preheader)
    preheader_change = (pb, pa) if pb != pa else None

    lb, la = _latch_labels(before_desc), _latch_labels(after_desc)
    latch_change = (lb, la) if lb != la else None

    eb, ea = _exit_labels(before_desc), _exit_labels(after_desc)
    exit_change = (eb, ea) if eb != ea else None

    return LoopDiff(cfg_diff, header_change, preheader_change,
                    latch_change, exit_change)

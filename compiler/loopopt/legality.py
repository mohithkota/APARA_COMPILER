"""
legality.py -- Loop legality framework (Loop Optimization Framework, M7).

A shared library of small, reusable LEGALITY PREDICATES that answer "is this
structural/analytic property true of this loop?" -- and nothing else. It is NOT
a pass and performs NO transformation: every predicate is a pure query over a
LoopDescriptor (and, for the analysis-backed ones, the M1/M2/M3 analyses already
attached to it). Its purpose is to eliminate the legality logic that individual
transforms would otherwise each re-implement, so that from M7 on every loop
transform asks the SAME questions the SAME way.

Each predicate returns a LegalityFact (truthy when the property holds), carrying
a human-readable reason and a `known` flag (False when the backing analysis was
unavailable and a conservative answer was returned). `evaluate(desc, [...])`
runs a list of predicates and returns the first failing fact (or a passing
"all" fact), which is how a transform composes exactly the predicates it needs.

Reuses (never duplicates): the descriptor's own structural fields (M0), the
canonicalizer's shared-exit helper (M4), and the InductionVars / MemEffects /
Profile analyses (M1/M2/M3), annotating a descriptor on demand when a caller
queries an analysis-backed predicate on an un-annotated loop.
"""

from ir_utils import src_names, dest_names
from .descriptor import TOP_TESTED, BOTTOM_TESTED
from .canonicalize import _targets, LoopCanonicalizer

# Reuse the canonicalizer's shared-exit query (a staticmethod on the frozen M4
# component) without duplicating it or modifying that component.
_shared_exit_edges = LoopCanonicalizer._shared_exit_edges


# ── result type ───────────────────────────────────────────────────────────────

class LegalityFact:
    """The outcome of one legality query. Truthy iff the property holds."""

    __slots__ = ('name', 'ok', 'reason', 'known')

    def __init__(self, name, ok, reason='', known=True):
        self.name = name
        self.ok = ok
        self.reason = reason
        self.known = known          # False => backing analysis missing; ok is conservative

    def __bool__(self):
        return self.ok

    def __repr__(self):
        s = 'ok' if self.ok else f'no({self.reason})'
        return f"LegalityFact({self.name}: {s}{'' if self.known else ' [unknown]'})"


def _fact(name, ok, reason='', known=True):
    return LegalityFact(name, ok, reason, known)


def evaluate(desc, predicates):
    """Run predicates in order; return the FIRST failing fact, else a passing
    'all' fact. This is how a transform composes the exact legality it requires."""
    for p in predicates:
        f = p(desc)
        if not f.ok:
            return f
    return _fact('all', True)


# ── structural predicates (pure functions of M0 descriptor fields) ────────────

def has_labeled_header(desc):
    ok = desc.cfg.blocks[desc.header].label is not None
    return _fact('has_labeled_header', ok, '' if ok else 'header has no label')


def is_top_tested(desc):
    ok = desc.shape == TOP_TESTED
    return _fact('is_top_tested', ok,
                 '' if ok else f'shape is {desc.shape}, not top-tested')


def is_bottom_tested(desc):
    ok = desc.shape == BOTTOM_TESTED
    return _fact('is_bottom_tested', ok,
                 '' if ok else f'shape is {desc.shape}, not bottom-tested')


def has_unique_preheader(desc):
    ok = desc.preheader is not None
    return _fact('has_unique_preheader', ok,
                 '' if ok else 'no unique preheader')


def has_single_latch(desc):
    ok = len(desc.latches) == 1
    return _fact('has_single_latch', ok,
                 '' if ok else f'{len(desc.latches)} latches (need exactly 1)')


def has_explicit_backedge(desc):
    """The (single) latch reaches the header by an explicit branch we can name."""
    if len(desc.latches) != 1:
        return _fact('has_explicit_backedge', False,
                     'not a single-latch loop', known=True)
    cfg = desc.cfg
    hlbl = cfg.blocks[desc.header].label
    term = cfg.instrs[cfg.blocks[desc.latches[0]].hi]
    ok = _targets(term, hlbl)
    return _fact('has_explicit_backedge', ok,
                 '' if ok else 'back edge is not an explicit branch')


def has_dedicated_exits(desc):
    """Every explicit loop-exit target is entered only from inside the loop
    (the M4 canonical dedicated-exit property). Reuses the canonicalizer helper."""
    shared = _shared_exit_edges(desc)
    ok = not shared
    return _fact('has_dedicated_exits', ok,
                 '' if ok else f'{len(shared)} exit edge(s) target a shared block')


# Instruction classes that make a header UNSAFE to treat as a pure guard (a side
# effect or a control transfer other than the trailing conditional branch).
_HEADER_UNSAFE = frozenset({
    'IRStore', 'IRStoreWide', 'IRGlobalStore', 'IRCall', 'IRIndirectCall',
    'IRVaStart', 'IRReturn', 'IRHalt', 'IRJump', 'IRCondJump',
})


def has_side_effect_free_header(desc):
    """The header is a straight-line block that ends in exactly one conditional
    branch and contains no side effect / extra control transfer before it."""
    cfg = desc.cfg
    hblk = cfg.blocks[desc.header]
    if type(cfg.instrs[hblk.hi]).__name__ != 'IRCondJump':
        return _fact('has_side_effect_free_header', False,
                     'header does not end in a conditional branch')
    for i in range(hblk.lo + 1, hblk.hi):
        if type(cfg.instrs[i]).__name__ in _HEADER_UNSAFE:
            return _fact('has_side_effect_free_header', False,
                         'header has a side effect / is not a pure guard')
    return _fact('has_side_effect_free_header', True)


def header_has_single_exit_test(desc):
    """The header branches to exactly one in-loop successor and one exit
    successor, and both are reachable via an explicit label."""
    cfg = desc.cfg
    body = desc.body_blocks
    succs = cfg.succs(desc.header)
    in_loop = [s for s in succs if s in body]
    out = [s for s in succs if s not in body]
    if len(in_loop) != 1 or len(out) != 1:
        return _fact('header_has_single_exit_test', False,
                     'header does not have exactly one in-loop and one exit successor')
    if cfg.blocks[in_loop[0]].label is None:
        return _fact('header_has_single_exit_test', False, 'body target has no label')
    if cfg.blocks[out[0]].label is None:
        return _fact('header_has_single_exit_test', False, 'exit target has no label')
    return _fact('header_has_single_exit_test', True)


def guard_inputs_loop_independent(desc):
    """No value the header's guard reads is produced inside the loop body (so the
    guard can be evaluated at the preheader and at the latch)."""
    cfg = desc.cfg
    hblk = cfg.blocks[desc.header]
    header_uses = set()
    for i in range(hblk.lo + 1, hblk.hi + 1):
        header_uses.update(src_names(cfg.instrs[i]))
    body_defs = set()
    for b in desc.body_blocks:
        if b == desc.header:
            continue
        blk = cfg.blocks[b]
        for i in range(blk.lo, blk.hi + 1):
            body_defs.update(dest_names(cfg.instrs[i]))
    clash = header_uses & body_defs
    ok = not clash
    return _fact('guard_inputs_loop_independent', ok,
                 '' if ok else f'header guard depends on body-defined value(s) {sorted(clash)}')


# ── analysis-backed predicates (reuse M1/M2/M3; annotate on demand) ───────────

def _ensure_iv(desc):
    from .analysis_iv import annotate_induction_vars
    if not (desc.iv_form is not None or desc.basic_ivs
            or desc.primary_iv is not None or desc.is_counted):
        annotate_induction_vars([desc])


def _ensure_mem(desc):
    from .analysis_mem import annotate_memory_effects
    if not desc.mem_analyzed:
        annotate_memory_effects([desc])


def _ensure_profile(desc):
    from .analysis_profile import annotate_profile
    if not desc.profile_analyzed:
        annotate_profile([desc])


def has_clean_iv(desc):
    """A recognizable primary induction variable governs the loop's exit test
    (reuses the M1 InductionVars analysis; annotated on demand)."""
    _ensure_iv(desc)
    ok = desc.primary_iv is not None
    reason = '' if ok else 'no recognizable primary induction variable'
    return _fact('has_clean_iv', ok, reason)


def memory_safe(desc):
    """The loop's memory effects are fully analyzed and contain no opaque call
    (a call could read/write arbitrary memory). Reuses the M2 MemEffects
    analysis; annotated on demand. A FACT for motion-style transforms -- rotation
    does not require it."""
    _ensure_mem(desc)
    if not desc.mem_analyzed:
        return _fact('memory_safe', False, 'memory effects not analyzable', known=False)
    ok = len(desc.calls) == 0
    reason = ('' if ok
              else f'{len(desc.calls)} opaque call(s) with unbounded memory effects')
    return _fact('memory_safe', ok, reason)


def profile_suitable(desc):
    """Profile facts (trip/critical-path/MII/IPB) are available to consult. This
    reports availability, NOT a cost decision (legality reports facts only).
    Reuses the M3 Profile analysis; annotated on demand."""
    _ensure_profile(desc)
    ok = desc.profile_analyzed and desc.body_inst_count > 0
    if not desc.profile_analyzed:
        return _fact('profile_suitable', False, 'profile not analyzed', known=False)
    reason = ('' if ok else 'empty loop body'
              ) + f' [N={desc.body_inst_count} mii={desc.mii} est_ipb={desc.est_ipb:.2f}]'
    return _fact('profile_suitable', ok, reason.strip())


# The public predicate menu (names match the milestone's list plus the
# rotation-relevant structural queries). Every future transform composes from
# this set instead of re-implementing legality.
PREDICATES = {
    'has_labeled_header': has_labeled_header,
    'is_top_tested': is_top_tested,
    'is_bottom_tested': is_bottom_tested,
    'has_unique_preheader': has_unique_preheader,
    'has_single_latch': has_single_latch,
    'has_explicit_backedge': has_explicit_backedge,
    'has_dedicated_exits': has_dedicated_exits,
    'has_side_effect_free_header': has_side_effect_free_header,
    'header_has_single_exit_test': header_has_single_exit_test,
    'guard_inputs_loop_independent': guard_inputs_loop_independent,
    'has_clean_iv': has_clean_iv,
    'memory_safe': memory_safe,
    'profile_suitable': profile_suitable,
}

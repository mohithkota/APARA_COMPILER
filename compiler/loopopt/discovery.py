"""
discovery.py -- LoopDiscovery (Loop Optimization Framework, Milestone M0).

Converts the EXISTING analysis results into LoopDescriptor objects. It does not
implement any control-flow analysis of its own: it reuses, unchanged,

    ir_utils.func_slices        -- per-function IR slicing
    analysis.build_cfg          -- control-flow graph
    analysis.compute_dominators -- dominator tree
    analysis.build_loop_info    -- natural loops (back edges / dominance)
    analysis.compute_liveness   -- live-in / live-out (borrowed onto descriptors)

Everything discovery adds is DERIVED from those results (preheader, exit edges,
shape classification, nesting links). Read-only: it never mutates the IR.

Scope: analyses are per FUNCTION SLICE (temp names and block ids restart per
function), so discovery builds one CFG/dom/loopinfo/liveness set per slice and
links nesting only within that slice.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir_utils import func_slices                                   # noqa: E402
from analysis import (build_cfg, compute_dominators, build_loop_info,  # noqa: E402
                      compute_liveness)
from .descriptor import (LoopDescriptor, TOP_TESTED, BOTTOM_TESTED,  # noqa: E402
                        IRREGULAR)


def _classify_shape(cfg, header, latches, body, exit_edges):
    """Observe (never change) the loop's test placement.

    TOP_TESTED   : the header itself has an edge leaving the loop AND ends in a
                   conditional branch -- the classic while/for guard.
    BOTTOM_TESTED: the header has no exit edge and the (single) latch ends in a
                   conditional branch -- do-while form.
    IRREGULAR    : anything else (multi-exit, exit not at header or latch, etc.);
                   later legality tiers decide what to do with these.
    """
    header_exits = any(b == header for (b, _s) in exit_edges)
    header_term = type(cfg.instrs[cfg.blocks[header].hi]).__name__
    if header_exits and header_term == 'IRCondJump':
        return TOP_TESTED
    if not header_exits and len(latches) == 1:
        latch_term = type(cfg.instrs[cfg.blocks[latches[0]].hi]).__name__
        latch_exits = any(b == latches[0] for (b, _s) in exit_edges)
        if latch_term == 'IRCondJump' and latch_exits:
            return BOTTOM_TESTED
    return IRREGULAR


def _build_descriptor(func_slice, epoch, cfg, dom, li, lv, loop):
    """Populate one LoopDescriptor's identity/structural fields from analyses."""
    d = LoopDescriptor(func_slice, epoch, loop, cfg, dom, li, lv)
    body = d.body_blocks
    header = d.header

    # latches = distinct back-edge tails (a natural loop may have several)
    d.latches = sorted({tail for (tail, _h) in d.back_edges})

    # preheader = the unique predecessor of the header from OUTSIDE the loop.
    # If there is not exactly one, leave it None -- the Canonicalizer (M4) will
    # create one later; M0 only reports the current structure.
    external_preds = [p for p in cfg.preds(header) if p not in body]
    d.preheader = external_preds[0] if len(external_preds) == 1 else None

    # exit edges = every CFG edge leaving the loop body
    exit_edges = []
    for b in body:
        for s in cfg.succs(b):
            if s not in body:
                exit_edges.append((b, s))
    d.exit_edges = sorted(exit_edges)
    d.exiting_blocks = sorted({b for (b, _s) in exit_edges})
    d.exit_blocks = sorted({s for (_b, s) in exit_edges})

    d.shape = _classify_shape(cfg, header, d.latches, body, exit_edges)
    return d


def _link_nesting(descs):
    """Link parent/child/innermost/outermost within ONE function slice.

    Parent of L = the smallest OTHER loop whose body contains L's header (the
    closest enclosing loop). Only descriptors from the same slice are compared,
    because block ids are slice-local (comparing across slices would alias
    identically-numbered blocks of different functions)."""
    for d in descs:
        enclosers = [e for e in descs
                     if e is not d and d.header in e.body_blocks]
        d.parent = min(enclosers, key=lambda e: len(e.body_blocks)) if enclosers else None
    for d in descs:
        d.children = [e for e in descs if e.parent is d]
        d.is_innermost = not d.children
        d.is_outermost = d.parent is None


def discover_function(instrs, lo, hi, epoch=0):
    """Discover every natural loop in the function slice instrs[lo..hi].

    Returns a list of LoopDescriptor (nesting linked). Builds one CFG / dom /
    loopinfo / liveness for the slice and shares those handles across all its
    descriptors."""
    cfg = build_cfg(instrs, lo, hi)
    dom = compute_dominators(cfg)
    li = build_loop_info(cfg, dom)
    lv = compute_liveness(cfg)
    descs = [_build_descriptor((lo, hi), epoch, cfg, dom, li, lv, loop)
             for loop in li.loops]
    _link_nesting(descs)
    return descs


def discover(instrs, epoch=0):
    """Discover every natural loop in every function of `instrs`.

    Innermost-first within each function (the order LICM/IVSR/unrolling want),
    then functions in program order. Pure analysis -- no IR mutation."""
    out = []
    for (lo, hi) in func_slices(instrs):
        descs = discover_function(instrs, lo, hi, epoch)
        descs.sort(key=lambda d: (-d.depth, d.header))   # innermost-first
        out.extend(descs)
    return out

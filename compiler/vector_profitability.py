"""
vector_profitability.py -- Vector Profitability Infrastructure (Milestone R4.0).

ESTIMATION ONLY. Given a legal kernel (vector_legality.py), estimates the benefit
of vectorizing it -- it makes no decision and emits no code. Future vector passes
consult these estimates (as R3.1/R3.2 consulted the oracle) to decide whether a
vectorization is worth attempting.

Estimates (all from the recognised structure + the capability layer's lane count;
no new analysis):

  * lanes                 : elements processed per vector instruction
  * scalar_body_ops       : data ops in one scalar iteration (from M3 profile)
  * instruction_reduction : fraction of dynamic instructions removed
  * bundle_reduction      : approximate dynamic bundle reduction
  * throughput_gain       : effective speed-up (~ lanes, capped by the ISA)
  * vector_utilization    : lanes used / bundle issue width
  * remainder_cost        : scalar tail iterations (trip % lanes)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir_utils import func_slices
from loopopt.discovery import discover_function
from loopopt.analysis_iv import annotate_induction_vars
from loopopt.analysis_mem import annotate_memory_effects
from loopopt.analysis_profile import annotate_profile
from vector_legality import analyze_legality_function
from vector_capability_db import LANE_CAPS


class VectorProfitability:
    __slots__ = ('legality', 'lanes', 'trip', 'scalar_body_ops',
                 'instruction_reduction', 'bundle_reduction', 'throughput_gain',
                 'vector_utilization', 'remainder_cost', 'profitable', 'note')

    def __init__(self, legality):
        self.legality = legality
        self.lanes = legality.lanes
        self.trip = legality.kernel.trip
        self.scalar_body_ops = 0
        self.instruction_reduction = 0.0
        self.bundle_reduction = 0.0
        self.throughput_gain = 0.0
        self.vector_utilization = 0.0
        self.remainder_cost = 0
        self.profitable = False
        self.note = None

    def __repr__(self):
        if not self.legality.legal:
            return f"Profit(illegal: {self.legality.reason})"
        return (f"Profit({self.legality.kernel.kind} x{self.lanes} lanes: "
                f"~{self.throughput_gain:.1f}x, instr -{self.instruction_reduction:.0%}, "
                f"util {self.vector_utilization:.0%}, remainder {self.remainder_cost})")


def estimate(legality, desc=None):
    """Profitability estimate for one legal kernel. Illegal kernels return an
    estimate flagged not-profitable."""
    p = VectorProfitability(legality)
    if not legality.legal:
        p.note = 'not-legal'
        return p
    lanes = max(1, legality.lanes)
    trip = legality.kernel.trip or 0
    p.scalar_body_ops = getattr(desc, 'body_inst_count', 0) if desc else 0

    # dynamic instruction reduction: the loop runs ceil(trip/lanes) vector
    # iterations instead of `trip` scalar ones -> ~ (1 - 1/lanes) of the body ops.
    vec_iters = -(-trip // lanes) if trip else 0             # ceil
    if trip:
        p.instruction_reduction = max(0.0, 1.0 - vec_iters / trip)
    # bundle reduction tracks instruction reduction (denser vector bundles)
    p.bundle_reduction = p.instruction_reduction
    # throughput: up to `lanes` elements per op, but bounded by how many vector
    # ops the 8 issue lanes can sustain -- for a single reduction chain, ~lanes.
    p.throughput_gain = float(lanes)
    # vector utilization: one vector op occupies one issue lane of the 8.
    p.vector_utilization = 1.0 / LANE_CAPS['total'] if lanes else 0.0
    # more meaningful: fraction of the 64-bit datapath the lanes fill (always 1.0
    # for a full-width vector op) -- report lane fill instead.
    p.vector_utilization = 1.0                                  # full-width packed op
    p.remainder_cost = (trip % lanes) if trip else 0

    # profitable heuristic: at least 2x throughput and a non-trivial trip so the
    # remainder does not dominate.
    p.profitable = (lanes >= 2 and trip >= 2 * lanes)
    if not p.profitable:
        p.note = 'trip-too-small-for-lanes' if trip else 'unknown-trip'
    return p


def analyze_profitability_function(instrs, lo, hi):
    """Legality + profitability for every innermost loop in a function."""
    legs = analyze_legality_function(instrs, lo, hi)
    # attach M3 profile (body op count) via descriptors keyed by header
    descs = discover_function(instrs, lo, hi)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    annotate_profile(descs)
    by_header = {d.header: d for d in descs}
    out = []
    for leg in legs:
        out.append(estimate(leg, by_header.get(leg.kernel.header)))
    return out


def analyze_profitability_module(instrs):
    out = []
    for (lo, hi) in func_slices(instrs):
        out.extend(analyze_profitability_function(instrs, lo, hi))
    return out

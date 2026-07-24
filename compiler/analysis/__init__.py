"""
compiler/analysis -- reusable IR analysis framework.

Analyses live here as small, independent modules that optimization passes
construct and query instead of rebuilding their own bespoke analysis. Each is
scoped to a single function slice (temp names restart per function; see
ir_utils). The package grows by ADDITION -- future milestones add CFG,
DominatorTree, LoopInfo, Liveness, AliasInfo as new modules consuming the same
ir_utils primitives, without rewriting existing analyses.

Milestone 1 provides:
    DefUse -- def sites, use sites, and the single-definition map.
Milestone 4 adds (analysis only; consumed by no optimization yet):
    build_cfg, reverse_post_order, CFG   -- control flow graph
    compute_dominators, Dominators       -- dominator tree / dominance queries
    build_loop_info, LoopInfo            -- natural loops / nesting
    compute_liveness, Liveness           -- live-in / live-out sets
"""

from .defuse import DefUse
from .cfg import build_cfg, reverse_post_order, CFG, Block
from .dominators import compute_dominators, Dominators
from .loopinfo import build_loop_info, LoopInfo, Loop
from .liveness import compute_liveness, Liveness

__all__ = [
    "DefUse",
    "build_cfg", "reverse_post_order", "CFG", "Block",
    "compute_dominators", "Dominators",
    "build_loop_info", "LoopInfo", "Loop",
    "compute_liveness", "Liveness",
]

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
"""

from .defuse import DefUse

__all__ = ["DefUse"]

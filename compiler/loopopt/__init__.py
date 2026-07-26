"""
compiler/loopopt -- Loop Optimization Framework.

The permanent, reusable substrate that every loop optimization consumes. It
grows by ADDITION, milestone by milestone, on top of compiler/analysis (CFG,
Dominators, LoopInfo, Liveness, DefUse) -- which it REUSES and never duplicates.

Milestone M0 (this commit) -- analysis-only, no IR mutation:
    discover / discover_function    -- LoopDiscovery: analyses -> LoopDescriptors
    LoopDescriptor                  -- central per-loop data structure
                                       (identity / structure / nesting fields)
    LoopVerifier, VerifyResult      -- observe-only structural verification

Milestones landed so far ADD (not redesign): InductionVars (M1), MemEffects
(M2), Profile (M3), the LoopCanonicalizer + CFG-diff developer tool (M4 -- the
first IR-mutating stage; structural normalization only), and the LoopTransform
framework (M5 -- the generic transaction/rebuild/verify/rollback substrate every
future transform runs through; no optimization logic, no passes), and Loop
Rotation (M6 -- the FIRST concrete transform, a LoopTransform subclass that runs
entirely through the M5 framework; while -> guarded do-while), and the legality
framework (M7 -- shared fact-only legality predicates that every transform
composes instead of re-implementing; LoopRotation now consumes it). Later
milestones ADD the migration of LICM/IVSR/loop_reg onto this infrastructure. M8
lands the first of those migrations -- LoopLICM, a LoopTransform reproducing
licm2.py's conservative loop-invariant code motion instruction-for-instruction on
the shared substrate (its only framework addition is MutationTransaction.move()).
M9 lands the second -- LoopIVSR, reproducing ivsr.py's induction-variable /
pointer strength reduction instruction-for-instruction (framework addition:
MutationTransaction.replace_span(); it reuses ivsr's pure planner verbatim).
"""

from .descriptor import (LoopDescriptor,
                         TOP_TESTED, BOTTOM_TESTED, IRREGULAR)
from .discovery import discover, discover_function
from .verify import LoopVerifier, VerifyResult
from .analysis_iv import (analyze_induction_variables, annotate_induction_vars,
                          BasicIV, DerivedIV, TripCount,
                          IV_FORM_MEMORY, IV_FORM_REGISTER)
from .analysis_mem import (analyze_memory_effects, annotate_memory_effects,
                           MemAccess, CallSite, AliasSummary)
from .analysis_profile import (analyze_profile, annotate_profile,
                               profile_from_records)
from .cfgdiff import (diff_cfg, diff_loop, CFGDiff, LoopDiff)
from .canonicalize import (LoopCanonicalizer, CanonReport, canonicalize)
from .transform import (LoopTransform, LoopTransformDriver, MutationTransaction,
                        TransformResult, TransformStats, PassRegistry)
from .rotate import LoopRotation, RotationReport, rotate_module
from .legality import (LegalityFact, evaluate, PREDICATES,
                       has_labeled_header, is_top_tested, is_bottom_tested,
                       has_unique_preheader, has_single_latch, has_explicit_backedge,
                       has_dedicated_exits, has_side_effect_free_header,
                       header_has_single_exit_test, guard_inputs_loop_independent,
                       has_clean_iv, memory_safe, profile_suitable)
from .loop_licm import LoopLICM, LICMReport, licm_module
from .loop_ivsr import LoopIVSR, IVSRReport, ivsr_module
from .loop_unroll import (LoopUnroll, UnrollReport, UnrollLegality,
                          UnrollProfitability, UnrollSurveyReport,
                          analyze_module as unroll_analyze_module,
                          drive_noop as unroll_drive_noop)
from .depgraph import (DependenceGraph, DepNode, DepEdge,
                       build_dependence_graph, build_function_graphs,
                       RAW, WAR, WAW, MEM_RAW, MEM_WAR, MEM_WAW, CONTROL,
                       REGISTER_KINDS, MEMORY_KINDS)
from .depgraph_disambig import (MemoryDisambiguator, Verdict,
                                build_disambiguated_function_graphs,
                                disambiguate_function)
from .schedule import (schedule_module, schedule_function,
                       schedule_function_order, ScheduleStats, SchedPolicy)
from .modulo import (analyze_module as modulo_analyze_module,
                     analyze_function as modulo_analyze_function,
                     build_kernel, min_ii, rec_mii, res_mii,
                     modulo_schedule, verify_schedule, pipeline_module,
                     KernelModel, ModuloSchedule, ModuloStats, PipelineResult)
from .loop_promote import (promote_module, promote_function,
                           PromoteStats, PromotionReport)
from .pipeline_regaware import (pipeline_regaware_module,
                                pipeline_regaware_function,
                                LoopRecurrence, canonical_recurrences,
                                realize_register_pipeline,
                                RegAwareStats, RegAwareReport)
from .pipeline_mve import (pipeline_mve_module, pipeline_mve_function,
                           realize_mve_kernel, MVEStats, MVEReport)
from .oracle_ilp import (analyze_loop as oracle_analyze_loop,
                         analyze_function as oracle_analyze_function,
                         analyze_module as oracle_analyze_module, LoopILP)
from .oracle_report import report_module as oracle_report_module, format_loop, summarize

__all__ = [
    "LoopDescriptor", "TOP_TESTED", "BOTTOM_TESTED", "IRREGULAR",
    "discover", "discover_function",
    "LoopVerifier", "VerifyResult",
    # M1 -- InductionVars analysis
    "analyze_induction_variables", "annotate_induction_vars",
    "BasicIV", "DerivedIV", "TripCount",
    "IV_FORM_MEMORY", "IV_FORM_REGISTER",
    # M2 -- MemEffects analysis
    "analyze_memory_effects", "annotate_memory_effects",
    "MemAccess", "CallSite", "AliasSummary",
    # M3 -- Profile analysis
    "analyze_profile", "annotate_profile", "profile_from_records",
    # M4 -- CFG-diff developer tool + LoopCanonicalizer
    "diff_cfg", "diff_loop", "CFGDiff", "LoopDiff",
    "LoopCanonicalizer", "CanonReport", "canonicalize",
    # M5 -- LoopTransform framework (infrastructure only; no passes)
    "LoopTransform", "LoopTransformDriver", "MutationTransaction",
    "TransformResult", "TransformStats", "PassRegistry",
    # M6 -- Loop Rotation (first concrete transform; runs through the framework)
    "LoopRotation", "RotationReport", "rotate_module",
    # M7 -- Legality framework (shared fact-only predicates; no transforms)
    "LegalityFact", "evaluate", "PREDICATES",
    "has_labeled_header", "is_top_tested", "is_bottom_tested",
    "has_unique_preheader", "has_single_latch", "has_explicit_backedge",
    "has_dedicated_exits", "has_side_effect_free_header",
    "header_has_single_exit_test", "guard_inputs_loop_independent",
    "has_clean_iv", "memory_safe", "profile_suitable",
    # M8 -- LICM migrated onto the framework (behaviourally equal to licm2.py)
    "LoopLICM", "LICMReport", "licm_module",
    # M9 -- IVSR migrated onto the framework (behaviourally equal to ivsr.py)
    "LoopIVSR", "IVSRReport", "ivsr_module",
    # R1.1 -- LoopUnroll infrastructure (analysis-only; no unrolling yet)
    "LoopUnroll", "UnrollReport", "UnrollLegality", "UnrollProfitability",
    "UnrollSurveyReport", "unroll_analyze_module", "unroll_drive_noop",
    # R2.1 -- DependenceGraph infrastructure (analysis-only; reusable dep-graph)
    "DependenceGraph", "DepNode", "DepEdge",
    "build_dependence_graph", "build_function_graphs",
    "RAW", "WAR", "WAW", "MEM_RAW", "MEM_WAR", "MEM_WAW", "CONTROL",
    "REGISTER_KINDS", "MEMORY_KINDS",
    # R2.2 -- memory dependence disambiguation (analysis-only; refines mem edges)
    "MemoryDisambiguator", "Verdict",
    "build_disambiguated_function_graphs", "disambiguate_function",
    # R2.3 -- dependence-aware IR scheduler (first graph consumer; reorders IR)
    "schedule_module", "schedule_function", "schedule_function_order",
    "ScheduleStats",
    # R2.4 -- scheduler quality (latency / pressure / bundle-aware + statistics)
    "SchedPolicy",
    # R2.5 -- software pipelining / modulo scheduling (RecMII/ResMII/MII + gen)
    "modulo_analyze_module", "modulo_analyze_function",
    "build_kernel", "min_ii", "rec_mii", "res_mii",
    "modulo_schedule", "verify_schedule", "pipeline_module",
    "KernelModel", "ModuloSchedule", "ModuloStats", "PipelineResult",
    # R2.6 -- loop register promotion (memory recurrence -> register recurrence)
    "promote_module", "promote_function", "PromoteStats", "PromotionReport",
    # R2.7 -- register-aware software pipelining (integrates R2.6 into R2.5)
    "pipeline_regaware_module", "pipeline_regaware_function",
    "LoopRecurrence", "canonical_recurrences", "realize_register_pipeline",
    "RegAwareStats", "RegAwareReport",
    # R2.8 -- modulo variable expansion + compact rotating-kernel realisation
    "pipeline_mve_module", "pipeline_mve_function", "realize_mve_kernel",
    "MVEStats", "MVEReport",
    # R3.0 -- oracle ILP bound analyzer (analysis only, no codegen change)
    "oracle_analyze_loop", "oracle_analyze_function", "oracle_analyze_module",
    "LoopILP", "oracle_report_module", "format_loop", "summarize",
]

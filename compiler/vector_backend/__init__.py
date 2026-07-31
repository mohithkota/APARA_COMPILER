"""
vector_backend -- the APARA Vector Backend Analysis Framework (Milestone R6.1).

ANALYSIS ONLY.  Nothing in this package mutates IR, changes a scheduling or
bundling decision, emits an instruction, or is imported by any production path.
It exists to answer ONE question with measured evidence:

    for kernels that are ALREADY vectorized, why is every empty vector issue
    slot empty, and which single compiler optimization would fill the most?

The framework operates AFTER vectorization, on two levels that must both be
measured because neither alone explains the result:

    vector IR (post-lowering)   -- dependence structure, critical path,
                                   available parallelism, ready-queue size.
                                   This is the ILP that EXISTS.

    final mcode bundles         -- issue-slot occupancy, per-slot instruction
                                   mix, empty-slot cause, register lifetimes.
                                   This is the ILP that is DELIVERED.

Modules
-------
    latency.py           latency + resource model (issue width, lanes, op
                         classes).  Reuses the frozen R2.4 model; adds no new
                         hardware claim.
    dependency_graph.py  vector-IR dependence graph (RAW/WAR/WAW/memory/
                         loop-carried, latency-weighted) + critical path,
                         parallelism, ready-queue simulation.
    occupancy.py         bundle occupancy over the REAL production bundler's
                         output, with a cause assigned to every empty slot.
    ilp_analysis.py      the driver: per-kernel reports, dynamic weighting,
                         what-if experiments, and the R6_1 markdown report.

Every module is import-safe from `compiler/` (the package adds its parent to
sys.path exactly as loopopt/ and evaluation/ already do).
"""

__all__ = ['latency', 'dependency_graph', 'occupancy', 'ilp_analysis']

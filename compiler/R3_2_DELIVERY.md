# R3.2 Delivery Report — Superblock / Trace Scheduling

**Milestone:** R3.2 (enlarge scheduling regions beyond basic blocks so the
existing scheduler can pack across them; trace scheduling *without* speculation or
duplication).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-27

> Not a new scheduler — a region-formation pass. The CFG, LoopInfo, Dominators,
> DependenceGraph, disambiguator, the existing scheduler (`loopopt/schedule.py`),
> the bundler, the oracle, and the existing validator/rollback are all CONSUMED
> unmodified. No speculation, no instruction duplication. `APARA_NO_SUPERBLOCK=1`
> disables it; any failure keeps the proven R3.1 output byte-for-byte.

---

## 1. Files added
| File | Purpose |
|---|---|
| `superblock.py` | Phase 1-2 region formation: CFG-based merging of single-entry/single-exit straight-line block chains (drop dead labels + redundant `goto`s). `RegionStats`, `form_superblocks`, `superblock_module`. |
| `trace_scheduler.py` | Phase 3-5 driver: run the existing scheduler over the enlarged regions, oracle gate, spill/bundle-safe acceptance, production integration (`apply_superblock_scheduling`, `superblock_schedule`, `format_superblock`). |
| `_r3_2_test.py` | Unit suite (23 checks): region semantics, no-duplication, region growth, behaviour preservation, accept/rollback, oracle gate, kill-switch, determinism. |
| `superblock_corpus.py` | Corpus evaluation vs R3.1. |
| `R3_2_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `compiler.py` | `compile_c_to_mcode()`: a guarded superblock block after the R3.1 SWP block, tracking `_prod_ir`; regenerates `body` only if accepted and spill-free. `APARA_NO_SUPERBLOCK` kill-switch. ~20 lines, additive. |

No scheduler, bundler, register allocator, software pipelining, oracle, or
dependence-analysis code is modified.

## 3. What "superblock without speculation/duplication" means here
The safe core of trace scheduling: merge a chain of basic blocks that form a
**single-entry / single-exit straight-line region** into one region. A block `B`
is merged into its layout-predecessor `P` iff `B` has exactly one predecessor
(`P`), `P` has exactly one successor (`B`), `B` is adjacent to `P`, and the `P→B`
edge is a fall-through or a redundant `goto B`. Removing that boundary (dropping
`B`'s now-single-predecessor label and any redundant `goto`) is a **pure no-op** —
control already flowed `P→B` unconditionally, so no instruction changes the path
it executes on. Nothing is hoisted above a branch (no speculation) and nothing is
copied onto an off-trace path (no duplication). Multi-entry regions, conditional
side exits, and irreducible control flow are simply left unmerged.

**Prime target:** a counted loop whose body and IV-increment the front end split
with a **dead label** (nothing branches to it — the back-edge targets the header).
Merging them lets the existing scheduler overlap the loop body with the increment,
and lets the bundler pack across what was a hard label barrier.

## 4. Phases
1-2. **Region formation / trace** — `superblock.py` builds the CFG and merges the
maximal single-pred/single-succ adjacent chains (the trace follows fall-through /
back-edge structure; no heuristic speculation is needed because side-exit blocks
are never merged).
3. **Cross-block scheduling** — `schedule_module` (the frozen R2.4 scheduler) runs
over the enlarged blocks. Dependence, resource, and memory-ordering legality are
its existing guarantees; it reorders within the merged region, never across the
region's single terminating branch.
4. **Compensation** — none needed: control flow is preserved exactly, so there is
no off-trace path to compensate. On any legality/scheduling failure the scheduler
rolls that function back to its original order (its existing behaviour).
5. **Oracle integration** — attempted only when the R3.0 oracle reports scheduling
headroom (a loop whose theoretical IPB exceeds achieved by ≥ threshold, default
0.5, `APARA_SUPERBLOCK_THRESHOLD`).

## 5. Validation & rollback (all reused)
- **Region formation** is semantics-preserving by construction and verified: the
  differential oracle confirms identical behaviour, and the non-control instruction
  multiset is unchanged (no duplication).
- **The scheduler** self-validates each function with the differential oracle and
  rolls back on mismatch (its existing R2.3/R2.4 mechanism).
- **Production acceptance** requires the whole program to still compile with **zero
  spills** AND the bundle count **not to increase**; otherwise the proven R3.1
  `body` is kept. The block is wrapped so any error reverts to R3.1.

## 6. Corpus results (124 programs, R3.1 → R3.2)
```
  oracle attempted (scheduling headroom) : 33
  superblock accepted (programs)         : 33
  rollbacks (spill/bundle/scheduler)      : 1
  behaviour mismatches vs R3.1           : 0    (MUST be 0)
  avg scheduling region                  : 7.18 -> 8.36 blocks   (+17% larger)
  superblock pass time                   : 5.0 ms / program

  Production compiler   R3.1 (SWP) -> R3.2 (SWP + superblock)
    static instructions : 11678 -> 11686   (+8, flat)
    bundles             : 6075  -> 5916    (-159)
    IPB                 : 1.922 -> 1.975    (+2.8%)
```
Cumulative production IPB across the integration line: **baseline 1.861 → R3.1
1.922 → R3.2 1.975**.

## 7. Success criteria — met
1. **Schedules profitable superblocks automatically** — 33 programs, default-on,
   oracle-gated.
2. **Correctness unchanged** — 0 mismatches vs R3.1; region formation is a proven
   no-op; the scheduler's own differential guards each function.
3. **Rollback reliable** — accepted only when spill-safe and non-bundle-increasing;
   1 rollback; guarded so any failure reverts to R3.1.
4. **Average scheduling region increases** — +17% (7.18 → 8.36 blocks).
5. **Production IPB improves beyond R3.1** — 1.922 → 1.975 (+2.8%), bundles −159
   at flat static size (denser packing).
6. **No significant compile-time regression** — 5.0 ms/program.

Frozen suites unaffected: `pipeline_crosscheck` 124/124 identical; R2.5–R3.1 and
R3.2 unit suites all pass.

## 8. Test summary
```
_r3_2_test.py ........................ ALL R3.2 UNIT TESTS PASS   (23/23 checks)
_r2_5 .. _r2_8, _r3_0, _r3_1 ......... PASS (unchanged)
pipeline_crosscheck.py ............... 124/124 identical, 0 rollbacks
```

## 9. Honest notes / limitations
- **Correctness is validated at the R3.1 baseline, not against `ir0`.** The
  ir_interp oracle is a simplified model; on heavily-optimized division / sub-word
  / bit-manipulation code it already diverges from `ir0` at the production-optimizer
  stage (a *pre-existing* oracle gap, present in R3.1 and earlier, unrelated to
  region enlargement). R3.2 is therefore validated to preserve its input (the R3.1
  output) exactly — 0 mismatches — which is the correct scope for this pass. As with
  R3.1, machine-level assurance rests on the frozen scheduler's dependence-legality
  and a recommended simulator pass before deployment; `APARA_NO_SUPERBLOCK=1`
  reverts instantly.
- **Scope is straight-line region enlargement.** The no-speculation / no-duplication
  mandate means only single-entry/single-exit chains are merged. Conditional side
  exits (real trace scheduling with speculation, or if-conversion to remove them)
  are future work; those regions are left as separate blocks.
- The superblock pass also lets the existing local scheduler run over functions it
  wasn't previously applied to in production; the bundle-non-increase gate ensures
  this never regresses, and it only helps.
- Not done (by mandate): no redesign of the scheduler, bundler, register allocator,
  software pipelining, or oracle.

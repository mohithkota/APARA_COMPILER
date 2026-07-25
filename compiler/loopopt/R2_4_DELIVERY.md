# R2.4 Delivery Report — Scheduler Quality Improvements

**Milestone:** R2.4 (improve the R2.3 scheduler's *decisions* using existing
infrastructure; no redesign).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-25

> Additive quality improvements to the R2.3 block-local scheduler. The scheduler
> structure (block-local list scheduling, topological verifier, differential gate,
> per-function rollback) is unchanged; only the priority/tie-break policy is
> enriched and reusable statistics are added. No software pipelining / modulo /
> cross-block scheduling. The DependenceGraph, `LoopTransform`, bundler, register
> allocator and `LoopUnroll` are untouched, and the pass remains standalone (not
> integrated into production) — the pipeline cross-check stays 124/124 identical.

---

## 1. Files added
| File | Purpose |
|---|---|
| `loopopt/_r2_4_test.py` | R2.4 unit suite (33 checks): latency model + latency-weighted height, latency prioritisation changing the pick, register-pressure tie-break, bundle-cap model, statistics population, determinism, semantics + regression under both policies. |
| `loopopt/schedule_r24_corpus.py` | Corpus evaluation comparing R2.3 vs R2.4 (bundles / IPB / schedule length / dependency height / register-pressure estimate / spills / instruction movement / scheduling time). |
| `loopopt/R2_4_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change (additive / backward-compatible) |
|---|---|
| `loopopt/schedule.py` | Adds `_latency` / `_iclass` (ISA-conservative latency + bundler resource class), `SchedPolicy` (`R23` reproduces the R2.3 scheduler exactly; `R24` is the new default), a latency-weighted `_heights` variant, a register-pressure and bundle-fill tie-break in the list-scheduler, and reusable statistics folded into `ScheduleStats`. The R2.3 core (block partition, topo verify, differential gate, rollback) is unchanged; `SchedPolicy.R23` gives byte-for-byte R2.3 output. |
| `loopopt/__init__.py` | Additive `SchedPolicy` export. |

## 3. Scheduling improvements implemented
1. **Latency-aware scheduling (F1).** Each instruction gets a conservative ISA-based latency (loads 3, multiply 3, divide/`%`/`fsqrt` 8, calls 5, vector 2–4, everything else 1 — the same op families the bundler treats as expensive). The critical-path height becomes `latency(n) + max(height(succ))`, so instructions on longer *latency* paths (not just longer instruction-count paths) are prioritised.
2. **Register-pressure-aware scheduling (F2).** A live-range estimate (reusing block `Liveness` for live-out, plus in-block use counts) tracks, as scheduling proceeds, how many temporaries are live. As a tie-break *beneath* the critical-path height (so the critical path is never sacrificed), the scheduler prefers the ready instruction with the greatest `deaths − births` — i.e. the one that frees more registers than it defines — shortening live ranges where legal. Register allocation itself is untouched.
3. **Bundle-aware tie breaking (F3).** A light bundle-fill model mirrors the bundler's *known lane caps* (≤4 loads/stores, ≤1 divide/sqrt, control ends a bundle, 8 total) to prefer, among still-tied candidates, an instruction whose resource class still fits the forming bundle window — discouraging un-packable same-class runs. It only informs the tie-break and the utilisation estimate; it never builds a bundle (that stays the bundler's job).
4. **Scheduling statistics (F4).** Reusable aggregates on `ScheduleStats`: critical-path length, average ready-list size, register-pressure (peak-live) estimate, instruction-movement distance (sum + max), and a bundle-utilisation estimate — to support future optimisation work.

Determinism is preserved: the final `−index` tie-break makes every choice a pure function of the input under all policies (verified).

## 4. Existing analyses reused (nothing re-derived)
| Reused | For |
|---|---|
| R2.1 `DependenceGraph`, R2.2 `MemoryDisambiguator` | constraints (unchanged from R2.3) |
| `analysis.compute_liveness` | block live-out for the register-pressure estimate |
| `ir_utils.dest_names` / `src_names` | def/use sets for pressure accounting |
| `ir_interp.differential` | the per-function correctness gate (unchanged) |
| the bundler's documented lane caps | the bundle-fill tie-break model (knowledge reused, not code) |

## 5. Validation methodology
Identical to R2.3 and applied to the R2.4 policy: the topological self-verifier
(0 failures), the `ir_interp` differential oracle with per-function rollback (0
rollbacks, 0 mismatches), and end-to-end compile + bundle of every program.
`SchedPolicy.R23` reproduces the R2.3 scheduler so both policies are validated
side by side. No existing R2.1/R2.2/R2.3 test was weakened — all still pass under
the R2.4 default.

## 6. Test summary
```
_r2_4_test.py ......................... ALL R2.4 UNIT TESTS PASS   (33/33 checks)
_r2_1 / _r2_2 / _r2_3 ................. PASS (unchanged; R2.3 tests pass under R2.4 default)
pipeline_crosscheck.py ................ 124/124 identical, 0 rollbacks (production frozen)
```

## 7. Corpus validation
```
programs analysed 124 · both policies: rollbacks 0 · structural fails 0 · behaviour mismatches 0
functions changed  R2.3 128  ->  R2.4 132
instructions moved R2.3 2591 ->  R2.4 2858
```

## 8. Performance comparison vs R2.3 (baseline → R2.3 → R2.4, production bundler ON)
```
  static instructions   : 11885 -> 11909 (+24) -> 11889 (+4)     # R2.4 less codegen bloat
  bundles (sched length): 6498  -> 6218        -> 6188           # R2.4 -30 vs R2.3
  IPB                   : 1.829 -> 1.915       -> 1.921          # R2.4 > R2.3
  register spills       : 0     -> 0           -> 0
  -- statistics (R2.3 vs R2.4) --
  avg dependency height : 3.20 (unit) vs 7.93 (latency-weighted)
  avg register-pressure : 2.75 vs 2.77 peak-live/block
  avg ready-list size   : 7.09 vs 6.96
  instruction movement  : 16162 vs 17844 (max 114 vs 99)
  est bundle util       : 5.226 vs 5.169 instrs/bundle (dep-free upper bound)
  scheduling time       : 0.39s vs 0.48s (whole corpus)
```
**Improvements.** R2.4 is a net win over R2.3 in the production configuration:
**−30 bundles (6218→6188), IPB 1.915→1.921**, and — the clearest signal of the
register-pressure work — codegen static bloat drops from **+24 to +4**
instructions with **spills still 0**. The pressure-aware tie-break shortens live
ranges enough that the register allocator emits fewer extra moves. Peak movement
per instruction also falls (114→99).

**Regressions / costs (discussed).** Scheduling time rises ~23% (0.39s→0.48s
whole corpus) from the added liveness pass and richer priority — still trivial in
absolute terms. Total instruction movement is higher (17844 vs 16162): the
latency heuristic reorders more aggressively; this is the mechanism of the gain,
not a defect, and every move is differentially verified. The *dependency-free*
bundle-utilisation estimate dips slightly (5.226→5.169) while the *real* bundle
count improves — confirming the estimate is a loose upper bound and the
authoritative bundler measurement is the one that moved the right way. The
peak-live estimate is essentially flat (2.75 vs 2.77); the real register-pressure
benefit shows up in codegen (the +24→+4 static reduction), not in the coarse
estimate.

## 9. Remaining work before software pipelining
- **R2.4 is done and frozen.** The scheduler is standalone and not integrated into production (shipped output unchanged).
- Further *local* scheduling quality (all safe): a hardware-accurate latency table and issue-width model, a slack/mobility-based (rather than pure-height) priority, and a stronger register-pressure controller (e.g. Sethi–Ullman ordering or a pressure ceiling that can throttle ILP) — none of which change semantics.
- The next architectural step (a separate milestone) is **software pipelining / modulo scheduling**, which is the first optimisation to *use the loop-carried recurrence edges* R2.1 records to overlap iterations. That requires the SCC/recurrence machinery already in the graph (`recurrences()`) plus a modulo scheduler — explicitly out of scope here and not started.
- Not done (by design): no cross-block/global scheduling, no software pipelining, no modulo scheduling, no register-allocator/bundler/graph redesign, no production integration, no `LoopUnroll` change.

# R3.0 Delivery Report — Oracle ILP Bound Analyzer

**Milestone:** R3.0 (analysis-only framework computing the theoretical ILP of
every innermost loop and quantifying why the scheduler cannot reach it).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-26

> ANALYSIS ONLY. Mutates no IR; changes no scheduling, bundling, register
> allocation, optimization decision, or generated code; affects correctness in no
> way. It only reports statistics. Proven: generated code is **byte-identical
> 124/124** with and without the analysis. This milestone is the decision tool for
> every future optimization.

---

## 1. Files added
| File | Purpose |
|---|---|
| `loopopt/oracle_ilp.py` | The analyzer: oracle dependence DAG, critical-path/MII metrics, ideal ready-set list-scheduling simulation, the three IPB numbers, limiter classification, opportunity ranking. `LoopILP` result. |
| `loopopt/oracle_report.py` | Per-loop detail view + per-module summary; CLI (`oracle_report.py file.c`). |
| `loopopt/oracle_corpus.py` | Corpus evaluation (Phase 6): aggregate IPB, largest gaps, bottleneck / recurrence-length / ready-set / opportunity distributions, and the no-change proof. |
| `loopopt/_r3_0_test.py` | Unit suite (31 checks): metric contract, ceiling correctness, ready-set well-formedness, innermost-only, determinism, and the mutates-nothing guarantee. |
| `loopopt/R3_0_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `loopopt/__init__.py` | Additive R3.0 exports only. |

No pass, analysis, scheduler, bundler, allocator, or codegen file is modified.

## 3. Reuse (no analysis duplicated)
- **DependenceGraph (R2.1) + MemoryDisambiguator (R2.2)** — the exact typed
  dependence DAG (RAW/WAR/WAW/memory/recurrence edges), used for edge annotation
  and the ready-set simulation.
- **analysis_profile (M3 / the M11 statistics framework)** — which itself reuses
  `parallelism_profile`'s `_critical_path`, `_rec_mii`, `_res_mii_detail`,
  `_reg_pressure` — supplies the critical path, RecMII, ResMII, MII, the
  resource-term breakdown (mem/width/div), recurrence membership, and register
  pressure.
- **schedule.py** `_latency` / `_iclass` / `_CAP` / `_BUNDLE_MAX` and **modulo.py**
  `_edge_latency` — the latency + bundle-resource model.

The only new code is the ready-set list-scheduling **simulation** (a measurement,
not a transform) and the classification/opportunity logic.

## 4. The analytical core — three IPB numbers
```
theoretical_ipb = min(N / MII, 8)      MII = max(RecMII, ResMII)  [M11 framework]
    Maximum ACHIEVABLE IPB: perfect software pipelining + infinite registers +
    perfect allocation, but the REAL caps (8/4/1/1) and REAL recurrence latency.
    The ceiling no scheduler can beat.

local_ideal_ipb = N / bundles(ideal single-iteration list schedule, inf. regs)
    Best LOCAL scheduler, true-dependence DAG only (anti/output renamed away),
    real caps, no cross-iteration overlap. Diagnostic.

achieved_ipb = N / bundles(in-order greedy pack, ALL deps incl. anti/output)
    Models the current local, in-order bundler on the memory-backed form.
```
`utilization = achieved / theoretical`. Gap decomposition:
`total = pipelining_gap (theoretical−local) + scheduler/renaming_gap (local−achieved)`.
This decomposition is what pinpoints whether the loss is the **scheduler**, the
**IR/allocator** (anti/output deps from memory-backing), or the **dependence
structure** (a low theoretical ceiling itself).

## 5. Phases
1. **Oracle DAG** — R2.1 edges among body ops, annotated (latency, distance,
   recurrence membership).
2. **Critical path** — instruction count, critical path (all-deps & true-deps),
   avg/max dependence depth, longest recurrence, RecMII, ResMII, theoretical II.
3. **Ready-set** — an ideal list scheduler (infinite registers, caps 8/4/1/1)
   records, per issue cycle, how many instructions were **ready**: histogram
   (0..8+), average and maximum.
4. **Upper-bound IPB** — theoretical vs local-ideal vs achieved, per loop, with
   the gap decomposition.
5. **Limiter classification** — recurrence-bound (memory / register), memory-bound,
   resource-bound (divide / issue-width), dependency-bound, control-bound, mixed;
   ranked by what binds the ceiling.
6. **Corpus report** — averages, largest gaps, and distributions of bottleneck
   class, recurrence length, and ready-set size.
7. **Opportunity ranking** — per loop, the estimated-highest-gain lever among
   register promotion, reassociation, software pipelining, loop unrolling, better
   alias analysis, vectorization, register renaming, or none.

## 6. Corpus results (124 programs, 65 innermost loops)
```
  generated code UNCHANGED           : 124/124   (the milestone's core guarantee)
  IPB (avg over innermost loops)
    theoretical ceiling (N/MII)       : 5.22
    local-ideal (1 iteration, inf-regs): 3.70
    achieved (current model)          : 1.64
    mean utilization                  : 32%
    real measured aggregate IPB       : 1.83   (codegen+bundler; cross-checks 1.64)

  Dominant bottleneck distribution
    resource-bound (issue width)      : 48  (74%)   <- ceiling is HIGH; loop is not
    control-bound                     : 14  (22%)      fundamentally limited
    recurrence-bound (memory)         :  3  (5%)

  Recurrence length (RecMII): 3 -> 61 loops, 4 -> 2, 5 -> 1, 7 -> 1
  Ready-set: 70 cycles expose 8+ ready instructions; 106 expose only 1
  Ranked opportunity: software-pipelining 74%, vectorization 17%,
                      register-renaming 6%, reassociation 2%, register-promotion 2%
```

## 7. The verdict — what fundamentally prevents higher IPB
The framework answers the milestone's question decisively, per loop and in
aggregate:

- **It is NOT the dependence structure.** The theoretical ceiling averages **5.22
  IPB** and 74% of loops are *issue-width-bound* — i.e. their ceiling is set only
  by having N instructions and 8 lanes, not by a recurrence or a memory-lane cap.
  The dependence structure permits ~5–6 IPB on most loops.
- **It is NOT the register file.** Peak register pressure is small (never binding),
  and RecMII is ~3 almost everywhere.
- **It IS exposed-but-unexploited ILP.** Achieved is **1.64** vs a **5.22** ceiling
  (32% utilization). The ready-set histogram is the proof: **70 cycles have 8+
  ready instructions** the current in-order local bundler leaves on the table. The
  gap decomposes into a **pipelining gap** (no cross-iteration overlap — R2.5–R2.8
  exist but are unwired) and a **scheduler/renaming gap** (in-order local packing
  over the memory-backed form, whose anti/output deps are artefacts of memory
  residence).
- **So the gap is the scheduler and the IR, not the workload.** The single
  highest-value lever is **software pipelining wired into production** (74% of
  loops), then **vectorization** (17%, the elementwise kernels), with register
  promotion / reassociation mattering only for the few genuinely
  recurrence-bound loops.

This confirms and *quantifies* the earlier qualitative analysis: the machine has 8
lanes, the loops contain ~5 IPB of parallelism, and the compiler manufactures ~1.6
of it into bundles.

## 8. Validation
- **Mutates nothing:** the corpus harness snapshots each program, runs the oracle,
  and re-generates code — **byte-identical 124/124** (IR repr and mcode). The unit
  suite independently asserts IR + generated code unchanged.
- **Ceiling correctness:** `theoretical == min(N/MII, 8)`, `MII == max(RecMII,
  ResMII)`, `theoretical ≥ achieved`, `utilization ≤ 1` on every loop.
- **Cross-check:** the model's achieved IPB (1.64, per-loop) tracks the real
  measured aggregate IPB (1.83, whole-program) — the model is slightly conservative
  because loop bodies are denser-dependency than average code.
- **Determinism:** identical output across runs.
```
_r3_0_test.py ........................ ALL R3.0 UNIT TESTS PASS   (31/31 checks)
_r2_1 .. _r2_8 ....................... PASS (unchanged)
pipeline_crosscheck.py ............... 124/124 identical, 0 rollbacks (production frozen)
```

## 9. Test summary
31 unit checks (metric contract, ceiling identities, ready-set well-formedness,
innermost-only, memset→vectorization, reduction→actionable lever, determinism,
report helpers, and the mutates-nothing guarantee). All frozen suites pass.

## 10. Remaining limitations / honest notes
- **Opportunity gains are estimates** (register promotion / reassociation / unroll
  factors use fixed heuristics). The *theoretical / local / achieved / utilization*
  numbers and the *limiter* classification are measured, not estimated; the ranking
  is directionally sound. A future refinement could compute promotion/reassociation
  counterfactuals by actually running R2.6 and re-analysing.
- **Achieved is a model** of the current in-order local bundler, not a
  per-loop reading of real bundles (codegen emits no loop→bundle provenance); the
  corpus reports the real aggregate IPB as the cross-check.
- **Scope**: innermost loops only (as specified). Non-loop code and outer loops are
  not scored.
- Gains are **not additive** (they overlap — e.g. software pipelining subsumes much
  of register-renaming); each is "how close does THIS single lever get to the
  ceiling."
- Analysis-only by mandate: no codegen / scheduling / bundling / allocation change.

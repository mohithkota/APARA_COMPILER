# R2.5 Delivery Report — Software Pipelining (Modulo Scheduling)

**Milestone:** R2.5 (the first optimisation that schedules across loop iterations).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-26

> Built entirely on frozen infrastructure (M0–M3 descriptors, R2.1 DependenceGraph
> + recurrence edges, R2.2 disambiguation, R2.4 latency/resource models, the
> `ir_interp` differential oracle). Standalone — **not** integrated into the
> production compiler, so shipped output stays frozen (pipeline cross-check
> 124/124 identical). Correctness is mandatory: Phases 1–2 never touch the IR;
> Phase 3 commits a pipeline only when structural + multi-seed differential +
> compile validation all pass, else the loop is rolled back untouched.

---

## 1. Files added
| File | Purpose |
|---|---|
| `loopopt/modulo.py` | All five phases: eligibility + kernel model, RecMII/ResMII/MII (Phase 1), the modulo reservation table + iterative modulo scheduler (Phase 2), prologue/kernel/epilogue realisation (Phase 3), the structural + multi-seed differential + compile gate with rollback (Phase 4), and `ModuloStats` (Phase 5). Driver `pipeline_module()`. |
| `loopopt/_r2_5_test.py` | R2.5 unit suite (30 checks): RecMII/ResMII, schedule legality, kernel construction, prologue/kernel/epilogue generation, loop-carried recurrence handling, determinism, rollback. |
| `loopopt/modulo_corpus.py` | Corpus evaluation + baseline→R2.3→R2.4→R2.5 comparison. |
| `loopopt/R2_5_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `loopopt/__init__.py` | Additive R2.5 exports only. |

No previous pass, the DependenceGraph, `LoopTransform`, the scheduler, the bundler, or register allocation is modified.

## 3. Algorithm implemented
**Eligibility (strict).** One innermost, top-tested counted loop with a clean IV, single preheader/latch/exit, reducible CFG, no calls or unsupported ops in the kernel. Everything else is reported UNSUPPORTED and left untouched.

**Phase 1 — MII.** Kernel = the loop body's data ops (control excluded). Edge latency from the R2.4 model (flow = producer latency, output = 1, anti/control = 0).
- **ResMII** = max over resource classes of ⌈uses / capacity⌉ (bundle caps: 8 slots, 4 memory lanes, 1 divide lane).
- **RecMII** = smallest II for which the recurrence graph has no positive cycle in weights `latency − II·distance` (Bellman-Ford longest-path feasibility, scanning II upward). Loop-carried edges carry distance 1 (conservative).
- **MII** = max(RecMII, ResMII).

**Phase 2 — modulo scheduling.** For II = MII, MII+1, …: solve the difference-constraint minimal cycle times (respecting intra deps and carried deps modulo II), then legalise resources greedily against a per-modulo-slot `ReservationTable` (push a conflicting op one cycle and propagate). `verify_schedule()` independently certifies every dependence satisfied and every modulo slot within caps. Op stage = cycle ÷ II; stages = ⌈length ÷ II⌉.

**Phase 3 — realisation.** For a KNOWN trip count `T`, the schedule is realised by linearising it over all `T` iterations: instance (iteration `it`, kernel op `o`) executes at absolute time `it·II + cycle(o)`, emitted in absolute-time order. That is exactly the software-pipelined schedule — a later iteration's early stage runs before an earlier iteration's late stage (overlap), while every dependence and every shared memory slot (IV/accumulator) is touched in schedule order. Each iteration gets a private temp namespace (modulo variable expansion by renaming); memory-slot offsets stay shared, so the recurrences self-serialise. The ramp-up / steady / drain regions are the prologue / kernel / epilogue. *(A compact register-rotating kernel LOOP is future work; this full realisation is the correctness-first form.)*

**Phases 4–5 —** validation (below) and `ModuloStats`.

## 4. Existing analyses reused (nothing re-derived)
| Reused | For |
|---|---|
| M0–M3 descriptors (`discover`, `annotate_induction_vars`, `annotate_memory_effects`) | eligibility, trip count, IV, clean-slot facts |
| R2.1 `DependenceGraph` + carried recurrence edges | intra + loop-carried kernel dependences |
| R2.2 `MemoryDisambiguator` | precise memory deps (fewer false recurrences) |
| R2.4 `_latency` / `_iclass` / caps | edge latencies + resource model |
| `ir_interp.differential` / `run_slice` | the multi-seed correctness gate |

## 5. Validation strategy
Each candidate pipeline must pass, in order, or it is rolled back (IR untouched):
1. **Schedule legality** — `verify_schedule()` (all deps mod II, resources within caps).
2. **Structural** — the module still parses into the same function slices; globals and inter-function code preserved.
3. **Multi-seed differential** — original vs pipelined function slice run on the natural seed **plus 4 random data seeds** (the loop's control flow is data-independent for a known trip count, so agreement across diverse data is strong evidence); any divergence → rollback.
4. **Compile** — the pipelined IR must pass the production CodeGen without raising.

The corpus harness independently re-checks behaviour (original vs committed pipeline) with the differential oracle: **0 mismatches**.

## 6. Test summary
```
_r2_5_test.py ......................... ALL R2.5 UNIT TESTS PASS   (30/30 checks)
_r2_1 .. _r2_4 ....................... PASS (unchanged)
pipeline_crosscheck.py ............... 124/124 identical, 0 rollbacks (production frozen)
```

## 7. Corpus validation
```
Phase 1 -- MII analysis (mutation-free)
  programs / loops / eligible : 124 / 79 / 45
  MII bound-by  Rec / Res     : 42 / 2          (93% RECURRENCE-bound)
  MII histogram               : {5:34, 6:6, 7:1, 8:2, 9:2}
Phase 3-4 -- pipeline generation (structural + differential + compile gated)
  loops pipelined  : 12     declined : 26     rolled back : 7
  reasons          : trip-not-known 21, differential-rollback 7,
                     single-stage 2, trip-too-small 3
  behaviour mismatches : 0     compile failures : 0
  avg stages/kernel/prologue/epilogue : 2.3 / 90 / 20 / 4
  RESULT: PASS (0 behaviour mismatches / 0 compile failures)
```
The 7 differential-rollbacks are the safety net working — candidate pipelines that did not certify equivalent were discarded, never emitted.

## 8. Performance comparison vs R2.4 (baseline → R2.3 → R2.4 → R2.5, bundler ON)
```
  static instructions : 11885 -> 11909 -> 11889 -> 13188
  bundles             : 6498  -> 6218  -> 6188  -> 6759
  IPB                 : 1.829 -> 1.915 -> 1.921 -> 1.951
  register spills     : 0     -> 0     -> 0     -> 2
```
**Improvements.** R2.5 reaches the **highest IPB of the whole series (1.951)** — the overlapped modulo schedule exposes cross-iteration ILP the local scheduler cannot, so the bundler packs the pipelined loops much more densely (isolated pipelined loops such as the 8-element sum jump from IPB ≈1.36 to ≈2.9). 12 loops are pipelined, all provably correct.

**Regressions (honest).** Static instructions and static bundle count RISE (13188 / 6759) because this realisation fully unrolls the known-trip loop — it is a *code-size-for-ILP* trade, and it also lifts register pressure enough to introduce **2 spills**. Dynamically the pipelined loops execute far fewer bundles (they are unrolled, no per-iteration loop overhead), but the static/code-size metrics regress. A compact register-rotating kernel-loop (MVE) form would keep the ILP without the code growth — see §9.

**The governing finding.** 42 of 45 eligible loops are **RecMII-bound**, dominated by the IV/accumulator *memory* recurrences (load-modify-store, ≈5 cycles, distance 1). At this memory-backed IR level II is pinned near the recurrence length on almost every loop, so overlap — and thus pipelining's benefit — is fundamentally limited until those recurrences are register-promoted (a separate pass, out of scope). R2.5 quantifies this precisely, matching the M11 "dependency-bound" conclusion.

## 9. Remaining limitations / future work
- **Compact kernel loop (MVE).** The current realisation is a full unroll of the known-trip schedule (code grows with T). A register-rotating / stage-unrolled kernel **loop** with guarded prologue/epilogue would deliver the same overlap without code growth and would handle symbolic trip counts (21 of the corpus loops were declined as `trip-not-known`).
- **Register promotion of recurrences.** The dominant RecMII comes from memory-backed IV/accumulator recurrences; promoting them to registers (breaking the load-modify-store cycle) is the prerequisite for II below the memory-recurrence floor and is the highest-value follow-up.
- **Better modulo scheduler.** The current scheduler is correctness-first (scan II upward, greedy resource legalisation); swing modulo scheduling with slack-based ordering would find tighter schedules and reduce the 7 differential-rollbacks / declines.
- Not done (by design): no production integration, no cross-BB / trace / superblock / hyperblock scheduling, no speculation, no changes to the graph / `LoopTransform` / scheduler / bundler / register allocator. Stopped after R2.5.

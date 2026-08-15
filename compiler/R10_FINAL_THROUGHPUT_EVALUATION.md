# R10 — Final throughput evaluation and artifact freeze

**No compiler source was modified during R10.** This milestone measures and
freezes; it implements nothing.

---

## 1. Executive summary

The APARA vector compiler is frozen at **`df3d49a`**. Across the 38-program
verified suite it executes **67 689 ticks**, down from **210 359** at the start of
the R6 optimization campaign — a **3.11× end-to-end speedup**, all of it
correctness-preserving against gcc golden references.

The headline metrics, stated in the three senses that must not be conflated:

| | value |
|---|---|
| **execution time** — suite ticks | **67 689** |
| **throughput** — whole-program weighted IPB | **2.213** |
| **density** — vector-region aggregate IPB | **3.659** |
| best kernel density | reduction vi32 = **6.000** (100% of its oracle ceiling) |
| best kernel throughput | conv3 vi8 = **6.48 ticks/output** |

The most important methodological result of the whole campaign is that these
three move independently. R9.5 improved execution time by 37.88% while leaving
vector-region density **bit-identical**; and `axpy vi16` runs **faster** than
`axpy vi8` while having **3× lower IPB**. **IPB is a diagnostic, not an
objective.** The correct final target is ticks per output element.

Two honest limits on the numbers: **74.5% of the suite's dynamic bundles are
harness initialisation scaffolding**, not kernel; and GEMM vi32 does not scale
past M=16 (§6), which is recorded as future work rather than fixed.

## 2. Frozen compiler identity

| field | value |
|---|---|
| repository | `/home/mohithkota/complier_Apara/cmp_wd` (local-only, never pushed) |
| branch | `feature/vector-backend-r6` |
| **final commit** | **`df3d49a`** |
| **final tag** | **`r10-final`** (evaluation), `r9.5-verified` (last code change) |
| last commit touching compiler source | `df3d49a` (R9.5) |
| Python | 3.12.3 |
| external deps | pycparser only |
| toolchain | `engine_isp/assembler/bin` — `mcode_align`, `mcode_assemble`, `mcode_run` |

## 3. Verification

Run on the frozen tree, this milestone:

| check | result |
|---|---|
| 38-program simulator suite | **38/38 PASS** |
| negative controls | **3/3 rejected** |
| unit suites | **21/21 PASS** |
| `pipeline_crosscheck` | **PASS — 124/124** identical |
| spills | **0** |
| tracked `git status` | **clean** |

## 4. Benchmark methodology

Every benchmark is compiled, aligned, assembled, simulated, and checked against
an **independently gcc-compiled reference**; a program that cannot produce a
reference is a FAILURE, not a skip. Three negative controls prove the harness
cannot pass vacuously.

Metrics come from the simulator's own counters (`Stopped after N ticks`,
`number of non-null instructions executed`). One bundle issues per tick, so
executed bundles = ticks. **All IPB figures exclude `$null`**, which is
mandatory after R9.5 (§8).

## 5. Whole-program results — and the scaffolding that dominates them

**74.5% of suite dynamic bundles are the harness's scalar initialisation loops.**
This is disclosed, not removed:

| kernel | ticks | vector-region share of dynamic bundles |
|---|---|---|
| gemm vi32 | 4 893 | **47.4%** |
| matmul16 | 4 381 | 41.8% |
| gemm vi8 | 4 375 | 36.6% |
| conv3 vi32 | 543 | 26.7% |
| axpy vi32 | 906 | 12.5% |
| elementwise vi16 | 901 | 5.5% |
| dot vi8 | 889 | 2.0% |
| dot vi32 / vu32, reduction vu8/vu16/vu32, both scalar controls | — | **0%** (realisation selected is scalar) |
| **suite** | **67 689** | **25.5%** |

The O(N) families initialise with scalar loops of the *same order* as the kernel,
so vectorising the kernel makes the initialisation dominate. GEMM (O(M³) kernel
vs O(M²) init) is the only family where growing the problem fixes this — which is
what Part 6 tests.

## 6. Kernel-dominated results

Same compiler, same validation framework, the suite's own templates with only
the size constant changed. **These runs use locally scaled templates and are not
bit-comparable with §5; compare within this table only.**

| benchmark | ticks | outputs | **ticks/output** | vIPB | vector share |
|---|---|---|---|---|---|
| gemm vi16 M=16 | 4 375 | 256 | **17.09** | 3.625 | 41.9% |
| gemm vi16 M=24 | 10 750 | 576 | 18.66 | 4.333 | 43.5% |
| gemm vi16 M=32 | 19 982 | 1 024 | 19.51 | 4.900 | **46.4%** |
| gemm vi32 M=16 | 4 893 | 256 | 19.11 | 4.900 | 47.4% |
| **gemm vi32 M=24** | 65 471 | 576 | **113.66** | 1.833 | 56.4% |
| **gemm vi32 M=32** | 148 975 | 1 024 | **145.48** | 1.833 | 58.7% |
| elementwise vi16 N=64 | 901 | 64 | 14.08 | 2.143 | 5.5% |
| elementwise vi16 N=256 | 3 246 | 256 | 12.68 | 3.750 | 2.6% |
| axpy vi16 N=256 | 3 321 | 256 | 12.97 | 2.524 | 4.4% |
| elementwise vi8 N=256 | 3 462 | 256 | 13.52 | 3.429 | 1.4% |

Two findings:

* **GEMM vi16 scales well.** Work per output grows linearly with M (the k loop),
  yet ticks/output rises only 17.09 → 19.51 from M=16 to M=32 — normalised per
  unit of work that is 1.068 → 0.610, a **43% efficiency improvement**, and the
  vector share rises to 46.4%.
* **GEMM vi32 does not scale past M=16.** ticks/output collapses 19.11 → 113.66
  → 145.48 and vector density drops 4.900 → 1.833. **Recorded as future work
  (§13); not investigated or fixed, because R10 is a freeze.**
* **Growing N does not rescue the O(N) families** — vector share *falls* (5.5% →
  2.6%) because the scalar init grows with N too. Their ticks/output is
  essentially flat (14.08 → 12.68).

## 7. Throughput — ticks per output element (primary ranking)

Suite kernels, ranked by the metric that is a performance claim:

| rank | kernel | ticks | outputs | **ticks/output** |
|---|---|---|---|---|
| 1 | conv3 vi8 | 415 | 64 | **6.48** |
| 2 | conv3 vu8 | 487 | 64 | 7.61 |
| 3 | conv3 vi16 | 534 | 64 | 8.34 |
| 4 | conv3 vi32 | 543 | 64 | 8.48 |
| 5 | conv3 vu16 | 607 | 64 | 9.48 |
| 6 | conv3 vu32 | 615 | 64 | 9.61 |
| 7 | axpy vi16 | 766 | 64 | 11.97 |
| 8 | axpy vi8 | 823 | 64 | 12.86 |
| 9 | elementwise vi8 | 887 | 64 | 13.86 |
| … | gemm vi16 | 4 375 | 256 | 17.09 |
| … | gemm vu32 | 5 149 | 256 | 20.11 |

**Best kernel throughput: conv3 vi8 at 6.48 ticks per output element.**

## 8. IPB results

| metric | value |
|---|---|
| whole-program weighted | **2.213** |
| whole-program min / median / mean / max | 1.260 / **1.663** / 1.884 / 3.506 |
| **vector-region aggregate** | **3.659** |
| highest vector-region | **6.000** (reduction vi32) |
| lowest vector-region | 1.571 (axpy vi16/vu16) |

**Which kernels reach ≥ 6?** Exactly one: **reduction vi32 = 6.000**. Closest
below: conv3 vi8/vu8 5.731, reduction vi16 5.000, gemm vi32/vu32 4.900.

**Which cannot theoretically reach 6 under this ISA?** Every kernel whose R3.0
oracle ceiling is below 6: axpy (5.25 / 5.00), dot (5.25 / 5.00), gemm vi8/vu8
(5.60), elementwise vi8/vu8 and reduction vi8/vu8 (5.667), conv3 vi8/vu8 (5.50),
scalar divmod (5.50).

**High IPB, worse runtime — the decisive counter-example:**

| kernel | vector IPB | ticks |
|---|---|---|
| **axpy vi16** | 1.571 | **766** |
| **axpy vi8** | **4.789** | 823 |

`axpy vi8` has **3.05× the IPB and is 7.4% slower** on the identical 64-element
job. Likewise `gemm vi16` (IPB 3.625) and `gemm vi8` (IPB 2.429) finish in the
**same 4 375 ticks**. **Lower IPB, better runtime** is therefore a real and
reproducible outcome, not a curiosity.

**What IPB means after R9.5:** R9.5 fills loop bundles with `$null` for
alignment, so a slot-occupancy IPB would score those bundles 8.0 while nothing
extra executes. Only two definitions remain meaningful — non-null instructions
per tick (throughput) and real instructions per real bundle (density) — and both
are used here with `$null` excluded.

## 9. Theoretical ISA ceilings

Using the **existing** R3.0 oracle (`loopopt.oracle_ilp`), not a new model.

| kernel | measured vIPB | oracle ceiling | % achieved |
|---|---|---|---|
| reduction vi32 | 6.000 | 6.00 | **100%** |
| conv3 vi8 / vu8 | 5.731 | 5.50 | 104% |
| axpy vi8 / vu8 | 4.789 | 5.00 | 96% |
| dot vi16 / vu16 | 4.786 | 5.25 | 91% |
| reduction vi16 | 5.000 | 6.00 | 83% |
| gemm vi32 / vu32 | 4.900 | 6.00 | 82% |
| gemm vi16 / vu16 | 3.625 | 6.00 | 60% |
| axpy vi16 / vu16 | 1.571 | 5.25 | 30% |

**Stated limitation:** the oracle computes `theoretical_ipb` on the **scalar**
IR, so it is *not* an exact hardware ceiling for vector code. Vectorising
*removes* instructions — one `$v` replaces eight scalar ops — so vector code is
being compared against a different instruction mix. That is why conv3 vi8 can
read 104%. The only exact architectural ceiling is the **8-slot issue width**;
against it, vector regions run at 45.7% occupancy. R4.6.5 recorded this caveat
first and it still applies.

## 10. Cumulative optimization impact

Suite ticks over the campaign, as recorded at each milestone:

| milestone | suite ticks | Δ | note |
|---|---|---|---|
| R6 baseline (1× unroll) | 210 359 | — | pre-adaptive |
| R6.4.1 adaptive unroll | 136 847 | −34.9% | **largest single gain** |
| R6.7 region superblock | 136 826 | −0.02% | 1 of 38 programs |
| R6.8 vector SWP | 136 261 | −0.4% | axpy only |
| R8.1a | 136 206 | −0.04% | regression closed |
| **R9.1 address value numbering** | **131 743** | **−3.28%** | 12 improved, 0 regressed |
| R9.2 branch-immediate folding | 131 424 | −0.24% | 4 win / 4 alignment-artifact losses |
| **R9.3 GEMM `[reg+imm]`** | **108 960** | **−17.09%** | 6 GEMM kernels, up to −50.4% |
| **R9.5 alignment-aware bundling** | **67 689** | **−37.88%** | **all 38 improved** |
| **total** | **210 359 → 67 689** | **−67.8% (3.11×)** | |

**Optimization effect vs harness effect.** The 210 359 → 136 847 step is partly a
*realisation-selection* change (adaptive unroll picking a better configuration),
not pure code improvement; R4.6.5 flagged that IPB and instruction counts moved
for benchmark-shape reasons there. From **R9.1 onward** (131 743 → 67 689,
**−48.6%**) every step was measured against a fixed benchmark set with unchanged
sources, so that portion is attributable to the compiler alone.

**Which optimizations contributed the most real performance:** R9.5 (−37.88%,
all kernels), then R9.3 (−17.09%, GEMM), then R9.1 (−3.28%). R9.2 contributed
−0.24% and is retained because it can never add work.

## 11. Remaining bottlenecks after R9.5

Unchanged from R9.4 — R9.5 operated below the scheduler, on layout, so it altered
no empty slot. Vector regions, dynamic, 71 506 empty slots (causes sum exactly):

| cause | share |
|---|---|
| **waiting-for-address-alu** | **48.5%** |
| waiting-for-vector-alu | 13.0% |
| waiting-for-vector-load | 13.0% |
| region-boundary-label | 11.7% |
| waiting-for-vector-multiply | 9.0% |
| memory-dependence | 2.1% |
| memory-lanes-full | 1.6% |
| waiting-for-scalar-load | 1.0% |
| store-ordering / reduction | 0.1% |
| **padding** | **not in this taxonomy** — measured separately, now ~0 executed |

Whole program: 381 279 empty slots, occupancy 26.1%; address-alu 34.9%,
region-boundary-label 25.5%, region-boundary-control 14.0%.

**What remains:** address-ALU waits dominate, but they are largely *not
convertible into ticks* — 30 of 32 hot vector blocks are dependence-height-bound
and 20 sit exactly on that bound, so filling a slot removes no bundle.

## 12. Compiler limits addressed vs architectural limits remaining

**Compiler-side, measured and fixed:**

| issue | evidence | milestone |
|---|---|---|
| redundant frame-address materialization | 12 programs improved | R9.1 |
| loop-invariant branch constants recomputed per iteration | 3 instrs → 2 | R9.2 |
| per-chunk address re-derivation hiding disjointness from R6.2 | 152 same-base edges → **0** | R9.3 |
| conservative memory dependence in GEMM | vector empty slots 42 479 → 1 519 | R9.3 |
| unmodelled alignment padding | executed pads 34 507 → **625** | R9.5 |
| register spills | 0 across all kernels | R7.1 + R9.x |

**Architectural / machine constraints remaining, each measurement-backed:**

| constraint | evidence |
|---|---|
| **no scaled-index addressing mode** (`[reg+reg]` / `[reg+imm]` only) | every index→byte conversion needs an explicit `<<`; address-alu is 48.5% of vector empty slots |
| **8-slot issue width** | vector regions at 45.7% occupancy; issue width binds in reduction vi16/vi32 |
| **`$v` granularity — one 64-bit register per instruction** | lanes = 8/elem_bytes fixes the per-instruction work |
| **28-register pool** | not currently binding (peak live ≤ 28, 0 spills) but leaves no headroom for deeper pipelining |
| **kernel arithmetic/memory ratio** | dependence height binds 30/32 hot blocks; oracle limiter is `resource-bound-width` for every vector kernel |
| **4 memory lanes / 1 divide lane** | never binding (1.6% / 0.1% of empty slots) — measured, not assumed |

## 13. Limitations and future work

1. **Harness scaffolding is 74.5% of suite dynamic bundles.** Whole-program IPB
   is diluted accordingly; vector-region figures describe the kernels.
2. **GEMM vi32 does not scale past M=16** — ticks/output 19.11 → 145.48 at M=32,
   vector density 4.900 → 1.833. Measured in §6, **not investigated**. This is the
   single clearest future-work item.
3. **34 bundles of scheduler slack** remain across 12 hot blocks (dot vi16/vu16
   hold 8 each). R6.5 previously failed to beat the shipped schedule with 12 000
   random legal reorderings.
4. **The oracle ceiling is scalar-derived** and is not an exact hardware bound.
5. **Frequency-weighted pad estimates** run ≈0.87× measured ticks; only
   arm-to-arm deltas are quoted from them.
6. R9.3's local-GVN experiment is retained unshipped in `wip_r9_3/`, measured
   cycle-neutral (−4.63% dynamic instructions, **0 ticks**).

## 14. Reproducibility

See `REPRODUCIBILITY.md` for exact commands, tool paths and environment.

## 15. Final conclusions

1. **Final whole-program weighted IPB: 2.213.**
2. **Final vector-region aggregate IPB: 3.659.**
3. **Highest measured vector-region IPB: 6.000** (reduction vi32).
4. **Kernels at ≥ 6 IPB: one** — reduction vi32, at 100% of its oracle ceiling.
5. **Best kernel throughput: 6.48 ticks/output** (conv3 vi8).
6. **Largest measured gain: 210 359 → 67 689 ticks, −67.8% (3.11×)**; attributable
   to the compiler alone from R9.1 onward: 131 743 → 67 689, **−48.6%**.
7. **Optimizations that contributed most:** R9.5 alignment-aware bundling
   (−37.88%, all 38 kernels), R9.3 GEMM `[reg+imm]` (−17.09%), R9.1 address value
   numbering (−3.28%).
8. **Dominant remaining bottleneck:** address-ALU dependence chains (48.5% of
   vector empty slots) — but largely ISA-mandated and mostly not convertible into
   ticks, since 20 of 32 hot blocks already ship at the dependence lower bound.
9. **Is further compiler optimization justified?** **Not on the present
   evidence.** Memory dependence is spent (2.1%), registers are not binding
   (0 spills), padding is 98.2% removed, and the scheduler is at its lower bound
   where it matters. The one concrete lead is GEMM vi32 scaling (§13.2), which is
   a *scaling defect to diagnose*, not a new optimization to invent.
10. **The appropriate final optimization target is throughput — ticks per output
    element — not IPB.** Proven three ways in this campaign: R9.5 cut execution
    time 37.88% with *zero* change in vector density; `axpy vi8` has 3× the IPB of
    `axpy vi16` and is slower; and `gemm vi16`/`gemm vi8` differ by 1.5× in IPB
    while taking identical time.

**R10 COMPLETE — FINAL THROUGHPUT EVALUATION — COMPILER FROZEN**

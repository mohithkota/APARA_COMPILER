# R9.4 — Post-R9.3 final vector performance analysis

**ANALYSIS ONLY. No compiler source modified.** Measured on `6e0f738`
(`r9.3-verified`) against `50e2b67` (`r9.2-verified`), the latter rebuilt from
`git archive HEAD` into an independent reference tree so both arms are measured
by the same instruments.

Companion to `R9_4_POST_R9_3_BOTTLENECK_ANALYSIS.md`, which covers the empty-slot
attribution and the padding experiment. This document answers the IPB question
per kernel and closes out the post-R9.3 picture.

---

## 1. Executive summary

**The primary question — what happened to IPB — has an unusually clean answer:
nothing, except in GEMM.**

All 26 non-GEMM kernels have **bit-identical vector-region IPB and bit-identical
ticks** in both arms (0.0% on every row of §2/§3). R9.3 changed exactly one
function (`gemm_lowering.build_unrolled`), and the measurement confirms it
touched nothing else. Aggregate vector-region IPB moved 2.349 → 3.659 (+55.8%)
purely on GEMM's contribution.

Within GEMM, **IPB and performance moved together**, which is not the usual case
in this project: gemm vi32/vu32 IPB +98.9% with ticks −50.4%/−47.9%; vi16/vu16
IPB +56.8% with ticks −30.3%/−28.1%; vi8/vu8 IPB +21.4% with ticks
−12.5%/−12.1%. No kernel lost IPB, and no kernel gained IPB while slowing down.

The reason they co-moved is worth stating precisely, because it is the exception
that proves the project's rule: R9.3 removed **dependence edges**, so bundles
fell faster than instructions did. IPB rose as a *consequence* of a real speedup,
rather than by emitting more instructions (the AXPY U=1 → U≥2 failure mode
recorded in the frozen-state notes, where IPB 1.57 → 6.95 was 45% *slower*).

The scheduler is at its dependence lower bound for **20 of 32 hot vector blocks
in both arms**, with **identical 34 bundles of total slack**. R9.3 did not
improve the schedule; it lowered the bound (matmul16 height 16 → 8) and the
scheduler kept hitting it. R6.5's conclusion therefore still holds after R9.3.

The dominant remaining cost is **bundle-alignment padding**: 35.0% of static
bundles and ~31.7% of measured ticks, established as executed, and demonstrated
removable with a −38.3% suite tick effect.

## 2. R9.2 vs R9.3 — vector-region IPB

Aggregate (dynamic, frequency-weighted), the two metrics kept strictly apart:

| scope | R9.2 IPB | R9.3 IPB | R9.2 occupancy | R9.3 occupancy |
|---|---|---|---|---|
| **whole program** | 1.896 | 2.092 | 23.7% | 26.1% |
| **vector regions** | **2.349** | **3.659** | **29.4%** | **45.7%** |

Per kernel (vector-region IPB only):

| kernel | R9.2 | R9.3 | Δ | Δ% |
|---|---|---|---|---|
| **gemm vi32 / vu32** | 2.464 | **4.900** | +2.436 | **+98.9%** |
| **gemm vi16 / vu16** | 2.312 | **3.625** | +1.313 | **+56.8%** |
| **matmul16** | 2.312 | **3.625** | +1.313 | **+56.8%** |
| **gemm vi8 / vu8** | 2.000 | **2.429** | +0.429 | **+21.4%** |
| axpy vi8 / vu8 | 4.789 | 4.789 | 0 | 0.0% |
| axpy vi16 / vu16 | 1.571 | 1.571 | 0 | 0.0% |
| axpy vi32 / vu32 | 1.812 | 1.812 | 0 | 0.0% |
| conv3 vi8 / vu8 | 5.731 | 5.731 | 0 | 0.0% |
| conv3 vi16 / vu16 | 1.800 | 1.800 | 0 | 0.0% |
| conv3 vi32 / vu32 | 2.818 | 2.818 | 0 | 0.0% |
| dot vi16 / vu16 | 4.786 | 4.786 | 0 | 0.0% |
| dot vi8 / vu8 | 3.900 | 3.900 | 0 | 0.0% |
| elementwise vi8 / vu8 | 4.316 | 4.316 | 0 | 0.0% |
| elementwise vi16 / vu16 | 2.143 | 2.143 | 0 | 0.0% |
| elementwise vi32 / vu32 | 3.125 | 3.125 | 0 | 0.0% |
| reduction vi8 | 2.600 | 2.600 | 0 | 0.0% |
| reduction vi16 | 5.000 | 5.000 | 0 | 0.0% |
| reduction vi32 | 6.000 | 6.000 | 0 | 0.0% |

## 3. R9.2 vs R9.3 — performance

| kernel | R9.2 ticks | R9.3 ticks | Δ | speedup | ticks/output |
|---|---|---|---|---|---|
| gemm vi32 | 14 211 | 7 043 | −7 168 | **−50.4%** | 27.5 |
| gemm vu32 | 14 979 | 7 811 | −7 168 | −47.9% | 30.5 |
| gemm vi16 | 10 093 | 7 037 | −3 056 | −30.3% | 27.5 |
| matmul16 | 10 099 | 7 043 | −3 056 | −30.3% | 27.5 |
| gemm vu16 | 10 861 | 7 805 | −3 056 | −28.1% | 30.5 |
| gemm vi8 | 8 045 | 7 037 | −1 008 | −12.5% | 27.5 |
| gemm vu8 | 8 301 | 7 293 | −1 008 | −12.1% | 28.5 |
| **all 26 non-GEMM kernels** | — | — | **0** | **0.0%** | unchanged |
| **suite total** | **131 424** | **108 960** | **−22 464** | **−17.09%** | |

`ticks/output`: GEMM produces 256 outputs (16×16 C), elementwise/axpy/conv3 64,
dot/reduction a single scalar (so ticks/output is not meaningful for those and is
omitted rather than reported misleadingly).

**Correlation of IPB change with execution-time change:** perfect rank agreement
within GEMM — the kernel with the largest IPB gain (vi32, +98.9%) has the largest
speedup (−50.4%), and the smallest (vi8, +21.4%) the smallest (−12.5%). Outside
GEMM both are exactly zero. There is no kernel where the two metrics disagree in
sign.

## 4. matmul16 detailed breakdown — figures verified against emitted code

| metric | R9.2 | R9.3 | prior claim |
|---|---|---|---|
| instructions (hot block `fb_10`) | **37** | **29** | ~37 / 29 ✓ |
| bundles | **16** | **8** | 16 / 8 ✓ |
| dependence height | **16** | **8** | 16 / 8 ✓ |
| height, registers only | 11 | 8 | — |
| memory dependence edges | 17 | **0** | — |
| address-generation instructions | 15 | **7** | — |
| issue-width lower bound | 5 | 4 | — |
| memory-lane lower bound | 4 | 4 | — |
| IR instructions (whole program) | 111 | 92 | — |
| vector-region occupancy | 28.9% | **45.3%** | — |
| vector-region IPB | 2.312 | **3.625** | — |
| aligned bundles per iteration | 25 | **13** | — |
| — of which pad | 9 | 5 | — |
| executed hot-loop ticks (×256) | 6 400 | 3 328 | — |
| executed hot-loop pad ticks | 2 304 | **1 280** | — |
| whole-program ticks | 10 099 | 7 043 | — |

**All three prior figures are confirmed exactly** (37 and 29 instructions, 16 and
8 bundles, height 16 and 8) against the production-emitted mcode, not the
hand-written block model.

## 5. Vector-region occupancy and empty-slot attribution

Vector regions, dynamic: 16 474 bundles · 131 792 issue slots · 60 286 occupied ·
**71 506 empty** · occupancy 45.7% · IPB 3.659. Causes sum to 71 506 exactly —
the partition is exhaustive by construction, not by a residual bucket.

| cause | R9.2 | R9.3 | % of R9.3 empty |
|---|---|---|---|
| waiting-for-address-alu | 22 926 | **34 702** | **48.5%** |
| waiting-for-vector-alu | 49 730 | 9 282 | 13.0% |
| waiting-for-vector-load | 47 411 | 9 267 | 13.0% |
| region-boundary-label | 11 700 | 8 372 | 11.7% |
| waiting-for-vector-multiply | 10 560 | 6 464 | 9.0% |
| memory-dependence | 42 479 | **1 519** | 2.1% |
| memory-lanes-full | 94 | 1 118 | 1.6% |
| waiting-for-scalar-load | 721 | 721 | 1.0% |
| store-ordering / reduction | 61 | 61 | 0.1% |

Whole program, dynamic: 64 534 bundles, 381 279 empty slots, occupancy 26.1%.
Top causes: waiting-for-address-alu 34.9%, region-boundary-label 25.5%,
region-boundary-control 14.0%, waiting-for-scalar-load 6.2%, memory-dependence
6.1%.

`padding` cannot appear in this taxonomy: occupancy analyses the bundles the
compiler *emits*, and pads are inserted afterwards by `mcode_align`. That is
exactly why it stayed invisible until §6.

## 6. Pad-bundle analysis

| family | kernels | static bundles | static pad | pad % | executed pad % | ticks |
|---|---|---|---|---|---|---|
| axpy | 6 | 541 | 203 | 37.5% | 39.9% | 9 243 |
| conv3 | 6 | 433 | 139 | 32.1% | 41.4% | 5 679 |
| dot | 6 | 556 | 179 | 32.2% | 33.2% | 13 050 |
| elementwise | 6 | 498 | 178 | 35.7% | 37.5% | 9 572 |
| gemm | 6 | 499 | 164 | 32.9% | 34.0% | 44 026 |
| reduction | 6 | 392 | 150 | 38.3% | 44.9% | 6 455 |
| scalar (control) | 2 | 165 | 67 | 40.6% | 46.7% | 20 935 |
| **TOTAL** | **38** | **3 084** | **1 080** | **35.0%** | **36.3%** | **108 960** |

Estimated executed pad bundles **34 507 of 95 039**; the estimator lands at 0.87×
measured ticks, so the pad contribution is approximately **31.7% of total
runtime**. Reachability is therefore established, which
`R9_2_DELIVERY.md` §7 explicitly could not do. Independently confirmed on
matmul16 by exact trip count (5 pad bundles/iteration × 256 = 1 280 ticks).

Mechanism verified, not assumed: `mcode_align` rounds each bundle to a
power-of-two slot count and requires `PC ≡ 0 (mod size)` — checked on gemm vi16,
**38 labelled bundles, 0 violations** — and bridging costs up to three
consecutive pad bundles, each costing one tick.

## 7. Address-generation analysis

Hot vector blocks, all kernels: address instructions **742 → 676 (−8.9%)**;
share of hot-block instructions 43.5% → 41.2%.

| shape | R9.2 | R9.3 | delta |
|---|---|---|---|
| base + constant | 463 | 438 | −25 |
| **scaled index (`<<`)** | **139** | **91** | **−48** |
| base + index | 101 | 108 | +7 |
| IV / pointer increment | 11 | 11 | 0 |
| other | 28 | 28 | 0 |

In GEMM specifically the reduction is dramatic — gemm vi32/vu32 **27 → 7**
address instructions, with scaled-index ops 17 → 3; gemm vi16/vu16 and matmul16
15 → 7, scaled-index 9 → 3.

**Did `[reg+imm]` materially reduce address-generation cost?** *In GEMM, yes —
by 74% in the 32-bit kernels.* **Suite-wide, no** — only −8.9%, because R9.3 was
deliberately scoped to `gemm_lowering.build_unrolled` and no other lowering path
uses it. And `waiting-for-address-alu` **rose** to 48.5% of vector empty slots,
because the other causes shrank around it.

The residual is substantially ISA-mandated: APARA addresses memory as
`[reg + reg]` or `[reg + imm]` only, with **no scaled-index addressing mode**, so
every element-index → byte-offset conversion needs an explicit `<<` and every
non-constant offset an explicit `+`.

## 8. Memory-dependence analysis

Hot vector blocks:

| | R9.2 | R9.3 |
|---|---|---|
| same-base edges | **152** | **0** |
| cross-base edges | 380 | 329 |
| GEMM vi32/vu32 (same / cross) | 56 / 21 | **0 / 0** |
| GEMM vi16/vu16, matmul16 | 12 / 3 | **0 / 0** |
| GEMM vi8/vu8 | 2 / 0 | **0 / 0** |
| vector-region empty slots from memory-dependence | 42 479 | **1 519 (−96.4%)** |

**How `[reg+reg]` became `[reg+imm]`, and what it did to the graph.** Before,
`build_unrolled` cloned the address computation once per chunk, so the four
accumulator accesses shared a base register but held their offsets in four
*different* registers. R6.2 evaluates addresses symbolically within a basic
block and treats registers live into the block as opaque, so four opaque offsets
are pairwise incomparable — every store had to be ordered against every later
access, giving the 152 same-base edges. R9.3 computes the invariant row base once
and expresses each chunk as `base + c*lanes*eb`, a *constant* delta. R6.2 can
compare constants, proves the four accesses disjoint, and **all 152 same-base
edges vanish**. Emitting every chunk's loads before any store then removes the
remaining cross-base edges in GEMM, which is what took matmul16's height from 20
(chunk-serial) to 8.

**Memory dependence is now spent as a target**: 2.1% of vector-region empty
slots. The 329 residual cross-base edges are in non-GEMM kernels and are largely
genuine — two opaque base registers that a deliberately block-local analysis
cannot relate.

## 9. Register-pressure analysis

Real allocator, 28-register pool:

| | R9.2 | R9.3 |
|---|---|---|
| kernels whose register profile changed | — | **7 (all GEMM + matmul16)** |
| gemm vi16 / vu16 / matmul16 peak live | 15 | **14** |
| gemm vi16 / vu16 / matmul16 registers used | 26 | **25** (3 free) |
| gemm vi8 / vu8 registers used | 23 | **19** (9 free) |
| gemm vi32 / vu32 peak live | 13 | 19 |
| median hot-block peak live | 15 | 14 |
| **memory spills (`cg.spilled_to_memory`)** | **0 / 39** | **0 / 39** |
| **evictions (`cg.spilled`)** | **0 / 39** | **0 / 39** |
| rematerialized evictions | 0 | 0 |

**The R9.3 speedup did not come at the expense of hidden register pressure.**
Zero spills and zero evictions in both arms, and the only kernels whose profile
changed are the GEMM ones R9.3 targeted. gemm vi32/vu32 peak live rose 13 → 19,
which is the expected cost of hoisting loads above stores (more values live at
once), but it stays well inside the pool and produced no eviction — the same
kernels are the ones that gained 50%.

## 10. Lower-bound analysis

Per hot vector block: shipped bundles vs dependence height vs `ceil(instrs/8)`
vs `ceil(mem_ops/4)`.

* **Dependence height is the binding resource in 30 of 32 blocks**; issue width
  binds only in reduction vi16/vi32. Memory lanes never bind.
* **At the dependence lower bound: 20/32 in R9.2, 20/32 in R9.3.**
* **Total slack (shipped − height): 34 bundles in R9.2, 34 bundles in R9.3 —
  unchanged.**
* Every GEMM block is at the bound in both arms (R9.2 16 = 16, R9.3 8 = 8).

**R6.5's conclusion survives R9.3.** The scheduler still reaches the lower bound
for the code it is given; R9.3 delivered its speedup by *lowering the bound*, not
by scheduling better. The 34 bundles of residual slack sit in dot vi16/vu16 (8
each), elementwise vi8/vu8 (3 each), axpy vi8/vu8 and dot vi8/vu8 (2 each), and
four blocks with 1 each.

## 11. Final IPB interpretation

1. **Did vector-region IPB increase?** Yes — aggregate 2.349 → 3.659 (+55.8%),
   entirely from GEMM.
2. **Which kernels gained most?** gemm vi32/vu32 +98.9%, then gemm vi16/vu16 and
   matmul16 +56.8%, then gemm vi8/vu8 +21.4%. No other kernel changed at all.
3. **Gained performance without much IPB change?** None. Every kernel that got
   faster also gained IPB, and every kernel with unchanged IPB has unchanged
   ticks.
4. **Lost IPB while becoming faster?** None.
5. **Distance from the theoretical limit.** The ISA issues 8 slots/bundle, so
   IPB 8.0 is the ceiling. R9.3 vector regions run at 3.659 = **45.7% of the
   limit** (was 29.4%). Best individual kernels: conv3 vi8/vu8 5.731 (71.6%),
   reduction vi32 6.000 (75.0%), gemm vi32/vu32 4.900 (61.3%). Worst: axpy
   vi16/vu16 1.571 (19.6%). **The remaining gap is not slack the scheduler can
   take** — 30 of 32 blocks are dependence-bound and 20 are exactly at the
   bound, so the distance to IPB 8 measures the programs' dependence structure,
   not compiler quality.

## 12. Remaining bottleneck ranking

| rank | bottleneck | % of vector empty slots | affected kernels | compiler-controllable | leverage |
|---|---|---|---|---|---|
| **1** | **bundle-alignment padding** | not in the taxonomy (post-emission) | **38/38** | **yes** — bundle size selection | **−38.3% suite ticks, measured** |
| 2 | address generation | 48.5% | most | partly (ISA has no scaled-index mode) | ~0 — blocks are dependence-bound |
| 3 | true vector dependence (vector-alu / load / multiply) | 35.0% | all | no — program structure | 0 |
| 4 | region boundaries (label + control) | 11.7% vector / 39.5% whole | all | already done (R3.2 superblock, in every hot tier) | 0 |
| 5 | scheduler slack | — | 12 blocks, 34 bundles | yes | small; R6.5 failed to beat it with 12 000 reorderings |
| 6 | memory dependence | 2.1% | non-GEMM | partly | small |
| 7 | memory lanes | 1.6% | few | no | 0 |
| 8 | register pressure | 0% | none | n/a | none |

**Why the biggest empty-slot category is not the biggest opportunity:** empty
slots are not ticks. With 30 of 32 hot blocks dependence-bound and 20 exactly at
the bound, filling an empty slot removes no bundle. Address work costs time only
where it lengthens the dependence *chain* — which is precisely what R9.3 already
harvested in GEMM.

## 13. Decision

Padding is the only candidate that satisfies every survival condition:

1. **affects an important hot kernel** — all 38, including every GEMM kernel;
2. **compiler-controllable** — the compiler chooses bundle sizes, which determine
   the alignment padding;
3. **measured evidence** — 35.0% of static bundles, ~31.7% of measured ticks;
   mechanism verified (`PC ≡ 0 mod size`, 0 violations in 38 labelled bundles);
4. **reduces executed bundles/ticks** — rewriting emitted bundles to full width
   and running the unmodified toolchain gives **108 960 → 67 178 ticks
   (−38.3%)**, all 38 kernels improving, every PostCondition passing;
5. **not IPB inflation** — instruction count is unchanged and bundle count falls,
   so ticks genuinely fall (the opposite of the AXPY U≥2 case);
6. **not already implemented** — `R9_2_DELIVERY.md` §7 records that no pass
   models bundle alignment; the scheduler and bundler optimise source bundles;
7. **validatable with the existing framework** — the experiment already ran the
   38-program suite with full PostCondition checking.

Caveats that must travel with it: −38.3% comes from a deliberately crude
width-8 upper bound costing +11.1% IMEM (largest program 640 of 2 048 words), so
a real pass choosing sizes rather than maximising them will land below that
figure.

IMPLEMENT NEXT OPTIMIZATION: alignment-aware bundle formation

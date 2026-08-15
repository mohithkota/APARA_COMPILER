# R9.5A — Final post-R9.5 IPB characterization

**ANALYSIS ONLY. No compiler source modified, no commits.** Measured on
`df3d49a` (`r9.5-verified`) against `6e0f738` (`r9.3-verified`).

---

## 1. Executive summary

**The one-line answer.** After R9.5 the compiler runs at **2.213 instructions
per executed bundle suite-wide**, and **3.659 inside the vector regions**. The
best vector kernel is `reduction vi32` at **6.000**, which is **100% of its R3.0
oracle ceiling**. The whole-program median is 1.663 and the whole-program maximum
is 3.506.

**The finding that matters most.** R9.5's +61% suite IPB gain was **entirely
throughput, not density**. Vector-region occupancy is **bit-identical between
R9.3 and R9.5 for all 39 kernels** — same instructions, same real bundles, same
empty slots, same 3.659 IPB. R9.5 did not make the code denser; it deleted empty
*bundles* that were sitting between the dense ones. The two metrics must not be
conflated, and this milestone is the cleanest example of why:

| | R9.3 | R9.5 |
|---|---|---|
| vector-region **density** (real instrs / real vector bundle) | 3.659 | **3.659 (unchanged)** |
| suite **throughput** (real instrs / tick) | 1.375 | **2.213 (+61.0%)** |

Confirmed at the hot-loop level: matmul16's `fb_10` executed 13 aligned bundles
per iteration under R9.3 (8 real + 5 pad) and executes **8 real + 0 pad** under
R9.5 — so its vector-region throughput has *converged onto* its density (both
3.625) instead of being diluted by padding.

## 2. Whole-program IPB

**Definition used: non-null instructions actually executed, divided by executed
bundles.** On this machine one bundle issues per tick, so the denominator is the
simulator's tick count. Both numerator and denominator come from the simulator's
own counters (`number of non-null instructions executed`, `Stopped after N
ticks`) — not from a static model. `$null` slots are excluded from the numerator,
which matters after R9.5 (see §5).

| statistic | value |
|---|---|
| minimum | **1.260** (reduction vi8) |
| median | **1.663** |
| mean (unweighted over 38 programs) | 1.884 |
| maximum | **3.506** (gemm vi32) |
| total executed instructions | 149 781 |
| total executed bundles (= ticks) | 67 689 |
| **weighted suite IPB** | **2.213** |

## 3. Vector-region IPB

**Definition used: real instructions in vector-region bundles / real vector-region
bundles**, frequency-weighted, from `vector_backend/occupancy.py` whose packing is
asserted identical to the production bundler. This is a *density* measure and is
not the same number as §2.

| kernel | v.instrs | v.bundles | **vIPB** | occupancy | ticks |
|---|---|---|---|---|---|
| reduction vi32 | 96 | 16 | **6.000** | 75.0% | 339 |
| conv3 vi8 / vu8 | 149 | 26 | **5.731** | 71.6% | 415 / 487 |
| reduction vi16 | 80 | 16 | 5.000 | 62.5% | 331 |
| gemm vi32 / vu32 | 12 544 | 2 560 | 4.900 | 61.3% | 4 893 / 5 149 |
| axpy vi8 / vu8 | 91 | 19 | 4.789 | 59.9% | 823 / 951 |
| dot vi16 / vu16 | 134 | 28 | 4.786 | 59.8% | 833 / 960 |
| elementwise vi8 / vu8 | 82 | 19 | 4.316 | 53.9% | 887 / 1 015 |
| dot vi8 / vu8 | 78 | 20 | 3.900 | 48.7% | 889 / 1 017 |
| gemm vi16 / vu16, matmul16 | 7 424 | 2 048 | 3.625 | 45.3% | 4 375 / 4 631 / 4 381 |
| elementwise vi32 / vu32 | 200 | 64 | 3.125 | 39.1% | 910 / 1 038 |
| conv3 vi32 / vu32 | 465 | 165 | 2.818 | 35.2% | 543 / 615 |
| reduction vi8 | 52 | 20 | 2.600 | 32.5% | 365 |
| gemm vi8 / vu8 | 4 352 | 1 792 | 2.429 | 30.4% | 4 375 / 4 631 |
| elementwise vi16 / vu16 | 120 | 56 | 2.143 | 26.8% | 901 / 1 029 |
| axpy vi32 / vu32 | 232 | 128 | 1.812 | 22.7% | 906 / 1 034 |
| conv3 vi16 / vu16 | 270 | 150 | 1.800 | 22.5% | 534 / 607 |
| axpy vi16 / vu16 | 176 | 112 | 1.571 | 19.6% | 766 / 895 |
| **AGGREGATE** | **60 286** | **16 474** | **3.659** | **45.7%** | |

## 4. R9.3 → R9.5 comparison

**Density: unchanged.** All 39 kernels report byte-identical vector-region
occupancy records in both arms (instructions, bundles, issue slots, empty slots,
occupancy, IPB). This is expected and correct: R9.5 edits only bundle *widths*
for layout, never the packing.

**Throughput: improved everywhere.** Ticks fell in all 38 kernels, 0 regressed,
while the dynamic instruction count is **identical (+0.00%)**.

| kernel | ticks R9.3 → R9.5 | Δ% | throughput IPB R9.3 → R9.5 |
|---|---|---|---|
| reduction vi8 | 752 → 365 | −51.5% | 0.612 → 1.260 |
| conv3 vi8 | 850 → 415 | −51.2% | 0.728 → 1.492 |
| axpy vi8 | 1 533 → 823 | −46.3% | 0.751 → 1.399 |
| reduction vu16 / vu32 | 1 608 → 869 | −46.0% | 1.338 → 2.476 |
| gemm vu16 | 7 805 → 4 631 | −40.7% | 1.607 → 2.708 |
| gemm vi16 | 7 037 → 4 375 | −37.8% | 1.709 → 2.749 |
| matmul16 | 7 043 → 4 381 | −37.8% | 1.709 → 2.747 |
| gemm vi32 | 7 043 → 4 893 | −30.5% | 2.435 → 3.506 |
| dot vi32 | 3 401 → 2 431 | −28.5% | 1.777 → 2.485 |
| **suite** | **108 960 → 67 689** | **−37.88%** | **1.375 → 2.213** |

**Did IPB and performance move together?** Yes, but only because the numerator
was pinned. Every kernel's real instruction count is unchanged, so throughput IPB
is `constant / ticks` — it *must* move inversely with ticks. That makes IPB a
faithful restatement of the speedup here and nothing more; it carries no
independent information in this comparison.

## 5. Padding impact, and what IPB means after R9.5

R9.5 pads loop-resident bundles with `$null` so that mcode_align stops inserting
pad *bundles*. That changes what different IPB definitions mean:

| definition | before R9.5 | after R9.5 | usable? |
|---|---|---|---|
| non-null instructions / tick | 1.375 | 2.213 | **yes — use this** |
| real instructions / real bundle (density) | 3.659 | 3.659 | **yes — use this** |
| slots occupied / issue slots, counting `$null` | meaningful | **meaningless** | **no** |

The third is now broken: a widened bundle contains 8 "instructions", of which up
to 7 are `$null`, so a slot-counting metric would score it 8.0 while nothing extra
executes. **Any post-R9.5 IPB figure must state whether `$null` is excluded.**
Every number in this document excludes it.

Supporting counts (suite): real instructions 149 781 (unchanged), real bundles
2 004 static (unchanged), executed pad bundles **34 507 → 625 (−98.2%)**, total
executed bundles 108 960 → 67 689.

## 6. Theoretical ceilings

Using the **existing** R3.0 oracle (`loopopt.oracle_ilp.analyze_module`, the same
analyzer `evaluation/metrics.oracle_of` calls) — no new model was invented. The
oracle computes `theoretical_ipb` on the **scalar** form, and every kernel's
limiter is reported as `resource-bound-width` (except scalar bubblesort:
`control-bound`).

| kernel | measured vIPB | oracle ceiling | % of ceiling |
|---|---|---|---|
| **reduction vi32** | **6.000** | 6.00 | **100%** |
| conv3 vi8 / vu8 | 5.731 | 5.50 | **104%** |
| axpy vi8 / vu8 | 4.789 | 5.00 | 96% |
| dot vi16 / vu16 | 4.786 | 5.25 | 91% |
| reduction vi16 | 5.000 | 6.00 | 83% |
| gemm vi32 / vu32 | 4.900 | 6.00 | 82% |
| dot vi8 / vu8 | 3.900 | 5.00 | 78% |
| elementwise vi8 / vu8 | 4.316 | 5.67 | 76% |
| gemm vi16 / vu16, matmul16 | 3.625 | 6.00 | 60% |
| elementwise vi32 / vu32 | 3.125 | 6.00 | 52% |
| reduction vi8 | 2.600 | 5.67 | 46% |
| conv3 vi32 / vu32 | 2.818 | 6.25 | 45% |
| gemm vi8 / vu8 | 2.429 | 5.60 | 43% |
| elementwise vi16 / vu16 | 2.143 | 6.00 | 36% |
| axpy vi32 / vu32 | 1.812 | 5.25 | 35% |
| axpy vi16 / vu16 | 1.571 | 5.25 | 30% |
| conv3 vi16 / vu16 | 1.800 | 6.25 | 29% |

Ceiling range across all kernels: **5.00 – 6.25**. conv3 vi8 exceeding 100% is
not an error — R4.6.5 already warns that the oracle ceiling is derived from the
*scalar* IR, so vector code (which replaces eight scalar ops with one `$v`) is
being compared against a different instruction mix.

## 7. Kernel ranking

* **highest vector IPB** — `reduction vi32`, 6.000 (100% of ceiling)
* **lowest vector IPB** — `axpy vi16 / vu16`, 1.571 (30% of ceiling)
* **fastest kernel** — `reduction vi16`, 331 ticks
* **biggest R9.5 improvement** — `reduction vi8`, −51.5% ticks
* **highest whole-program IPB** — `gemm vi32`, 3.506
* by % of ceiling: reduction vi32 100%, conv3 vi8/vu8 104%, axpy vi8/vu8 96%,
  dot vi16/vu16 91%

## 8. Occupancy / empty-slot attribution

Because packing is unchanged, the attribution is **identical to R9.4** — all 39
kernels match exactly. Vector regions, dynamic, 71 506 empty slots (causes sum
exactly; the partition is exhaustive by construction):

| cause | share |
|---|---|
| waiting-for-address-alu | **48.5%** |
| waiting-for-vector-alu | 13.0% |
| waiting-for-vector-load | 13.0% |
| region-boundary-label | 11.7% |
| waiting-for-vector-multiply | 9.0% |
| memory-dependence | 2.1% |
| memory-lanes-full | 1.6% |
| waiting-for-scalar-load | 1.0% |
| store-ordering / reduction | 0.1% |
| **padding** | **not in this taxonomy** — see below |

Whole program: 381 279 empty slots, occupancy 26.1%; address-alu 34.9%,
region-boundary-label 25.5%, region-boundary-control 14.0%.

**Answer to "what dominates now that padding is gone":** `waiting-for-address-alu`
at 48.5% of vector-region empty slots — the same as at R9.4. Padding never
appeared in this taxonomy because occupancy measures the bundles the compiler
*emits*, while pads were inserted afterwards by the aligner. R9.5 removed 98.2%
of executed pads without touching a single empty slot in this table, which is
precisely why the two views had to be reported separately.

## 9. Lower bounds

Unchanged from R9.4, since R9.5 does not alter packing:

* **dependence height binds in 30 of 32 hot vector blocks**; issue width binds
  only in reduction vi16/vi32; memory lanes never bind;
* **20 of 32 hot blocks ship exactly at the dependence lower bound**;
* residual slack 34 bundles, concentrated in dot vi16/vu16 (8 each).

**The production scheduler still reaches the relevant lower bound**, and R9.5
neither helped nor hurt it — it operated entirely below the scheduler, on layout.

## 10. The 6-IPB question, answered directly

1. **Does the current compiler achieve 6 IPB anywhere?** **Yes** — in the vector
   region of `reduction vi32`, at exactly **6.000**.
2. **Which vector kernels achieve ≥ 6?** Exactly one: `reduction vi32`.
3. **Which are closest?** conv3 vi8/vu8 (5.731), reduction vi16 (5.000), gemm
   vi32/vu32 (4.900), axpy vi8/vu8 (4.789), dot vi16/vu16 (4.786).
4. **Which cannot theoretically reach 6 under the current ISA?** Every kernel
   whose oracle ceiling is below 6: **axpy (5.25 / 5.00), dot (5.25 / 5.00),
   gemm vi8/vu8 (5.60), elementwise vi8/vu8 and reduction vi8/vu8 (5.667), conv3
   vi8/vu8 (5.50), scalar divmod (5.50)**. For these, 6 IPB is unreachable
   without an ISA change, so it is not a valid target.
5. **Highest vector-region IPB currently achieved:** **6.000**.
6. **Whole-program maximum:** **3.506** (gemm vi32).
7. **Whole-program median:** **1.663**.
8. **Weighted suite-wide IPB:** **2.213**.

**These are three different numbers and must not be substituted for one another.**
The whole-program figure is diluted by scalar init loops, which are 82–96% of
dynamic bundles in several kernels; the vector-region figure describes only the
vectorized inner loops.

## 11. Performance vs IPB — where higher IPB is NOT better

The AXPY lesson reproduces exactly in the current data:

| comparison | vector IPB | ticks | verdict |
|---|---|---|---|
| **axpy vi16** | 1.571 | **766** | **3.0× lower IPB, yet FASTER** |
| **axpy vi8** | **4.789** | 823 | higher IPB, 7.4% slower |
| gemm vi16 | 3.625 | 4 375 | same ticks... |
| gemm vi8 | 2.429 | 4 375 | ...at 1.5× lower IPB |
| conv3 vi16 | 1.800 | 534 | |
| conv3 vi8 | 5.731 | **415** | here higher IPB *is* faster |

`axpy vi8` issues three times as many instructions per bundle as `axpy vi16` and
takes 7.4% longer to do the same 64-element job, because the vi8 realisation
executes more instructions in total. `gemm vi16` and `gemm vi8` finish in exactly
the same 4 375 ticks with IPBs of 3.625 and 2.429.

The three concepts must stay distinct:

* **density** — instructions per real bundle (how full the VLIW word is);
* **throughput** — instructions per tick (density × how few empty bundles run);
* **execution time** — ticks, the only one that is a performance claim.

A compiler that emits more instructions for the same work raises density and
lowers performance. **Ticks per output element is the metric to optimise**; IPB
is a diagnostic.

## 12. Final recommendation

Selecting among the three offered conclusions:

**C. IPB ≥ 6 IS NOT A MEANINGFUL OPTIMIZATION TARGET FOR THE REMAINING KERNELS.**

Grounds, all measured:

1. **For most kernels 6 is unreachable by construction** — the R3.0 oracle
   ceiling is 5.00–5.67 for axpy, dot, gemm vi8/vu8, elementwise vi8/vu8,
   reduction vi8/vu8 and conv3 vi8/vu8. Targeting 6 there would require an ISA
   change, which is out of scope.
2. **Where 6 is reachable, it is already reached or nearly so** — reduction vi32
   is at 6.000 = 100% of ceiling; conv3 vi8/vu8 are at 104%.
3. **The kernels below their ceiling are dependence-bound, not slack-bound** —
   30 of 32 hot blocks are limited by dependence height and 20 sit exactly on
   that bound. Raising their IPB means removing dependence edges, which is what
   R9.1/R9.3 already harvested; §8 shows the residual memory-dependence cause is
   down to 2.1%.
4. **The largest remaining empty-slot cause is not convertible into ticks** —
   `waiting-for-address-alu` is 48.5% of vector empty slots, but with blocks at
   their dependence bound, filling those slots removes no bundle, and much of the
   work is ISA-mandated (no scaled-index addressing mode).
5. **Chasing IPB directly is measurably counterproductive here** — §11 gives a
   live example where 3× the IPB is 7.4% slower.

No single further optimization is therefore proposed. If work continues, the
honest target is **ticks per output element**, and the ranked candidate list from
`R9_4_POST_R9_3_FINAL_ANALYSIS.md` §12 still stands — with the top item now
delivered by R9.5.

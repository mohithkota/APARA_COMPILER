# R9.4 — Post-R9.3 bottleneck analysis

**ANALYSIS ONLY. No compiler source was modified.** Measured on `6e0f738`
(`r9.3-verified`), against `50e2b67` (`r9.2-verified`) rebuilt from
`git archive HEAD` as an independent reference tree.

Instruments used are the existing ones: `vector_backend/occupancy.py`
(`pack_with_attribution`, verified identical to the real bundler),
`vector_backend/ilp_analysis.py` (`production_codegen`, `label_frequencies`),
`bundler._annotate_memrefs` / `_conflicts_with_stores` for R6.2 edges, and the
real `mcode_align` / `mcode_assemble` / `mcode_run` toolchain.

---

## 1. Executive summary

**R9.3 did what it was designed to do and, in doing so, changed which resource
binds.** Memory dependence — the thing R9.3 attacked — has essentially vanished
from the vector regions (42 479 → 1 519 empty slots, −96%). Vector-region
occupancy went 29.4% → 45.7% and dynamic vector bundles halved.

**The dominant remaining cost is not in the compiler's dependence graph at all —
it is bundle-alignment padding.** 35.0% of all bundles in the shipped images are
`pad_*` bundles of `$null`, and they are reachable and executed. A direct
experiment on the real simulator — rewriting the compiler's own emitted bundles
to full width, changing nothing in the compiler — removes **every** pad bundle
and cuts **suite ticks 108 960 → 67 178 (−38.3%)**, with all 38 kernels
improving (29.0%–52.9%) and every PostCondition still passing.

That is roughly twice the size of R9.3's own gain, it affects every kernel
including the scalar ones, it needs no ISA change, and **no pass in the compiler
models it**.

Everything else is small or closed: register pressure is not a limiter (zero
spills, zero WAW-caused empty slots), memory dependence is spent, and 20 of 32
hot vector blocks sit exactly on their dependence lower bound.

**Conclusion: `IMPLEMENT NEXT OPTIMIZATION: alignment-aware bundle formation`.**

## 2. R9.2 → R9.3 comparison (measured, both arms)

Frequency-weighted, 39 kernels (38 suite + matmul16):

| | R9.2 | R9.3 | |
|---|---|---|---|
| **whole-program bundles** | 80 886 | 64 534 | −20.2% |
| whole-program occupancy | 23.7% | 26.1% | |
| whole-program IPB | 1.896 | 2.092 | |
| **vector-region bundles** | 32 858 | 16 474 | **−49.9%** |
| vector-region occupancy | 29.4% | **45.7%** | |
| vector-region IPB | 2.349 | **3.659** | |
| vector-region empty slots | 185 682 | 71 506 | −61.5% |

Vector-region empty slots by cause:

| cause | R9.2 | R9.3 | delta |
|---|---|---|---|
| waiting-for-address-alu | 22 926 | **34 702** | **+11 776** |
| waiting-for-vector-alu | 49 730 | 9 282 | −40 448 |
| waiting-for-vector-load | 47 411 | 9 267 | −38 144 |
| **memory-dependence** | 42 479 | **1 519** | **−40 960** |
| waiting-for-vector-multiply | 10 560 | 6 464 | −4 096 |
| region-boundary-label | 11 700 | 8 372 | −3 328 |
| memory-lanes-full | 94 | 1 118 | +1 024 |
| others | 782 | 782 | 0 |

GEMM hot blocks, R9.2 → R9.3:

| kernel | instrs | bundles | height | height(reg) | mem edges | addr instrs |
|---|---|---|---|---|---|---|
| gemm vi16 / vu16 / matmul16 | 37→29 | 16→**8** | 16→8 | 11→8 | 17→**0** | 15→7 |
| gemm vi32 / vu32 | 69→49 | 28→**10** | 28→10 | 19→9 | 105→**16** | 27→7 |
| gemm vi8 / vu8 | 18→17 | 9→**7** | 9→7 | 6→7 | 2→**0** | 6→5 |

## 3. Whole-program attribution (dynamic, exhaustive)

64 534 bundles · 516 272 issue slots · 134 993 occupied · **381 279 empty** ·
occupancy 26.1% · IPB 2.092.
Causes sum to 381 279 exactly — the partition is exhaustive by construction, not
by a residual bucket.

| cause | empty slots | % |
|---|---|---|
| waiting-for-address-alu | 133 086 | **34.9%** |
| region-boundary-label | 97 107 | 25.5% |
| region-boundary-control | 53 256 | 14.0% |
| waiting-for-scalar-load | 23 559 | 6.2% |
| memory-dependence | 23 223 | 6.1% |
| waiting-for-scalar-alu | 15 906 | 4.2% |
| waiting-for-vector-alu | 9 282 | 2.4% |
| waiting-for-vector-load | 9 267 | 2.4% |
| store-ordering | 8 334 | 2.2% |
| waiting-for-vector-multiply | 6 464 | 1.7% |
| memory-lanes-full | 1 118 | 0.3% |
| divide-lane-full | 441 | 0.1% |
| no-ready-instruction | 234 | 0.1% |
| waiting-for-reduction | 2 | 0.0% |

`padding` is **not** a category here, and cannot be: occupancy analyses the
bundles the compiler emits, whereas pads are inserted afterwards by
`mcode_align`. That is precisely why it went unseen until §5.

## 4. Vector-region attribution (dynamic)

16 474 bundles · 131 792 slots · 60 286 occupied · **71 506 empty** ·
occupancy 45.7% · IPB 3.659.

| cause | empty slots | % |
|---|---|---|
| waiting-for-address-alu | 34 702 | **48.5%** |
| waiting-for-vector-alu | 9 282 | 13.0% |
| waiting-for-vector-load | 9 267 | 13.0% |
| region-boundary-label | 8 372 | 11.7% |
| waiting-for-vector-multiply | 6 464 | 9.0% |
| memory-dependence | 1 519 | 2.1% |
| memory-lanes-full | 1 118 | 1.6% |
| waiting-for-scalar-load | 721 | 1.0% |
| store-ordering / reduction | 61 | 0.1% |

## 5. Padding analysis — the dominant remaining cost

### Mechanism (verified, not assumed)
`mcode_align` rounds every bundle up to a power-of-two slot count (1/2/4/8) and
requires it to start at `PC ≡ 0 (mod size)`. Checked on gemm vi16: **38 labelled
bundles, 0 violations of `pc % size == 0`.** Bridging to the next alignment costs
up to **three consecutive pad bundles** (observed run lengths 1, 2 and 3), and
**each pad bundle costs one tick**.

### Cost
| | |
|---|---|
| static pad bundles | **1 080 / 3 084 = 35.0%** |
| executed pad bundles (frequency-weighted) | **34 507 / 95 039 = 36.3%** |
| estimator vs measured ticks | 95 039 / 108 960 = 0.87 |

The 0.87 ratio means the frequency model accounts for most of the real tick
count, so unlike `R9_2_DELIVERY.md` §7 — which explicitly could not establish
reachability — **these pads are established as executed.** Worst offenders by
executed pad share: scalar bubblesort 50.5%, reduction vi8 49.0%, conv3 vi8
48.3%, reduction vu16/vu32 47.3%.

Independently confirmed on matmul16's hot block by exact trip count: 13 aligned
bundles per iteration of which 5 are pad, ×256 iterations = 1 280 pad ticks.

### Is it compiler-controllable? — direct experiment
Every bundle in each kernel's shipped pre-align mcode was rewritten to exactly 8
slots with `$null` filler. **No compiler source was changed** — only emitted
text — and the identical `mcode_align → mcode_assemble → mcode_run` sequence was
run.

| | baseline | width-8 |
|---|---|---|
| gemm vi16 aligned bundles | 80 | **54** |
| gemm vi16 pad bundles | 26 | **0** |
| gemm vi16 ticks | 7 037 | **4 366 (−38.0%)** |
| PostConditions | 3/3 pass | **3/3 pass** |

Across all 38 kernels: **ticks 108 960 → 67 178 (−38.3%), every kernel improved
(29.0% – 52.9%), every PostCondition passes, zero failures.** IMEM cost is
+11.1% (14 424 → 16 032 words); the largest program is 640 words against the
2048-word limit, so there is ample headroom.

Width-8 is a deliberately crude upper bound, not the proposed implementation —
it shows the size of the prize and proves the mechanism, and a real pass would
choose sizes to trade bundle count against pad count.

## 6. Address-generation analysis

Address work in hot vector blocks: **742 → 676 instructions (−8.9%)**; share of
hot-block instructions 43.5% → 41.2%. R9.3 removed address *instructions* in
GEMM (15→7, 27→7) but the suite-wide share barely moved, and
`waiting-for-address-alu` **rose** to 48.5% of vector empty slots — it is now the
largest single cause.

Shapes actually emitted across six hot blocks: base+constant 9, scaled index
`<<` 8, base+index (`reg+reg`) 7.

**How much is ISA-mandated:** APARA addresses memory as `[reg + reg]` or
`[reg + imm]` only — there is **no scaled-index addressing mode**. Every
`element_index → byte_offset` conversion therefore requires an explicit `<<`,
and every non-constant offset an explicit `+`. That portion cannot be removed by
any compiler without an ISA change.

**Crucially, this is a share of EMPTY SLOTS, not of ticks.** 20 of 32 hot vector
blocks are exactly at their dependence lower bound (§9), so filling those empty
slots cannot remove a bundle. Address work only costs ticks where it lengthens
the dependence *chain* — which is what R9.3 already harvested in GEMM.

## 7. Memory-dependence analysis

| | R9.2 | R9.3 |
|---|---|---|
| conservative alias edges, hot blocks (sum) | 1 013 | 780 (−23.0%) |
| GEMM hot-block edges | 17 / 105 / 2 | **0 / 16 / 0** |
| vector-region empty slots, memory-dependence | 42 479 | **1 519 (−96.4%)** |
| store-ordering empty slots | 59 | 59 |

Memory dependence is **spent as an optimization target**: it now accounts for
2.1% of vector-region empty slots. The 780 residual edges are concentrated in
non-GEMM kernels and are largely genuine (cross-array accesses through two opaque
base registers, which R6.2 is block-local by design and cannot resolve).

## 8. Register-pressure analysis

| | value |
|---|---|
| kernels with `cg.spilled` | **0 / 39** |
| kernels with `cg.spilled_to_memory` | **0 / 39** |
| rematerialization real spills | 0 |
| rematerialization evictions avoided | 0 |
| empty slots caused by `register-pressure` (WAW), vector dynamic | **0** |
| matmul16 hot block registers | 25 / 28 (was 26/28 pre-R9.3) |

**Register pressure is not a performance limiter after R9.3**, and R9.3 slightly
improved it. This re-confirms `R9_3_RAW_AND_REDUNDANCY_ANALYSIS.md` §1 on the
new code.

## 9. Dependence and resource lower bounds

For every hot vector block: shipped bundles vs dependence height vs
`ceil(instrs/8)` vs `ceil(mem_ops/4)`.

* **The binding resource is dependence height in 30 of 32 blocks** (issue width
  binds only in reduction vi16/vi32). Memory lanes never bind.
* **20 of 32 hot blocks ship exactly at the dependence lower bound.**
* The 12 that do not carry **34 bundles of total slack**:

| block | shipped | height | slack |
|---|---|---|---|
| dot vi16 / vu16 | 28 | 20 | **8** each |
| elementwise vi8 / vu8 | 19 | 16 | 3 each |
| axpy vi8 / vu8, dot vi8 / vu8 | 19 / 20 | 17 / 18 | 2 each |
| reduction vi16 / vi32, elementwise vi32 / vu32 | 4 / 8 | 3 / 7 | 1 each |

So the answer to the central question is: **the production bundler is still at
the lower bound for the large majority of hot blocks, including every GEMM
block R9.3 touched.**

## 10. Ranked remaining opportunities

| rank | opportunity | empty-slot share | measured tick potential | kernels | complexity |
|---|---|---|---|---|---|
| **1** | **alignment-aware bundle formation** | n/a (invisible to slot accounting) | **−38.3% suite, measured end-to-end** | **38/38** | medium — bundler-side size selection |
| 2 | scheduler slack on 12 blocks | — | ≤34 bundles × frequency; small | 12 | medium-high |
| 3 | address-chain shortening beyond R9.3 | 48.5% of vector empty slots | near zero — blocks are at the dependence bound | few | high |
| 4 | residual memory dependence | 2.1% | small | non-GEMM | high (R6.2 is block-local by design) |
| 5 | register pressure | 0% | none | none | n/a |

## 11. Rejected opportunities

* **`waiting-for-address-alu` (48.5% of vector empty slots)** — rejected as a
  primary target despite being the largest cause. Empty slots are not ticks: the
  blocks are already at their dependence lower bound, so filling slots removes no
  bundle. A large share is ISA-mandated (no scaled-index addressing mode). This
  is exactly the trap §9 of the brief warns about.
* **`region-boundary-label` + `region-boundary-control` (39.5% whole-program)** —
  structural: the bundler does not schedule across basic blocks. R3.2 superblock
  formation already addresses this and is already selected in every hot tier
  (`IVSR+LICM+loop-reg+superblock`).
* **Memory dependence** — reduced 96% by R9.3; residual is genuine cross-array
  ambiguity that a block-local analysis cannot resolve.
* **Register allocation / renaming / live-range splitting** — zero spills, zero
  WAW-caused empty slots. Nothing to fix.
* **Local value numbering (R9.3 lead #1)** — already measured cycle-neutral
  (−4.63% dynamic instructions, **0 ticks**). See `R9_3_LOCAL_GVN_WIP.md`.
* **Scheduler slack** — real but small (34 bundles), and R6.5 previously failed
  to beat the shipped schedule with 12 000 random legal reorderings. Kept as
  rank 2, not recommended now.

## 12. Final recommendation

**Alignment-aware bundle formation** is the only candidate that satisfies all
seven survival conditions:

1. **≥2 kernels / one dominant kernel** — 38 of 38, plus matmul16.
2. **Measured evidence** — 35.0% of static bundles and 36.3% of executed bundles
   are pads; mechanism verified (`pc % size == 0`, 0 violations).
3. **Reduces executed bundles/ticks** — measured **−38.3% suite ticks** on the
   real simulator, end to end.
4. **No ISA change** — uses `$null`, already emitted by the compiler; the
   experiment ran on the unmodified toolchain.
5. **Not IPB inflation** — instruction count is unchanged; bundle count falls, so
   ticks genuinely fall. (The opposite failure mode of `R9_3_LOCAL_GVN_WIP.md`.)
6. **Not covered by an existing pass** — `R9_2_DELIVERY.md` §7 already recorded
   that *no pass models bundle alignment*; the scheduler and bundler optimise
   SOURCE bundles.
7. **Validatable with the existing framework** — the experiment already used the
   38-program suite with full PostCondition checking; all 38 passed.

Caveats that must travel with the number: the −38.3% comes from the crude
width-8 upper bound, which costs +11.1% IMEM; a real pass must choose sizes
rather than maximise them, and its gain will land somewhere below that bound.
The measurement also assumes the aligner and simulator behave as observed here,
which they did on all 38 kernels.

IMPLEMENT NEXT OPTIMIZATION: alignment-aware bundle formation

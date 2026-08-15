# R9.5 — Alignment-aware bundle formation

Baseline: `6e0f738` (`r9.3-verified`). Kill switch **`APARA_NO_ALIGN_BUNDLE=1`**
reproduces that baseline **byte-for-byte** (verified on matmul16's full mcode
against a `git archive HEAD` reference tree).

**Headline: suite ticks 108 960 → 67 689 (−37.88%). All 38 kernels improve, 0
regress. Executed pad bundles −98.2%. IMEM +4.4%.**

---

## 1. The existing alignment mechanism

`mcode_align` assigns every bundle a **capacity** and then an address:

* capacity = **8** if the bundle holds a CTI (`?`-branch / `$call` / `$return`),
  a load/store, a divide or `$fsqrt` — it must be a "full bundle";
  otherwise the instruction count rounded up to **1 / 2 / 4 / 8**;
* the bundle is placed at the next address that is a **multiple of its
  capacity**, and the gap is filled with `pad_*` bundles of `$null`.

This rule is not new to R9.5 — it is already documented and implemented in
`bundler.resolve_code_labels` / `_bundle_capacity`, which the compiler uses as a
linker to compute label addresses. **R9.5 reuses that existing model rather than
duplicating it.**

Verified in Phase 1 on gemm vi16: 38 labelled bundles, **0 violations** of
`pc % capacity == 0`. Pad runs bridge a gap `g` with **popcount(g) pad bundles**
(g=7 → sizes 1,2,4; g=6 → 2,4; g=4 → 4), and **each pad bundle issues, costing a
tick**.

Per-pad accounting for gemm vi16 (12 pad runs, 26 pad bundles):

| run | pads | sizes | pc before | target capacity | target label | frequency |
|---|---|---|---|---|---|---|
| 2 | 3 | 1,2,4 | 0x41 | 8 | (mid-block) | **257** |
| 7 | 2 | 2,4 | 0xda | 8 | (mid-block) | **272** |
| 8 | 2 | 2,4 | 0xfa | 8 | (mid-block) | **256** |
| 9 | 3 | 1,2,4 | 0x109 | 8 | (mid-block) | **256** |
| 1 | 3 | 1,2,4 | 0x11 | 8 | `main` | 1 |
| 4 | 1 | 4 | 0x8c | 8 | `fc_5` | 17 |

The expensive runs are **mid-block** — pads before a *label* are skipped when the
block is entered by a branch, but a pad in the middle of straight-line code
executes every time.

## 2. The new alignment-aware decision

**Only bundles whose capacity is below 8 can misalign the stream.** A capacity-8
bundle placed at a multiple of 8 leaves the address a multiple of 8, so **a run
of capacity-8 bundles is self-aligning and needs no padding at all**. A small
pure-ALU bundle (1–4 instructions, no memory or control) dropped into such a run
knocks the address off the boundary, and every following full bundle then pays up
to three pad bundles.

R9.5 therefore makes one narrow decision, in `bundler._align_aware_widen`, on the
final bundle list:

> **inside a loop, pad a sub-capacity bundle out to 8 slots with `$null`.**

* nothing is reordered, repacked or rescheduled;
* no hazard, lane, dependence or legality rule is consulted or changed;
* no instruction is added or removed — `$null` is architecturally a no-op, and
  the bundle's real instructions and their order are untouched;
* straight-line code outside loops is left alone, because its pads execute once
  and the IMEM would be spent for nothing.

Loop membership is structural: a bundle is in a loop if some later bundle
branches to a label at or before it (`_loop_depth`). No profile is needed, and
none exists at that stage of the pipeline.

The pass runs at the end of `bundle_mcode`, so the layout `resolve_code_labels`
subsequently computes — and therefore the addresses it patches — is the one that
ships.

**Dependence legality, register allocation, memory ordering and control flow are
untouched by construction**: the pass runs after codegen and after packing, and
its only edit is appending `$null` to an existing bundle.

## 3. Candidate cost model

Primary objective is **executed pad bundles**, not IPB. Candidates were generated
by minimum loop depth to widen and each was run through the **real**
`mcode_align → mcode_assemble → mcode_run`:

| candidate | ticks | bundles | pads | IMEM | Δticks | ΔIMEM |
|---|---|---|---|---|---|---|
| production (no widening) | 7 037 | 80 | 26 | 392 | — | — |
| conservative (depth ≥ 2) | 5 261 | 76 | 22 | 408 | −25.2% | +4.1% |
| **moderate (depth ≥ 1) — shipped** | **4 375** | **66** | **12** | **408** | **−37.8%** | **+4.1%** |
| aggressive (all bundles) | 4 366 | 54 | 0 | 432 | −38.0% | +10.2% |

Widening *everything* buys 0.2 further points for 2.5× the IMEM. The minimum-pad
candidate is therefore **not** the best, exactly as Phase 4 warned; the shipped
choice is minimum-dynamic-bundle at controlled size.

## 4. matmul16 before/after

| metric | R9.3 | R9.5 |
|---|---|---|
| **simulator ticks** | 7 043 | **4 381 (−37.8%)** |
| dynamic instructions | 12 034 | 12 034 (unchanged) |
| static bundles (source) | 58 | 58 (unchanged) |
| static instructions | 132 | 132 (unchanged) |
| dynamic IPB (non-null / tick) | 1.709 | 2.747 |
| PostConditions vs gcc | 4/4 | **4/4** |

Real bundles and instruction counts are **identical** — the entire gain is pad
bundles that no longer execute.

## 5. GEMM corpus results

| kernel | R9.3 ticks | R9.5 ticks | Δ% |
|---|---|---|---|
| gemm vu16 | 7 805 | 4 631 | **−40.7%** |
| gemm vi16 | 7 037 | 4 375 | −37.8% |
| gemm vi8 | 7 037 | 4 375 | −37.8% |
| gemm vu8 | 7 293 | 4 631 | −36.5% |
| gemm vu32 | 7 811 | 5 149 | −34.1% |
| gemm vi32 | 7 043 | 4 893 | −30.5% |

## 6. Other vector kernels — the effect is general, not GEMM-specific

| kernel | Δ% | | kernel | Δ% |
|---|---|---|---|---|
| reduction vi8 | **−51.5%** | | conv3 vi8 | **−51.2%** |
| axpy vi8 | −46.3% | | reduction vu16/vu32 | −46.0% |
| reduction vi32 | −45.8% | | scalar divmod | −45.3% |
| reduction vi16 | −45.3% | | elementwise vi8 | −44.5% |
| dot vi8 | −44.4% | | conv3 vu16 | −42.9% |
| axpy vu16 | −42.6% | | scalar bubblesort | −37.2% |
| dot vi32 | −28.5% | | dot vu32 | −28.6% |

**All 38 kernels improve, including the two deliberately-scalar controls** —
padding was never a vector-specific problem.

## 7. Pad reduction

| | R9.3 | R9.5 |
|---|---|---|
| static bundles | 3 084 | 2 629 |
| static pad bundles | 1 080 | **625 (−42.1%)** |
| **executed pad bundles (est.)** | **34 507** | **625 (−98.2%)** |

The surviving 625 pads are almost entirely outside loops, where they execute once
— which is precisely the design intent. Static pads fall 42% while *executed*
pads fall 98%.

## 8. Tick reduction

**Suite 108 960 → 67 689 ticks, −37.88%.** Dynamic instruction count is
**identical (+0.00%)**, which is the cleanest possible evidence that nothing was
added, removed or re-scheduled: only bundles that used to issue `$null` no longer
do.

## 9. IMEM impact

| | words | |
|---|---|---|
| R9.3 | 14 424 | |
| R9.5 | 15 056 | **+4.4%** |
| R9.4 width-8 upper bound | 16 032 | +11.1% |

Largest single program after R9.5: **592 words against the 2 048-word limit**, so
there is ample headroom. R9.5 captures **98.9% of the theoretical tick benefit
(−37.88% of −38.3%) at 40% of the IMEM cost.**

## 10. IPB impact — reported, not chased

IPB was not the objective and must be read carefully here: the widened bundles
contain `$null`, so any *slot-occupancy* IPB would be meaningless. The figures
below are the honest ones — **non-null instructions per tick**, from the
simulator's own counters:

| kernel | R9.3 | R9.5 |
|---|---|---|
| matmul16 | 1.709 | 2.747 |
| gemm vi32 | 2.435 | 3.506 |
| reduction vu16 | 1.338 | 2.476 |
| conv3 vi8 | 0.728 | 1.492 |

IPB rose everywhere purely because the denominator (ticks) fell while the
numerator (real instructions) is unchanged. It is a *consequence* of the speedup,
not its cause.

## 11. Correctness

| check | result |
|---|---|
| 38-program simulator suite | **38/38 PASS** |
| negative controls | **3/3 rejected** |
| unit suites | **21/21 PASS** |
| `pipeline_crosscheck` | **PASS — 124/124** identical |
| matmul16 vs gcc golden | **4/4 PostConditions** |
| new spills | **none** — 0/7 GEMM+matmul16, both arms |
| kill switch `APARA_NO_ALIGN_BUNDLE=1` | **byte-identical to R9.3** |
| dynamic instruction count | **unchanged (+0.00%)** |

`pipeline_crosscheck` passing 124/124 is expected and meaningful here: it
compares IR and generated code *before* bundling, so it confirms R9.5 changed
nothing upstream of the bundler.

## 12. Final recommendation

Every success criterion is met:

1. **executed padding reduced** — 34 507 → 625 (−98.2%);
2. **ticks improve** — 108 960 → 67 689 (−37.88%);
3. **no kernel regresses** — 38 improved, 0 regressed;
4. **real bundle count unchanged** — static bundles 2 004 → 2 004, dynamic
   instructions +0.00%;
5. **IMEM growth controlled** — +4.4%, largest program 592 of 2 048 words;
6. **survives the real alignment stage** — every number above is from
   `mcode_align` → `mcode_assemble` → `mcode_run`;
7. **the shortfall against the zero-padding bound is a genuine tradeoff, not an
   artifact** — the 0.4-point gap is exactly the cold-code pads R9.5 deliberately
   leaves in place, and closing it costs 2.5× the IMEM (§3).

**Recommendation: ship.** The obvious refinement — an exact DP over `pc mod 8`
choosing per-bundle widths instead of the loop-membership rule — was prototyped
and is not worth it: the loop rule already captures 98.9% of the achievable
benefit, and the residual pads are in code that executes once.

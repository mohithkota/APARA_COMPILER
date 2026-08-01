# R6.4.1 — Adaptive Unroll-Factor Selection

**The unroll factor is now chosen per module by measurement instead of being
fixed at 4×. It picks the simulator-fastest factor on 8 of 8 kernels tested, and
eliminates every regression the fixed factor caused.**

Suite ticks **210 359 (1×) → 138 014 (fixed 4×) → 136 847 (adaptive)**, −34.9%
against no unrolling. 38/38 verification maintained.

---

## 1. Why a fixed factor was not enough

R6.4 adopted 4× on an aggregate: best total across the suite. Per kernel the
picture was worse — four 16-bit kernels regressed 6–16%. Measuring each kernel at
every factor shows why no single value can win:

| kernel | 1× | 2× | 4× | 8× | best |
|---|---|---|---|---|---|
| axpy vi16 | **1550** | 1792 | 1792 | 1792 | **1×** |
| axpy vu16 | **1806** | 1920 | 1920 | 1920 | **1×** |
| elementwise vi16 | 1678 | **1526** | 1858 | 1858 | **2×** |
| elementwise vu16 | 1806 | **1654** | 1922 | 1922 | **2×** |
| conv3 vi8 | **850** | 850 | 850 | 850 | 1× (tie) |
| dot vi8 | 1680 | 1656 | **1609** | 1609 | **4×** |
| gemm vi16 | 23 791 | 66 799 | **11 905** | 11 905 | **4×** |
| reduction vi32 | 884 | 722 | 690 | **631** | **8×** |

Every candidate factor is optimal for some kernel, and 2× remains pathological
for GEMM (it loses vectorization and falls back to scalar).

## 2. The objective — and validating it before trusting it

A selector needs a compile-time proxy for simulator ticks. The first one I tried
was wrong, and it is worth recording why:

| proxy | predicts the measured optimum |
|---|---|
| static bundle count | never — always prefers 1× (smallest code) |
| loop-body bundles × iterations, everything else once | **4 / 8** |
| **R6.1 frequency-weighted dynamic bundle count** | **8 / 8** |

The middle proxy fails because it treats *all* non-vector-loop bundles as
executing once, ignoring the other loops in the program — in these benchmarks a
64-iteration initialisation loop dominates, so the estimate was mostly error.

The working objective reuses R6.1 wholesale: `label_frequencies` supplies each
block's proved trip count and `occupancy.analyze_mcode` weights every emitted
bundle by it. **It was validated against the simulator on all eight kernels
before being allowed to choose anything.**

## 3. Design

In `vectorize_all_module`, each candidate factor (8, 4, 2, 1 — largest first, so
a tie keeps the larger) is built **through the same pipeline**, so each is
validated by the differential oracle exactly as before, and the lowest estimated
dynamic bundle count wins. A factor that loses vectorization is simply skipped,
which is how the 2× GEMM pathology is avoided without special-casing it. Any
exception in a candidate is caught and that candidate dropped, so the search can
never break a build. `APARA_VECTOR_UNROLL` pins the factor and skips the search.

No scheduler, bundler, legality, lowering or ISA change.

## 4. Results

**Per kernel, adaptive vs the best achievable:**

```
axpy vi16 1550   elementwise vi16 1526   gemm vi16 11905   reduction vi32  631
axpy vu16 1806   elementwise vu16 1654   dot vi8    1609   conv3 vi8       850
                                                        gap to optimum: +0.00%
```

**Adaptive vs fixed 4× (the kernels that differ):**

| kernel | fixed 4× | adaptive | change |
|---|---|---|---|
| elementwise vi16 | 1858 | 1526 | **−17.9%** |
| elementwise vu16 | 1922 | 1654 | **−13.9%** |
| axpy vi16 | 1792 | 1550 | **−13.5%** |
| reduction vi32 | 690 | 631 | −8.6% |
| axpy vu16 | 1920 | 1806 | −5.9% |
| dot vi16 | 1508 | 1432 | −5.0% |
| dot vu16 | 1636 | 1560 | −4.6% |

**No kernel is worse than under fixed 4×.** All four R6.4 regressions are gone.

Suite total: **136 847 ticks, −34.9% vs 1×, −0.85% vs fixed 4×.** The suite-level
delta is small because GEMM dominates the total and its factor did not change;
the per-kernel improvements are where the value is.

## 5. Cost

**Compile time roughly 5×** for a vectorized kernel: 0.281 s → 1.432 s, because
the module is built once per candidate factor. Suite verification went from ~40 s
to 123 s.

That is a real trade and it is the main argument against this design. Mitigations
not implemented: cache the estimate per kernel shape, search 4× and 1× only
(which would capture most of the gain), or stop early when a factor is clearly
worse.

## 6. Limitations

* **The choice is per MODULE, not per kernel.** A program with several vector
  loops of different shapes gets one factor for all of them. Every benchmark here
  has a single vector kernel, so the measurements do not exercise that case and
  do not justify the design for it.
* **The estimate is a model**, validated on eight kernels of four families. It is
  not guaranteed to track ticks on shapes unlike these; it selects, it does not
  prove.
* **The 2× GEMM pathology is still unexplained** — avoided by the search rather
  than understood.
* Ticks are the simulator's, not hardware cycles.

## 7. Regression

| check | result |
|---|---|
| simulator verification | **38/38 PASS**, negative controls reject |
| adaptive vs per-kernel optimum | **0.00% gap on 8/8** |
| `pipeline_crosscheck` | PASS 124/124 |
| unit suites (all 15) | all pass |

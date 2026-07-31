# R4.6.5 — Vector Performance Characterization

**Milestone:** R4.6.5 (evaluation only — no optimization performed).
**Status:** ✅ COMPLETE · **Date:** 2026-07-31 · **Tree:** `ba7cc98` (R4.6.1)

> Measurement only. Nothing in the scheduler, bundler, vectorizer, backend or IR
> was modified. Every number below comes from the REAL production path
> (tier-1 scalar optimizer → R3.2 superblock → codegen → bundler).

---

## 0. A measurement caveat that shapes every conclusion
**IPB is a density metric, not a throughput metric.** It counts instructions per
issued bundle. Vectorizing a loop *removes* instructions — one `$v` replaces eight
scalar operations — so a kernel can get dramatically faster while its IPB barely
moves. The R3.0 oracle's `theoretical_ipb` is also computed on the **scalar** IR,
so "distance to oracle" compares vector code against a scalar-derived ceiling.

Both numbers are reported, but the honest throughput figure is **dynamic
operations**, and that is where the vector work shows up: **−85.2%**.

## 1. Headline results (26 benchmarks, 7 families + 5 deliberately scalar)
```
  IPB (static instructions / bundle; 8 = APARA issue width)
    scalar compiler (all)              mean 1.873  peak 2.364  min 1.556
    vector compiler (all)              mean 2.462  peak 3.667  min 1.560
    vector compiler (vectorized only)  mean 2.598  peak 3.667  min 1.700
    non-vectorizable kernels           mean 1.890  peak 2.280   (unchanged, correct)
    oracle theoretical (scalar form)   mean 5.438  peak 6.750  min 3.750

    vector vs scalar        +31.5%
    vs oracle ceiling       45.3% utilization
    vs raw 8-wide issue     30.8%

  Dynamic operations (the throughput metric)
    aggregate           28544 -> 4231     -85.2%
    per kernel          mean -84.6%, range -56.1% .. -97.4%

  Static cost of that speed (vectorized kernels)
    bundles             537 -> 602        +12.1%
    code size         34717 -> 54643      +57.4%

  Occupancy (vectorized kernels, mean)
    bundle occupancy      0.325   (32.5% of the 8-wide issue)
    memory-lane occupancy 0.193   (19.3% of the 4 ld/st lanes)
    vector ops / bundle   0.104
    branch density        0.016

  Compile time            mean 0.18 s, max 0.95 s per benchmark
```

## 2. Coverage
```
    vectorized   21        rejected   5        rolled back   0
    total        26        of which deliberately non-vectorizable: 5
```
**Every benchmark that can be vectorized, is.** All 5 rejections are the control
group (bubble sort, gcd, binary search, popcount, divmod). **Zero rollbacks** —
no kernel was lowered and then thrown away.

## 3. IPB distribution (vector compiler)
```
       1.5-2.0 | #######  7
       2.0-2.5 | #######  7
       2.5-3.0 | #######  7
       3.0-4.0 | #####    5
         >=4.0 |          0
```
Nothing reaches 4 IPB. The ceiling is not being approached.

## 4. Gap analysis — every lost slot attributed to a MEASURED cause
Lost slots = `(theoretical_ipb − achieved_ipb) × bundles`, bucketed by the R3.0
oracle's own limiter classification for vectorized loops and by the pipeline's
recorded rejection reason for declined ones. Total: **2276 slots**.

| cause | slots | share | where |
|---|---|---|---|
| true data dependence | 1684 | **74.0%** | gemm vi16 166, gemm 8x8x32 141, gemm 16³ 125, conv 7-tap 112, conv vi16 105, reduction vi32 86, expr a+b+c+d 80, elementwise add 80 |
| non-vectorized loop | 332 | 14.6% | binsearch 143, divmod 111, popcount 44, gcd 34 |
| memory dependence | 200 | 8.8% | bubblesort 200 |
| remainder handling | 59 | 2.6% | axpy remainder 59 |
| register pressure | 0 | 0% | — |
| branch overhead | 0 | 0% | — |
| unsupported pattern | 0 | 0% | — |
| hardware restriction | 0 | 0% | — |

**Three facts follow directly from this table, and each is checkable in
`results/gap_detail.csv`:**

1. **74% of the remaining gap is true data dependence inside kernels that are
   ALREADY vectorized.** No vectorizer removes a recurrence; this is the
   dependence structure of dot/GEMM/convolution themselves.
2. **100% of the "non-vectorized loop" and "memory dependence" buckets (532 slots,
   23.4%) is the 5-kernel control group** — and not one of them is vectorizable by
   any vectorizer: binary search is data-dependent control flow, gcd and popcount
   are sequential recurrences, divmod is bounded by the single divide lane, and
   bubble sort has a loop-carried swap.
3. **Register pressure, branch overhead and unsupported patterns account for
   ZERO measured slots.** Nothing is being lost to compiler limitations that a
   new pass could address.

## 5. The decision: is R4.7 (General Loop Vectorizer) justified?

**No. The evidence does not support it.**

A General Loop Vectorizer can only attack the "non-vectorized loop" bucket —
**14.6%** of the gap by slot count. But every kernel in that bucket was measured
and is non-vectorizable for a *fundamental* reason, not a compiler one:

| kernel | slots | why no vectorizer helps |
|---|---|---|
| binary search | 143 | data-dependent control flow; each iteration's address depends on the previous comparison |
| divmod | 111 | APARA has **1 divide lane** (R4.0 capability DB) — a hardware restriction |
| popcount | 44 | sequential bit recurrence `x >>= 1` |
| gcd | 34 | sequential modulo recurrence |

**Realistically addressable share of the remaining gap: ~0%.**

Even granting a generous hypothetical — that a general vectorizer somehow
recovered *half* the entire non-vectorized bucket — mean IPB would move from
2.462 to roughly 2.6, about **+6%**, against the 74% of the gap it cannot touch
at all. Meanwhile the vector work already delivered **−85.2% dynamic operations**,
which is where the real performance is.

### Recommendation
**End feature development. Proceed to R5.0 (final thesis evaluation).**

If any further engineering were done, the measurements rank it as:
1. **Nothing** — no measured bucket justifies a new pass.
2. If forced to pick: **static code size** (+57.4% on vectorized kernels, bundles
   +12.1%) is the only metric that regressed, and it is a *deliberate* trade
   (unrolled chunks buy dynamic operations). It is a size problem, not an IPB one,
   and R4.2.5/R4.2.6/R4.4.5 already attacked it three times with diminishing
   returns.
3. **Remainder handling** is 2.6% of the gap — the smallest measured bucket, and
   R4.4.5 already halved it.

## 6. Answering the two questions this milestone existed to answer
**"How close is the compiler to the APARA issue-width limit?"**
Mean **2.462 IPB against an 8-wide machine = 30.8%**, or **45.3%** of the
scalar-derived oracle ceiling of 5.438. Peak observed 3.667.

**"What single optimization would provide the largest remaining gain?"**
None that is worth building. 74% of the remaining gap is intrinsic data
dependence, and 23.4% is kernels that are not vectorizable by any technique. The
largest *measured* opportunity a new pass could address is under 3%.

## 7. Infrastructure delivered
```
  evaluation/
    metrics.py   per-program measurement on the real production path
    runner.py    the 26-benchmark suite + CSV driver
    compare.py   scalar/vector/oracle comparison + gap classification
    report.py    text report + CSV summaries
    plots.py     dependency-free SVG + ASCII plots
    results/     benchmarks.csv, summary_by_family.csv,
                 gap_analysis.csv, gap_detail.csv, report.txt
    plots/       ipb_per_benchmark.svg, gap_causes.svg, ipb_distribution.txt
```
Reusable: `python3 evaluation/__main__.py` regenerates everything.

## 8. Honest limitations of this study
- **The oracle ceiling is scalar-derived** (§0). Comparing vector IPB to it
  overstates the gap for vectorized kernels. The dynamic-operation figure is the
  sound throughput measure and is reported alongside.
- **Dynamic operation counts come from the vectorizer's own model**, not a
  simulator (project policy is IR-level validation). They are exact for
  straight-line bodies and modelled for compact loops.
- **IPB here is static density.** A true dynamic IPB would need per-bundle
  execution counts from a simulator run, which this project does not invoke.
- **26 benchmarks** is a characterization suite, not a workload study; the 5
  scalar kernels are a deliberate control group, not a representative application
  mix.

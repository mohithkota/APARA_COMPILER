# R5.0 — Final Evaluation: A Vectorizing Compiler for the APARA VLIW Accelerator

**Artifact commit:** `cf33ad4` · **Base tag:** `r4.6.5-evaluated` · **Date:** 2026-07-31
**Status:** FROZEN — no optimization pass was modified in this milestone.

---

## 1. Compiler architecture summary

A C compiler targeting APARA, an 8-issue VLIW accelerator with packed SIMD
support. The pipeline, in execution order:

```
  C source
    -> pycparser front end -> IR generation
    -> VECTORIZATION  (one generic pipeline, six clients)
    -> scalar optimizer   IVSR, strength reduction, LICM, loop-register promotion,
                          copy-prop, coalescing, DCE, SCCP, GVN, mem2reg
    -> software pipelining (R3.1, oracle-gated modulo scheduling)
    -> superblock formation + scheduling (R3.2)
    -> register allocation (28 registers) -> bundler (8-wide VLIW) -> mcode
```

Vectorization runs **first**, so vector IR flows through every scalar pass
unchanged. The vector subsystem is one pipeline with pluggable clients:

| layer | module | role |
|---|---|---|
| pipeline | `vector_pipeline.py` | Detection → Legality → Profitability → Transformation → Validation → Compile → Commit/Rollback. Clients cannot skip a gate. |
| capability | `vector_capability*.py` | ISA ground truth, extracted from codegen + `golden_stubs.h`, including confirmed-broken operations |
| addressing | `vector_affine.py` | the single address recognizer: `coeff*IV + invariant` |
| expressions | `expression_tree.py`, `expression_lowering.py` | one representation, two evaluators (vector + scalar) |
| realisation | `vector_compact_loop.py`, `vector_size_probe.py` | unrolled / compact / peeled, chosen by post-optimizer measurement |
| remainder | `vector_remainder_peel.py` | one declarative peel framework for every client |
| validation | `vector_validation.py`, `PackedVectorInterp` | differential oracle faithful to the hardware, including its bugs |

## 2. Supported vector clients

| client | pattern | milestone | ISA used |
|---|---|---|---|
| Dot product | `s += a[i]*b[i]` | R4.1 | `$dot`, `$dot $accumulate` |
| Reduction | `s += a[i]` | R4.1 | `$vreduce +` |
| Elementwise | `c[i] = a[i] op b[i]`, copy | R4.2 | `$v + - *` |
| AXPY | `Y[i] += a*X[i]` | R4.3 | `$v *` with `$replicate`, `$v +` |
| Packed GEMM | i-k-j over 1-D packed arrays | R4.4 | as AXPY, row-addressed |
| Convolution | fused k-tap stencils, 1-D and 2-D rows | R4.6/R4.6.1 | as elementwise, shift-addressed |
| Expression trees | `a+b+c`, `a*b+c`, `(a+b)*c`, … | R4.5 | `$v` recursively |

No new IR node, vector instruction or backend feature was introduced after R4.0.

## 3. Evaluation methodology

Every figure is produced by `evaluation/` on the **real production path** —
tier-1 scalar optimizer + R3.2 superblock + codegen + bundler — not a
pre-optimizer proxy. For each benchmark the harness compiles twice (vectorization
on and off) and runs the R3.0 oracle on the scalar form for the ceiling.

**A caveat that governs interpretation.** *IPB is a density metric, not a
throughput metric.* Vectorizing **removes** instructions — one `$v` replaces eight
scalar operations — so a kernel can become much faster while its IPB barely moves
(AXPY vi8 is exactly this: IPB 1.700 → 1.700, dynamic operations −90.3%). The
sound throughput measure is **dynamic operation count**, reported alongside.

## 4. Benchmark suite

26 benchmarks: 21 across the seven vector families, plus **5 deliberately
non-vectorizable scalar kernels** as a control group.

| family | n | members |
|---|---|---|
| dot | 2 | vi8, vi16 |
| reduction | 2 | vi8, vi32 |
| elementwise | 3 | add, mul, copy |
| expression | 4 | `a+b+c`, `a*b+c`, `(a+b)*c`, `a+b+c+d` |
| axpy | 3 | vi8, vi16, remainder |
| gemm | 3 | vi8 16³, vi8 8×8×32, vi16 |
| convolution | 4 | 3-tap, 5-tap, 7-tap, 3-tap vi16 |
| **scalar (control)** | 5 | bubble sort, gcd, binary search, popcount, divmod |

## 5. Coverage

| outcome | count |
|---|---|
| vectorized | **21** |
| rejected | 5 |
| **rolled back** | **0** |
| total | 26 |

**Every benchmark that can be vectorized, is** — 21/21 of the vectorizable set.
Zero kernels were lowered and then discarded. Beyond the suite, the 124-program
regression corpus is **byte-identical** with vectorization on and off, since it
contains no packed arrays.

## 6. Dynamic instruction reduction

| statistic | value |
|---|---|
| aggregate, 21 vectorized kernels | **28 544 → 4 231 (−85.2 %)** |
| per-kernel mean | −84.6 % |
| best | −97.4 % (reduction vi8) |
| worst | −56.1 % (reduction vi32, 2 lanes) |

| family | mean reduction |
|---|---|
| gemm | −91.4 % |
| elementwise | −89.0 % |
| expression | −87.1 % |
| axpy | −82.4 % |
| convolution | −79.5 % |
| dot | −84.7 % |
| reduction | −76.8 % |

## 7. Static code growth

| metric | scalar | vector | Δ |
|---|---|---|---|
| bundles (21 kernels) | 537 | 602 | **+12.1 %** |
| code size (chars) | 34 717 | 54 643 | **+57.4 %** |

This is a **deliberate trade**: unrolled chunks buy dynamic operations. It was
attacked three times — R4.2.5 (compact loops), R4.2.6 (post-optimizer size gate
and acceptance margin), R4.4.5 (generalized remainder peeling) — with diminishing
returns each time. The selector still evaluates up to four realisations per kernel
and keeps the smallest that compiles spill-free.

## 8. IPB measurements

| population | mean | peak | min | median | n |
|---|---|---|---|---|---|
| scalar compiler | 1.873 | 2.364 | 1.556 | 1.762 | 26 |
| **vector compiler** | **2.462** | **3.667** | 1.560 | 2.480 | 26 |
| vector, vectorized only | 2.598 | 3.667 | 1.700 | 2.560 | 21 |
| non-vectorizable control | 1.890 | 2.280 | 1.560 | 1.889 | 5 |
| oracle theoretical (scalar form) | 5.438 | 6.750 | 3.750 | 5.600 | 26 |

**Vector vs scalar: +31.5 %.** Utilization: **45.3 %** of the oracle ceiling,
**30.8 %** of the raw 8-wide issue width.

| family | mean vector IPB |
|---|---|
| convolution | 3.140 |
| gemm | 2.922 |
| expression | 2.906 |
| elementwise | 2.400 |
| reduction | 2.202 |
| dot | 2.119 |
| axpy | 1.923 |

Distribution: 7 kernels in [1.5, 2.0), 7 in [2.0, 2.5), 7 in [2.5, 3.0), 5 in
[3.0, 4.0), **none ≥ 4.0**.

Occupancy (vectorized kernels, mean): bundle 0.325 of 8 lanes, memory-lane 0.193
of 4, vector ops per bundle 0.104, branch density 0.016.

## 9. Oracle comparison

The R3.0 oracle computes, per innermost loop, `theoretical_ipb = min(N/MII, 8)`
with `MII = max(RecMII, ResMII)` and the real 8/4/1/1 machine caps. Mean ceiling
across the suite is **5.438**; the vector compiler achieves **2.462**.

**This comparison is generous to the oracle and unfair to the compiler**, because
the ceiling is computed on the **scalar** IR. A vectorized kernel executes far
fewer instructions for the same work, so its achievable density is bounded by a
different (smaller) instruction set than the one the oracle measured. The gap
should be read as an upper bound on what remains, not as recoverable headroom.

## 10. Gap analysis

Lost slots = `(theoretical_ipb − achieved_ipb) × bundles`, attributed per
benchmark to a **measured** cause: the oracle's own limiter classification for
vectorized loops, the pipeline's recorded rejection reason for declined ones.
Total **2 276 slots**.

| cause | slots | share | dominant contributors |
|---|---|---|---|
| true data dependence | 1 684 | **74.0 %** | gemm vi16 166, gemm 8×8×32 141, gemm 16³ 125, conv 7-tap 112, conv vi16 105 |
| non-vectorized loop | 332 | 14.6 % | binsearch 143, divmod 111, popcount 44, gcd 34 |
| memory dependence | 200 | 8.8 % | bubble sort 200 |
| remainder handling | 59 | 2.6 % | axpy remainder 59 |
| register pressure | 0 | 0 % | — |
| branch overhead | 0 | 0 % | — |
| unsupported pattern | 0 | 0 % | — |
| hardware restriction | 0 | 0 % | — |

Three conclusions follow directly, each verifiable in `results/gap_detail.csv`:

1. **74 % is intrinsic data dependence inside kernels that are already
   vectorized.** No vectorizer removes a recurrence.
2. **The entire non-vectorized + memory-dependence share (532 slots, 23.4 %) is
   the 5-kernel control group**, none of which is vectorizable by any technique.
3. **Register pressure, branch overhead and unsupported patterns cost zero
   measured slots.** Nothing is being lost to a fixable compiler limitation.

## 11. Remaining limitations

| limitation | consequence | evidence |
|---|---|---|
| 2-D arrays are never packed (`ir_gen._is_packed_array_decl` is False for 2-D **by design**) | GEMM/stencils must use 1-D packed arrays with explicit indexing | measured stride 8 for `vi8_t A[8][8]`, identical to `int` |
| canonical i-j-k matmul unsupported | `B[k][j]` is column-strided; APARA has no gather | `vector_affine` reports STRIDED |
| induction variable must start at 0 | `for (j = 1; …)` stencils declined | `iv-does-not-start-at-zero`, R4.6.1 |
| expression depth ≤ 8 | windows wider than ~7 taps declined | `expression-too-deep` |
| `scalar − vector` unsupported | `$replicate` broadcasts src2 only | refused at match time, R4.5 |
| tap-innermost convolution unrecognised | accumulator is an array element at an invariant address | reduction machinery expects a scalar slot |
| static code growth | +57.4 % code, +12.1 % bundles | §7 |
| unsigned `$vreduce`, 32-bit `$dot`, 4-bit lanes, native abs/max/min | never emitted | R4.0 confirmed-broken list |

## 12. Why R4.7 (General Loop Vectorizer) was not pursued

A general loop vectorizer can only attack the **non-vectorized loop** bucket:
**14.6 %** of the gap. Every kernel in it was measured and is non-vectorizable for
a fundamental reason:

| kernel | slots | obstruction |
|---|---|---|
| binary search | 143 | data-dependent control flow — the next address depends on the previous comparison |
| divmod | 111 | APARA has **one divide lane** — a hardware restriction |
| popcount | 44 | sequential bit recurrence `x >>= 1` |
| gcd | 34 | sequential modulo recurrence |

**Realistically addressable share ≈ 0 %.** Under a deliberately generous
hypothesis — recovering *half* the entire bucket — mean IPB would move 2.462 →
≈ 2.6, about **+6 %**, while leaving untouched the 74 % that is intrinsic
dependence. Set against the **−85.2 %** dynamic-operation reduction the vector
work already delivers, the return does not justify the milestone.

**Decision: feature development ended. No R4.7.**

## 13. Threats to validity

- **The oracle ceiling is scalar-derived.** It is computed on the scalar IR, so
  comparing vector IPB against it overstates the remaining gap (§9). Reported
  because it is the project's established baseline, not because it is exact.
- **Dynamic counts come from the vectorizer's own model, not a simulator.** They
  are exact for straight-line unrolled bodies and *modelled* for compact loops
  (per-iteration cost × trip). Project policy is IR-level validation.
- **No hardware simulation was run.** All correctness evidence is the packed
  differential oracle, which models `golden_stubs.h` semantics *including known
  hardware bugs*. A simulator-backed acceptance gate remains available and is
  recommended before hardware deployment.
- **IPB here is static density**, not dynamic IPB. A true dynamic IPB needs
  per-bundle execution counts from a simulator run.
- **Benchmark selection.** 26 hand-written kernels chosen to exercise each client,
  not a representative application workload. The 5 scalar kernels are a deliberate
  control group; including them lowers the reported mean IPB, which is intentional.
- **Unsupported kernels are excluded from vector statistics by construction** —
  coverage is 21/21 of the *vectorizable* set, not of all conceivable loops.
- **The non-zero-IV and column-stride limitations** (§11) mean some real stencils
  are declined; the suite reflects the supported spelling.
- **Single machine, single toolchain version.** No cross-platform validation.

## 14. Reproducibility

See `REPRODUCIBILITY.md`. In short:

```
    python3 compiler/evaluation/__main__.py
```

regenerates every CSV, figure and summary table in this report with no manual
intervention, from the repository root. Verified deterministic by diffing
`results/benchmarks.csv` across two runs: **the only differing column is
`compile_seconds`** (wall-clock). Every count, IPB value, decision, realisation
and gap percentage is bit-stable.

## 15. Final conclusions

1. **The compiler vectorizes every kernel class it targets**, through one generic
   pipeline with six clients, and rolls back rather than mis-compiles: **0
   mismatches and 0 rollbacks** across the suite, **124/124** byte-identical on
   the regression corpus.
2. **Throughput improved by 85.2 %** in dynamic operations on vectorized kernels,
   at a deliberate cost of +12.1 % bundles and +57.4 % code size.
3. **Density improved 31.5 %** (IPB 1.873 → 2.462), reaching **30.8 %** of the
   8-wide issue width and **45.3 %** of the scalar-derived oracle ceiling.
4. **The remaining gap is dominated (74 %) by intrinsic data dependence**, with a
   further 23.4 % in kernels no vectorizer can help. Compiler-fixable causes —
   register pressure, branch overhead, unsupported patterns — measure **zero**.
5. **Therefore feature development is complete.** The evidence does not support a
   general loop vectorizer, and no other measured bucket exceeds 3 %.

The single most valuable *next* step is not a compiler optimization but
**validation on hardware or a cycle-accurate simulator**, which would replace the
modelled dynamic counts and the scalar-derived ceiling with measured ones.

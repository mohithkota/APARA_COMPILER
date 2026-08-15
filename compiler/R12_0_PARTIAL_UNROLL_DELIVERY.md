# R12.0 — Partial unrolling for vector loops

Branched from **`r11-verified`** (`92c5c45`). `r10-final` untouched.

**Outcome: STOPPED at Phase 7 by the milestone's own stop condition. No compiler
source was changed.**

The milestone asked one question:

> *Can intermediate unroll factors remove the measured GEMM vi32 M=32
> realization cliff without introducing unacceptable register pressure or
> correctness risk?*

**Answer: intermediate unroll factors already exist and are already chosen
adaptively. At the target configuration they are rejected by the differential
oracle as producing WRONG RESULTS — so the cliff is not a missing feature, it is
a latent correctness defect in the existing partial-unroll path at 16 chunks.**

---

## 1. Phase 1 — the premise did not hold

The brief states the compiler has "effectively two realizations: compact and
fully unrolled". Tracing the code shows three levels, and the middle one is
already adaptive:

| what | where | status |
|---|---|---|
| partial unrolling of the chunk loop | `vector_compact_loop.build_compact_chunk_loop` | **exists** — emits U chunks per iteration (R6.4) |
| the factor U | `vector_compact_loop.unroll_factor` | default 4, must divide `chunks`, so no tail is introduced |
| **adaptive per-module choice of U** | `dot_vectorizer.vectorize_all_module` | **exists** (R6.4.1) — searches `_UNROLL_CANDIDATES = (8, 4, 2, 1)` |
| selection metric | `dot_vectorizer._estimated_dynamic_bundles` | **frequency-weighted DYNAMIC bundles**, not static size |

So "compact" is not U=1 — it is whatever factor the search picked. Every
requirement Phase 4 lists is already met: each candidate is built through the
**full** pipeline (vectorization, scalar optimization, SWP/superblock, register
allocation, spill and bundle probes and the differential oracle), and the metric
is explicitly not a static heuristic. The R6.4.1 docstring records that this
objective "picks the measured-fastest factor on 8 of 8 kernels".

Consequence: implementing partial unrolling would have **duplicated an existing
feature**, which Phase 12 forbids.

## 2. Phases 6–7 — measuring the target, with no code change

`APARA_VECTOR_UNROLL` pins the factor and skips the search, so every candidate is
measurable directly. GEMM vi32, simulator ticks:

| M | U=1 | U=2 | U=4 | U=8 | U=16 |
|---|---|---|---|---|---|
| 16 | 4 893 | 4 893 | 4 893 | 4 893 | 4 893 |
| 24 | 31 236 | 31 236 | 31 236 | 31 236 | 31 236 |
| **32** | **148 975** | 280 047 | 280 047 | 280 047 | 280 047 |

At M=16/24 the factor is irrelevant because the *unrolled* realisation wins
(M=24 via the R11 rescue), so the chunk loop is never built.

At M=32 the search's own estimates are:

| U | estimated dynamic bundles | outcome |
|---|---|---|
| 8 | — | **not vectorized** |
| 4 | — | **not vectorized** |
| 2 | — | **not vectorized** |
| **1** | **167 423** | **compact — selected** |

The 280 047 figure is not an unrolled vector build; it is the **scalar
fallback**, because the kernel loses vectorization entirely at U≥2.
(`realisation_of` reports 'unrolled' for scalar code, which is what initially
made this look like a realisation flip.)

**No intermediate factor beats the shipped realisation.** That is Phase 7's stop
condition verbatim, and the STOP CONDITIONS list repeats it: *"no valid partial
factor beats the existing realization"*.

## 3. Why U≥2 loses vectorization — the real finding

The rejection reason is not a spill and not register pressure:

```
U=8: vectorized=False  reasons=['differential:mismatch']
U=4: vectorized=False  reasons=['differential:mismatch']
U=2: vectorized=False  reasons=['differential:mismatch']
U=1: vectorized=True   reasons=['ok']
```

**The differential oracle is rejecting the partially-unrolled build because it
computes a different answer than the scalar reference.** The safety gate is doing
exactly its job — refusing to emit wrong code — and the search silently falls
back to U=1.

Bounding the defect across types and sizes:

| type | lanes | M=16 | M=24 | M=32 |
|---|---|---|---|---|
| vi8 | 8 | chunks 2 — all U ok | chunks 3 — ok | chunks 4 — ok |
| vi16 | 4 | chunks 4 — ok | chunks 6 — ok | chunks 8 — ok |
| vi32 | 2 | chunks 8 — ok | chunks 12 — ok | **chunks 16 — U≥2 MISMATCH** |

**The defect is specific to 16 chunks with U≥2.** Twelve chunks are fine, eight
are fine, and every other element type is fine at every size tested.

## 4. Correctness impact — none in shipped output

This is a **latent** defect, not a shipped one:

* the differential oracle catches it before commit, on every build;
* the adaptive search falls back to U=1, which is validated and correct;
* GEMM vi32 M=32 passes its gcc golden check (3/3 PostConditions) today;
* the shipped 38-program suite never reaches 16 chunks.

No correctness risk exists now. What exists is a bug that makes the partial-unroll
path unusable at that chunk count — and it is masked, so it would not have been
found without pinning the factor.

## 5. What this means for the M=32 cliff

The cliff (145.48 ticks/output) is **not** caused by a missing intermediate
realisation. All four factors are constructed and evaluated on every build. Three
of them are discarded for computing the wrong answer, so the search is left with
one option.

Whether fixing the chunks=16 defect would actually *close* the cliff is unknown
and was not assumed: U≥2 has never produced a correct build at that size, so no
valid performance measurement of it exists. It might win; it might still lose to
compact. That question cannot be answered until the defect is fixed, which is a
separate milestone and was not started (the brief says: do not continue into
R12.1).

## 6. Deliverables against the brief

| phase | outcome |
|---|---|
| 1 — understand current unrolling | done; premise corrected |
| 2 — define partial realization | **already implemented** (`build_compact_chunk_loop`) |
| 3 — semantic requirements | the existing path violates them at 16 chunks (§3) |
| 4 — integration with adaptive search | **already implemented** (R6.4.1) |
| 5 — probe rule / R11 rescue | unchanged and still in force |
| 6 — primary target measured | §2 |
| 7 — expected decision | **stop condition triggered** |
| 8–9 — cross-size / cross-datatype | §3 table |
| 10 — correctness | no code changed; nothing to re-validate |
| 11 — performance | no candidate to report; §2 is the full picture |
| 12 — code quality | nothing duplicated, because nothing was written |

## 7. Recommendation

1. **Do not implement partial unrolling** — it exists, it is adaptive, and it
   already selects correctly everywhere it produces valid code.
2. **The real open item is the chunks=16 differential mismatch.** It is bounded,
   reproducible in one command
   (`APARA_VECTOR_UNROLL=2` on GEMM vi32 M=32), masked in production, and it is a
   correctness bug in existing code rather than a missing optimization. That is a
   better-defined piece of work than "partial unrolling" ever was.
3. `r10-final` remains the thesis artifact; nothing in R12.0 changes any shipped
   behaviour.

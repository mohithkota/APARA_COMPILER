# R1.3 Delivery Report — Factor-2 LoopUnroll Quality Improvements

**Milestone:** R1.3 (improve the R1.2 factor-2 unroller — same factor, same
legality, same profitability).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-25

---

## 1. Files added
| File | Purpose |
|---|---|
| `loopopt/loop_unroll3.py` | `LoopUnrollFactor2R13` — subclasses R1.2's `LoopUnrollFactor2`, inherits its helpers, overrides only `run()` with the four improvements. `unroll_module()` + report. |
| `loopopt/_r1_3_test.py` | R1.3 test suite (extends R1.2; R1.2 tests kept). |
| `loopopt/unroll3_corpus.py` | Corpus validation + baseline/R1.2/R1.3 measurement comparison. |

## 2. Files modified
**None.** Everything through R1.2 is frozen and untouched: R1.1 `LoopUnroll`,
R1.2 `LoopUnrollFactor2`, `ir_interp.py`, the M5 framework, legality, and the
profitability model. R1.3 is purely additive (a new subclass), so R1.2 remains
available and directly comparable. R1.1 (45/45), R1.2 (43/43), and the R1.2
corpus (24 loops, 0 mismatches) all still pass unchanged.

## 3. Optimisations implemented
1. **Induction-variable substitution.** R1.2's second copy reloaded the IV from
   its slot — a store→load serial chain (copy 1 stores `i+1`; copy 2 reloads it)
   the bundler cannot overlap. R1.3 reuses the value copy 1 **already computed**
   (the source temp of copy 1's IV-update store, `= i+step`) directly as a
   register, rewriting copy 2's IV-slot loads to it. The slot is clean (no alias),
   so values are identical; the cross-copy memory dependency disappears.
   - **Soundness guard (found via a corpus mismatch, then fixed):** only IV loads
     **before** the copy's single IV-slot *store* are substituted. A load *after*
     the store observes this copy's own updated IV — e.g. `s += i` after `i++` in
     `for(i=0,s=0;i<4;i++,s+=i)` — and must keep reloading. Post-store loads are
     intra-copy (no cross-copy dependency), so leaving them costs nothing.
2. **Dead-remainder elimination.** When the trip count is a compile-time constant
   evenly divisible by the factor (`T % 2 == 0`), the remainder loop can never
   iterate and is omitted entirely (the main guard's exit stays the loop exit).
3. **Symbolic bounds.** R1.2 required a constant guard bound. R1.3 also unrolls a
   loop whose bound is a **loop-invariant temp** (the M7 `guard_inputs_loop_independent`
   fact already holds and the bound is available at the preheader): it computes
   `bound - step` **once in the preheader** and guards the main loop against it,
   keeping the remainder on the original bound.
4. **Cleanup.** Substitution leaves copy 2's IV address computations (`loadaddr`)
   dead; those are dropped — only from code THIS pass generates, never the original.

Legality, profitability, the unroll factor (2), and the M5 framework are unchanged.

## 4. Validation methodology
Reuses R1.2's differential oracle (`ir_interp`): every changed function is
executed baseline-vs-R1.3 on identical state; return value **and** full final
memory must match. Layered with the M5 verifier + automatic rollback (tested with
a forced-failure verifier), and a compile check (CodeGen + bundler, no new spills).
The one corpus mismatch this uncovered (`f09_comma.c`, a second IV `s+=i`) drove
the post-store-load soundness guard above; it now matches.

## 5. Test summary
`_r1_3_test.py` — **all checks PASS**: const even/odd correctness, single-block
(substitution path), IV substitution (strictly fewer loads & address computations
than R1.2, behaviour identical), dead-remainder elimination (even → 1 loop, odd →
2 loops), symbolic bounds (trips 0–11, preheader computes `bound−step` once),
unsupported/nested rejection, rollback (byte-identical restore), clean
verification, regression compatibility, and improved-IR compiles. R1.2 (43/43)
and R1.1 (45/45) suites still pass.

## 6. Corpus validation (124 programs)
| Metric | R1.3 | R1.2 |
|---|---|---|
| Programs transformed | 21 | 21 |
| Loops transformed | 24 | 24 |
| Verifier failures | **0** | 0 |
| Rollbacks | **0** | 0 |
| Compilation failures | **0** | 0 |
| New register spills | 0 | 0 |
| Behaviour matches / **mismatches** | 16 / **0** | 16 / 0 |
| Not interpretable (calls/floats) | 6 | 6 |

**RESULT: PASS.** Coverage is unchanged (24 loops); the one symbolic corpus loop
does not meet the preheader-availability precondition, so R1.3's symbolic support
(proven on fixtures) adds no corpus coverage — but introduces no regression.

## 7. Performance comparison vs R1.2 (baseline / R1.2 / R1.3, same CodeGen+bundler, 21 programs)
| Metric | Baseline | R1.2 | R1.3 |
|---|---|---|---|
| Static ops | 2837 | 4113 (1.450×) | **3568 (1.258×)** |
| Bundles (code-size proxy) | 1644 | 2458 (1.495×) | **2051 (1.248×)** |
| Aggregate IPB | 1.726 | 1.673 (−0.052) | **1.740 (+0.014)** |

**R1.3 vs R1.2 directly: bundles 0.834× (≈17 % smaller code), IPB +0.066.**

Highlights:
- **Code size / static ops** improve markedly (dead-remainder elimination + the
  substitution cleanup remove instructions R1.2 emitted).
- **IPB** turns from a small **regression** (−0.052) into a small **improvement**
  (+0.014 over baseline): removing the cross-copy IV reload lets the bundler
  overlap the two copies' independent address computations.
- Coverage: unchanged (24 loops); **no** new spills, **no** mismatches.

## 8. Remaining work before R1.4
- **Broaden symbolic reach**: handle bounds loaded in the header (reload/hoist into
  the preheader) so the corpus's symbolic loops become eligible.
- **Substitute the first copy's IV reloads too** (from the header's IV register),
  removing the remaining intra-copy address reloads.
- **Larger factors** (from the model's `recommended_factor`), still framework-only.
- Unroll-and-jam and software pipelining remain explicitly out of scope (deferred).

Stopping after R1.3 — fully verified. Factor unchanged (2); legality and
profitability unchanged; not wired into the production pipeline; no R1.4 started.

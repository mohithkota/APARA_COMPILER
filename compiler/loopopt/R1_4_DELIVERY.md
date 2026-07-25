# R1.4 Delivery Report — Generalised LoopUnroll (factors 2/4/8, wider symbolic)

**Milestone:** R1.4 (generalise the transform; keep R1.3's correctness & quality).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-25

---

## 1. Files added
| File | Purpose |
|---|---|
| `loopopt/loop_unroll4.py` | `LoopUnrollFactorN` — subclasses R1.3's `LoopUnrollFactor2R13`, overrides only `run()`. Factors 2/4/8, chained IV substitution, wider symbolic bounds, generalised dead-remainder. `unroll_module()` (+ optional `force_factor` for tests). |
| `loopopt/_r1_4_test.py` | R1.4 test suite (extends R1.1–R1.3; those remain). |
| `loopopt/unroll4_corpus.py` | Corpus validation + baseline/R1.2/R1.3/R1.4 comparison + factor usage. |

## 2. Files modified
**None.** Everything through R1.3 is frozen and untouched (R1.1 `LoopUnroll`, R1.2
`LoopUnrollFactor2`, R1.3 `LoopUnrollFactor2R13`, `ir_interp`, the M5 framework,
legality, profitability). R1.4 is a new subclass. R1.1 (45/45), R1.2 (43/43),
R1.3 (all) still pass unchanged.

## 3. New capabilities
1. **Higher unroll factors (2/4/8).** The factor is the profitability model's
   `recommended_factor`, clamped to the supported set {2,4,8} and never above a
   known trip count (a support/sanity bound — no new heuristic). For factor F the
   body becomes F chained copies; the main guard is `iv < C − (F−1)·step`; a
   single remainder loop drains the `T mod F` leftovers.
   - The frozen model never recommends 8 (its code-growth budget is 6 < 8), so
     factor 8 is supported and **tested** (via a `force_factor` override) but not
     exercised on the corpus.
2. **Chained IV substitution.** Copy k reuses the value copy k−1 already computed
   (a register), never a slot reload. Substitution is now *full*: loads before a
   copy's IV store take the entry value, loads after it take that copy's own
   increment. Copies 2..F−1 make **no** IV-slot access and their dead IV stores
   are dropped — so there is no store→store WAW chain across copies.
3. **Wider symbolic bounds.** Loops whose invariant bound is loaded in the
   **header** (not only the preheader) are now unrolled: `bound − (F−1)·step` is
   computed where the bound is available (preheader if possible, else header).
   - **Soundness:** the bound's *value* must be provably loop-invariant — it must
     load from a **clean slot the loop never writes**. `guard_inputs_loop_independent`
     alone is insufficient (it only proves the guard *temp* isn't body-defined).
4. **Generalised dead-remainder elimination:** a known trip with `T % F == 0`
   omits the remainder loop entirely, for every factor.

Legality, the profitability model, the unroll factor policy (model-driven), and
the M5 framework are unchanged.

## 4. Validation methodology
Reuses R1.2's `ir_interp` differential oracle: every changed function is executed
baseline-vs-R1.4 on identical state and must match on return value **and** full
memory. Layered with the M5 verifier + automatic rollback (forced-failure test)
and a compile check (CodeGen + bundler, no new spills). Two real bugs the oracle
caught and drove fixes for:
- `u8_reverse_accumulate` (`while(lo<hi){lo++;hi--;}`) — the "bound" `hi`
  decrements, so it is not invariant → added the clean-unwritten-slot invariance
  check. Key lesson: temp-level independence ≠ value invariance.
- (test fixture) an array deliberately overlapping the IV slot — not a transform
  bug; well-formed IR never overlaps distinct slots, which the corpus confirms.

## 5. Test summary
`_r1_4_test.py` — **all 23 checks PASS**: factor 4 & 8 correctness (even/odd/
small trips), automatic factor selection (model picks 4), header-defined symbolic
bounds (factors 4 & 8), post-store IV reload (`s+=i`), **non-invariant bound
rejection**, per-factor remainder (dead when `T%F==0`, present otherwise), IV
substitution active at factor 4, rollback (byte-identical), clean verification,
regression compatibility, and compiles at every factor. R1.1/R1.2/R1.3 suites
still pass.

## 6. Corpus validation (124 programs)
| Metric | R1.4 | R1.3 | R1.2 |
|---|---|---|---|
| Programs transformed | **32** | 21 | 21 |
| Loops transformed | **43** | 24 | 24 |
| factor-2 / -4 / -8 usage | 6 / 37 / 0 | — | — |
| Verifier failures | **0** | 0 | 0 |
| Rollbacks | **0** | 0 | 0 |
| Compilation failures | **0** | 0 | 0 |
| New register spills | 0 | 0 | 0 |
| Behaviour matches / **mismatches** | 30 / **0** | 16 / 0 | 16 / 0 |
| Not interpretable (calls/floats) | 11 | 6 | 6 |

**RESULT: PASS.** Coverage nearly **doubled** (24 → 43 loops) — the symbolic
header-bound loops are now handled.

## 7. Performance comparison (baseline / R1.2 / R1.3 / R1.4, same CodeGen+bundler, 32 programs)
| Metric | Baseline | R1.2 | R1.3 | R1.4 |
|---|---|---|---|---|
| Static ops | 4752 | 6028 | 5483 | **7777** (1.637× base) |
| Bundles (code size) | 2693 | 3507 | 3100 | **4435** (1.647× base, 1.431× R1.3) |
| Aggregate IPB | 1.765 | 1.719 | 1.769 | **1.754** (−0.011 base, −0.015 R1.3) |

**Discussion — improvements and regressions.**
- **Improvement — coverage & factors:** the headline result. Applicable loops
  nearly doubled (24 → 43) via invariant symbolic bounds, and 37 loops now unroll
  at **factor 4** (vs factor 2 everywhere before). This is the milestone's goal.
- **Regression — code size:** bundles grow to 1.647× baseline (1.431× over R1.3),
  because factor-4 dominates and more loops are unrolled. Expected and inherent to
  higher factors; the R1.3 quality wins (substitution, dead-remainder, cleanup)
  keep it well below a naïve 4× (the copies emit no redundant IV loads/stores).
- **IPB roughly flat (slightly down):** the extra copies *expose* more independent
  work, but with **no scheduling / unroll-and-jam / software pipelining** (all
  explicitly out of scope) nothing *packs* it, and the corpus is dependency-bound
  (the M11 finding). So more unrolling alone cannot raise IPB here — converting the
  exposed ILP into throughput is downstream scheduling work.

## 8. Remaining work before production integration
- **Scheduling to realise the exposed ILP** — unroll-and-jam / software pipelining
  / modulo scheduling (all deliberately deferred): without these, higher factors
  grow code without raising IPB, so a pipeline-integration cost model must weigh
  size vs. throughput.
- **Cost-guided factor choice** in the profitability model (which factor actually
  pays off per loop) rather than the current budget-based recommendation.
- **Broader invariant-bound proofs** (globals, hoistable expressions) and
  intermediate-store elimination for the post-store (`s+=i`) shape.
- Pipeline integration and a hardware-simulator differential remain gated on the
  above.

Stopping after R1.4 — fully verified. Factors limited to 2/4/8; legality and
profitability unchanged; not wired into the production pipeline; no later roadmap
items begun.

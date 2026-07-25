# R1.1 Delivery Report — LoopUnroll Infrastructure

**Milestone:** R1.1 (first step of the M12 loop-opt roadmap, R1 = unrolling)
**Scope:** infrastructure only — eligibility, legality, and profitability *analysis*
plus clean M5-framework integration. **No unrolling is performed.**
**Date:** 2026-07-25
**Status:** ✅ COMPLETE & VERIFIED

---

## What R1.1 delivers

`loopopt/loop_unroll.py` — a `LoopUnroll` transform that:

1. **Selects structurally eligible loops** — reuses the frozen framework
   (M0 descriptor/discovery, M1 IVs, M2 mem-effects, M3 profile, M7 legality
   predicates). Duplicates no analysis.
2. **Computes a detailed legality report** per loop (`UnrollLegality`) — nine
   named checks, each a pass/fail `LegalityFact`; the overall verdict plus the
   first (most informative) rejection reason.
3. **Computes a compute-only profitability model** (`UnrollProfitability`) —
   code growth, ILP exposure (resource- vs recurrence-bound), register-pressure
   estimate, trip-count suitability, and the resulting factor/mode decision.
   **Nothing is transformed.**
4. **Integrates with the M5 framework as a deliberate no-op** — `run()` returns
   `False`, so each loop flows through `MutationTransaction` + verify + rollback
   and changes nothing.

Actual kernel duplication / IV & trip adjustment / remainder loop are **deferred
to R1.2** (not started).

---

## Bug found & fixed this session

While completing `_r1_1_test.py`, the `test_reject_irregular` case exposed a real
**legality gap**: `_shape_supported` classified a loop solely by its
`desc.shape` field, which only reflects the *header/latch guard*. A loop with a
**mid-body `break` / early return** (a genuine second exit) was therefore
mis-classified as `top-tested` and reported **eligible** — which is unsafe, since
unrolling would have to replicate an uncontrolled early exit inside every copy.

**Fix (legality only — profitability model untouched):** `_shape_supported` now
additionally requires `len(desc.exiting_blocks) == 1`. Multi-exit loops are
rejected as *"unsupported control flow (N exits; mid-body break / early exit)."*
The fact-count stays at 9 (folded into the existing predicate rather than adding
a tenth), so no other test's expectations changed.

This is a correctness fix the failing test proved necessary — not a redesign.

---

## Verification

### R1.1 unit suite — `python3 compiler/loopopt/_r1_1_test.py`
```
45 checks — 45 [ok] / 0 [FAIL]
R1.1 TESTS PASSED
```
Coverage: structural eligibility of a canonical counting loop; known & unknown
trip counts; nested-loop outer-rejection / inner-eligibility; opaque-call
rejection; **multi-exit rejection**; no-IV rejection; detailed legality report
shape; profitability *discrimination* (accepts a resource-bound loop **and**
rejects recurrence-bound, tiny-trip, high-pressure, and ineligible cases);
clean framework no-op.

### Corpus validation — `python3 compiler/loopopt/unroll_survey.py`
```
programs analysed : 124
loops analysed    : 79
loops eligible    : 45
loops rejected    : 34
verifier failures : 0
rollbacks         : 0
IR changes        : 0

eligible-loop decisions (compute-only):
  would-unroll (profitable) : 45
  eligible-not-profitable   : 0

rejection reasons:
  14  not innermost (contains a nested loop; deferred)
   7  opaque call (unbounded memory effects)
   7  no recognizable primary induction variable
   5  unsupported control flow (shape=irregular)
   1  unsupported control flow (2 exits; mid-body break / early exit)   ← newly caught by the fix

RESULT: PASS (0 IR changes / 0 verifier failures / 0 rollbacks)
```

### The "all eligible loops are profitable" property
Confirmed intact and now more accurate: **45/45** eligible loops are profitable
(`eligible-not-profitable = 0`). This was previously investigated and confirmed
a real property of the corpus (all eligible loops are innermost counted loops
that are resource-bound and small — exactly the shape unrolling helps). The
legality fix additionally removed one *false-positive* eligibility (the mid-body
break loop), so the property now holds over a strictly-correct eligible set.

---

## Deliverables
- `loopopt/loop_unroll.py` — LoopUnroll infrastructure (analysis + no-op transform)
- `loopopt/_r1_1_test.py` — 45-check unit suite (completed this session)
- `loopopt/unroll_survey.py` — corpus validation harness
- `loopopt/R1_1_DELIVERY.md` — this report

## Explicitly NOT done (per R1.1 scope)
- No loop unrolling performed (deferred to R1.2)
- No profitability-model changes (it was proven correct by the tests)
- LoopUnroll is **not** wired into the production pipeline
- R1.2 not started

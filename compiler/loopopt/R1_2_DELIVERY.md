# R1.2 Delivery Report — Factor-2 LoopUnroll Transform

**Milestone:** R1.2 (first production-quality loop-unroll transform)
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-25
**Mandate:** real unrolling, intentionally conservative, correctness over performance.

---

## 1. Files added
| File | Purpose |
|---|---|
| `loopopt/loop_unroll2.py` | `LoopUnrollFactor2` — the real factor-2 transform (subclasses R1.1 `LoopUnroll`, overrides only `run()`), plus `unroll_module()` + report. |
| `loopopt/ir_interp.py` | Small self-contained IR interpreter used **only** as the differential-validation oracle (executes a function before/after unrolling and compares observable behaviour). Not a compiler component. |
| `loopopt/_r1_2_test.py` | R1.2 test suite (43 checks). |
| `loopopt/unroll2_corpus.py` | Corpus validation + differential + measurement harness. |

## 2. Files modified
**None.** R1.1 is frozen: `LoopUnroll`, its legality, its profitability model, and
the M5 framework are untouched. R1.2 is purely additive. (`LoopUnroll.run()` stays
a no-op, so R1.1's `test_framework_noop` still holds — R1.1 suite still passes 45/45.)

## 3. Transformation algorithm
Factor 2, always with a remainder loop, for the strict canonical shape (a subset
of R1.1-eligible loops): top-tested, single preheader / single latch / single
exit, innermost, clean primary IV with positive step `s`, a **constant** guard
bound `iv < C` / `iv <= C` with standard while-polarity, and a side-effect-free
header. The body payload may span **multiple blocks** (the common `for` shape:
header + body + increment-latch).

For header guard `iv < C`, step `s`, payload `P`:

```
head: <setup>; if iv < C-s goto P1 else REM_ph      (tightened guard)
  P1 = <payload>          latch back-edge -> entry2   (first copy, in place)
entry2: <setup'>          (reload IV = iv+s, fresh temps)
  P2 = <payload'>         fresh temps + fresh labels; latch -> head (second copy)
REM_ph: goto REM_head
REM_head: <setup''>; if iv < C goto Pr else exit
  Pr = <payload''>        fresh temps + fresh labels; latch -> REM_head
exit:
```

Key correctness points (all mechanised through `MutationTransaction`; the frozen
framework does rebuild / verify / rollback):
- **Memory-backed IV.** The IV lives in a stack slot; copy 1 writes `iv+s`, copy 2
  RELOADS the slot (a freshened copy of the side-effect-free header setup) and so
  operates on `iv+s`, writing `iv+2s`.
- **Private namespace.** Every duplicated temp AND every duplicated internal label
  is renamed into a private namespace (`__u2t*` / `__u2L*`); external branch
  targets (the header back-edge, the exit) are preserved. No definition or label
  collides — SSA/temporary correctness is preserved.
- **Tightened main guard** `iv < C-s` runs the body only when *both* iterations are
  in range → `floor(remaining/2)` times; the **remainder loop** (original guard
  `iv < C`) drains the 0-or-1 leftover iteration. Total iterations unchanged.
- **Single latch preserved.** Copy 1's back-edge is rerouted into copy 2; only copy
  2 branches back to the header. The pass records the loops it synthesises and
  never re-unrolls them (terminates immediately).
- Anything outside the supported shape (bottom-tested, symbolic bound, non-standard
  polarity, non-contiguous payload, non-innermost, opaque call, multi-exit) is a
  **clean no-op** or is rejected by the reused R1.1 legality — never mis-transformed.

## 4. Validation methodology
**Differential execution** is the semantic oracle. `ir_interp.py` executes a
function's IR (memory-backed locals + DMEM globals, 64-bit integer semantics)
before and after unrolling from identical state and compares the return value and
the full final memory. Layers:
1. **Controlled tests** (`_r1_2_test.py`) — hand-built loops with known trip counts
   prove semantics exactly (return value *and* array contents), including the
   remainder path.
2. **Framework structural guarantees** — the M5 verifier runs after every edit;
   any violation triggers automatic rollback to byte-identical IR (tested with a
   forced-failure verifier).
3. **Corpus differential** — for every function the transform changed, baseline vs
   unrolled are executed and compared; functions the oracle can't run (calls/
   floats) are reported as *not-interpretable*, never scored as pass/fail.
4. **Compile check** — both versions go through the same CodeGen + bundler; the
   unrolled program must still generate code without new register spills.

## 5. Corpus statistics (124 programs)
| Metric | Value |
|---|---|
| Programs analysed | 124 |
| Build failures | 0 |
| Programs transformed | 21 |
| **Loops transformed** | **24** |
| Loops skipped | 79 |
| Verifier failures | **0** |
| Rollbacks | **0** |
| Compilation failures | **0** |
| New register spills | 0 |
| Behaviour matches (changed fns) | 16 |
| Behaviour **mismatches** | **0** |
| Not interpretable (calls/floats) | 6 |

**RESULT: PASS** (0 verifier failures / 0 rollbacks / 0 mismatches / 0 compilation failures).

Why 24 of 45 profitable loops: R1.2 deliberately requires a **constant** trip
bound (1 loop is symbolic → deferred) and the canonical contiguous single-exit
shape; recurrence-bound loops with larger trips are (correctly) declined by the
unchanged profitability model. This is the intended conservative scope.

## 6. Test summary
`_r1_2_test.py` — **43/43 checks PASS.** Covers: factor-2 unrolling (single- and
multi-block), even & odd trips, the remainder loop (created & correct), trip = 1
(rejected), trip = 2 (unrolled), an even/odd sweep (trips 2–12, memory verified),
nested-loop rejection (outer ineligible, inner unrolled), unsupported-loop
rejection (multi-exit / opaque call / symbolic bound → no-op, IR unchanged),
rollback on a forced verification failure (byte-identical restore), clean
verification, regression compatibility (untouched functions stay byte-identical),
and that unrolled IR still compiles without spilling.

R1.1 suite still **45/45** (frozen, unaffected).

## 7. Preliminary performance observations (measure only — nothing optimised)
Baseline vs unrolled, through the **same** downstream CodeGen + bundler (21 measured):
- static ops: **1.450×** (below 2× because header/guard are shared and the
  remainder is small),
- bundles (code-size proxy): **1.495×**,
- aggregate IPB: **1.726 → 1.673 (−0.052)**.

IPB does **not** improve yet — expected for a first unroller with **no scheduling
or unroll-and-jam**: the per-copy IV *reload* is a serial dependency, and the
loops the model accepts are largely dependency-bound (consistent with the M11
finding that the corpus is dependency-, not resource-, bound). Unrolling here
*exposes* duplicated independent work but nothing yet *packs* it; extracting the
ILP is downstream scheduling work.

## 8. Remaining work for R1.3
- **Symbolic / variable trip bounds** (`iv < n`): compute `n − s` in the preheader,
  guard the main loop against it — the single largest coverage gap (most of the
  21 not-yet-transformed profitable loops).
- **Unroll-and-jam / IV substitution**: replace the per-copy IV reload with an
  add on the live IV so copy 2 has no serial reload — the change that lets the
  bundler actually raise IPB.
- **Higher factors** (4/8) driven by the profitability model's `recommended_factor`
  (R1.2 forces 2).
- **Remainder minimisation** for known-even trips (skip emitting a dead remainder).
- Pipeline integration and a hardware-simulator differential remain out of scope
  until the transform is proven to help (post-scheduling).

Stopping after R1.2 — fully verified. No R1.3 work started; not wired into the
production pipeline.

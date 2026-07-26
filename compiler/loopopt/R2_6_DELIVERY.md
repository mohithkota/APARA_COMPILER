# R2.6 Delivery Report — Loop Register Promotion

**Milestone:** R2.6 (attack the RecMII bottleneck R2.5 measured: convert memory-
backed loop-carried recurrences into register recurrences).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-26

> Runs BEFORE R2.5 and never modifies it. Standalone — not wired into the
> production compiler (pipeline cross-check stays 124/124 identical). Correctness
> is mandatory: a promotion is committed only when the module still parses
> (globals preserved), a clean-slot-respecting multi-seed differential does not
> mismatch, AND the result compiles; otherwise the loop is rolled back untouched.

---

## 1. Files added
| File | Purpose |
|---|---|
| `loopopt/loop_promote.py` | The pass: eligibility + promotability analysis, the codegen-safe transform (preheader load / body register moves / exit write-back), RecMII-before/after + memory-recurrence-removed verification, and the structural + multi-seed-differential + compile gate with rollback. Drivers `promote_module()` / `promote_function()`; `PromoteStats`, `PromotionReport`. |
| `loopopt/_r2_6_test.py` | R2.6 unit suite (24 checks): accumulator, product/min/max reductions, IV promotion, multiple loads, multiple-store rejection, alias rejection, unsupported-loop rejection, determinism. |
| `loopopt/promote_corpus.py` | Corpus evaluation + baseline→R2.3→R2.4→R2.5→R2.6 comparison + the R2.6→R2.5 composition measurement. |
| `loopopt/R2_6_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `loopopt/__init__.py` | Additive R2.6 exports only. |

No previous pass (including the frozen R2.5 `modulo.py`), the DependenceGraph, the scheduler, register allocation, or the bundler is modified.

## 3. Analysis reused (nothing duplicated)
| Reused | For |
|---|---|
| M0–M3 descriptors (`discover` / `annotate_induction_vars` / `annotate_memory_effects`) | loop shape (single preheader/latch/exit), and **M2 `clean_slots`** — the escape analysis that proves "no alias may modify X" |
| R2.1 `DependenceGraph` + R2.2 `MemoryDisambiguator` | RecMII before/after and verifying the memory recurrence disappeared |
| R2.5 `modulo` (`rec_mii`, `KernelModel`, `_edge_latency`, `_compiles`) | RecMII computation + the compile gate (consumed, not modified) |
| `ir_interp` | the multi-seed differential oracle |

## 4. Promotion algorithm
For each eligible loop (one innermost natural loop; single preheader, single latch, single exit; no call in the body), a stack slot X is **promotable** when — reusing M2's clean-slot analysis — X is CLEAN (its address never escapes → no alias, no indirect store, no volatile reachability), is loaded and has **exactly one store** in the body, with one consistent element width. The transform is the codegen-safe shape **proven on hardware by the production `loop_reg` pass** (R2.6 reproduces its exact `IRAssign`-move form on the loopopt framework, so codegen's existing loop-aware live-range extension keeps the register live across the back-edge):
```
  preheader:   pa = &X ; P = load(pa)        # load once
  body load    t = *(&X)   ->  t = P          # register move (IRAssign)
  body store   *(&X) = v    ->  P = v          # register move (IRAssign)
  exit:        wa = &X ; store(wa) = P         # store once
```
Both the induction variable and any accumulator (sum / product / min / max) are promoted. The now-dead address `IRLoadAddr`s are dropped. No loop-body load/store of a promoted variable remains — the recurrence is register-resident.

## 5. Correctness proof
Register promotion of a **clean** slot is sound by the M2 escape analysis: the slot's address never escapes, so **no memory operation other than the promoted load/store can access it** — its value flows only through the recurrence, the preheader captures the entry value, and the single exit write-back restores it for any post-loop use. Two consequences the validation relies on:
1. A call elsewhere in the function cannot touch the clean slot, so when the interpreter cannot execute the function (a call aborts it) the promotion is still proven correct — accepted as *legal-by-construction* (exactly as production `loop_reg` promotes such functions with no differential at all).
2. A pointer cannot legitimately alias the clean (negative-offset) slot, so the differential seeds keep every pointer NON-NEGATIVE — fabricating pointer→clean-slot aliasing would test an impossible execution and falsely reject.

## 6. Validation strategy
Committed only when all hold, else rolled back untouched:
1. **Structural** — module still parses into the same function slices; globals / inter-function code preserved.
2. **Multi-seed differential** — original vs promoted run on the natural seed plus data seeds that vary the trip count and array data while keeping pointers non-negative (clean-slot-respecting). A `mismatch` rolls back; `unsupported` (interpreter can't run — a call outside the loop) is accepted on the clean-slot proof.
3. **Compile** — the promoted IR passes the production CodeGen.

The corpus harness independently re-checks behaviour (original vs promoted) with the differential oracle: **0 mismatches**.

## 7. Test summary
```
_r2_6_test.py ......................... ALL R2.6 UNIT TESTS PASS   (24/24 checks)
_r2_1 .. _r2_5 ....................... PASS (unchanged)
pipeline_crosscheck.py ............... 124/124 identical, 0 rollbacks (production frozen)
```

## 8. Corpus summary
```
  programs / loops examined  : 124 / 57
  promotable / promoted      : 48 / 46   (differentially-verified 33, clean-slot-proof-only 13)
  rollbacks (differential)   : 2         behaviour mismatches : 0
  memory recurrences removed : 198
  avg RecMII  before -> after: 5.48 -> 3.67
  loop-body memory ops       : 2369 -> 2271   (98 fewer)
  rejections                 : call-in-body 7, multi-exit 2, differential-rollback 2
```

## 9. Performance comparison
```
  baseline -> R2.3 -> R2.4 -> R2.5 -> R2.6   (bundler ON)
  static instructions : 11885 -> 11909 -> 11889 -> 13188 -> 11983
  bundles             : 6498  -> 6218  -> 6188  -> 6759  -> 6527
  IPB                 : 1.829 -> 1.915 -> 1.921 -> 1.951 -> 1.836
  register spills     : 0     -> 0     -> 0     -> 2     -> 0

  R2.6 -> R2.5 software-pipeliner coverage:
    loops R2.5 pipelines WITHOUT promotion : 12
    loops R2.5 pipelines WITH   promotion  :  2
```
**Improvements.** R2.6 achieves the milestone's core goal: it removes **198 memory
recurrences** and lowers the dependence-graph **RecMII 5.48 → 3.67** (the memory
load-modify-store cycle ≈5 becomes a register cycle ≈3), verified per loop. It cuts
**98 loop-body memory operations** — a *per-iteration* saving that multiplies
dynamically (the same lever the production `loop_reg` pass exists to pull:
"fewer executed loads"). No spills, code size near baseline (11983 vs 11885).

**Regressions / limitations (honest).** The *static* bundle/IPB metrics are
roughly flat (6527 vs 6498; IPB 1.836 vs 1.829) because register promotion is a
*dynamic* optimisation — it removes executed memory traffic per iteration, not
static loop-body bundles. More importantly, **R2.6 → R2.5 does not compose in the
generator**: R2.5's software-pipeliner eligibility requires a MEMORY-slot counted
IV, and promoting the IV to a register removes exactly that, so R2.5 declines the
promoted loops (pipeline coverage 12 → 2). The RecMII reduction is therefore
realised at the **analysis** level (and for the local R2.3/R2.4 scheduler, whose
critical path shortens), but R2.5's *generator* cannot yet exploit it. The two
rollbacks are the correctness gate working.

## 10. Remaining limitations
- **R2.5 register-IV support (the key follow-up).** To turn R2.6's lower RecMII into better *pipelines*, R2.5's `build_kernel` eligibility and M1's IV analysis must recognise **register** induction variables, not only memory-slot ones. That is a change to R2.5/M1, out of scope here (R2.5 is frozen and must not be modified). Once done, R2.6 → R2.5 would compose and the RecMII win would flow into the modulo scheduler.
- **Scope.** One eligible loop per function; only clean, single-store scalar recurrences (IV + accumulators). Multiple-store slots, escaping slots, call-containing and multi-exit loops are rejected cleanly.
- Not done (by design): no SSA reconstruction, no full mem2reg, no speculative/alias-speculative promotion, no rotating registers / MVE, no changes to the scheduler / R2.5 / register allocation / bundler / previous passes, no production integration.

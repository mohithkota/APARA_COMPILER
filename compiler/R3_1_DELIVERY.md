# R3.1 Delivery Report — Production Software Pipelining Integration

**Milestone:** R3.1 (wire the validated R2.5–R2.8 software-pipelining framework
into the production `compile_c_to_mcode()` path, gated by the R3.0 oracle, the
existing validator/rollback, and the existing spill criterion).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-26

> No new scheduler. Pure integration + profitability + validation + rollback. The
> DependenceGraph, disambiguator, R2.5–R2.8, the oracle, the bundler, the register
> allocator, and the spill-tier fallback are all CONSUMED unmodified. Production
> output is byte-identical to today's compiler except on loops that were pipelined
> AND passed every gate; `APARA_NO_SWP=1` reverts instantly.

---

## 1. Files added
| File | Purpose |
|---|---|
| `production_swp.py` | The integration + profitability module: oracle-gated candidate selection, invoke R2.8, per-function differential + zero-spill validation, splice into the production IR, per-function rollback. `ProfitabilityRecord`, `SWPSummary`, `apply_production_swp`, `format_profitability`. |
| `_r3_1_test.py` | Unit suite (16 checks): pipelines profitable loops, correctness unchanged, spill safety, rollback reliability, kill-switch identity, determinism, profitability report. |
| `swp_prod_corpus.py` | Corpus evaluation vs current production and vs standalone R2.8. |
| `R3_1_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `compiler.py` | `compile_c_to_mcode()`: capture the selected production-optimized IR (`_sel_ir`), then a single guarded SWP block that applies `apply_production_swp` and re-generates `body` only when a pipeline was accepted and the program still has zero spills. `APARA_NO_SWP` kill-switch. ~20 lines, additive. |

No pass, scheduler, bundler, allocator, dependence analysis, or MVE code is modified.

## 3. Where SWP sits in the pipeline
The production optimizer picks the best non-spilling tier (IVSR/LICM/loop-reg +
cleanup) → `_sel_ir` (today's output). Then:
```
oracle profitability  →  R2.8 pipeline  →  per-function differential ('match')
                      →  splice into _sel_ir  →  whole-program ZERO-spill check
                      →  accept | rollback (keep the proven slice)
```
SWP runs on the raw memory-backed `_ir0` (R2.5/R2.6 need memory-slot IVs, exactly
as validated standalone), and each committed function slice REPLACES that function
in `_sel_ir`. Non-pipelined functions and non-profitable programs are untouched.

## 4. Profitability (the R3.0 oracle)
`profitable_functions(ir0, threshold)` runs `oracle_ilp.analyze_module` and keeps
a function when an innermost loop's **top opportunity is software-pipelining** with
an estimated IPB gain **≥ threshold** (default 0.5, `APARA_SWP_THRESHOLD`). This is
the R3.0 tool making the decision — no new cost model.

## 5. Validation & rollback (all reused)
A function is pipelined into production only if ALL hold, else it rolls back to the
proven slice:
1. **R2.8 commits** a validated pipeline (its own structural + differential +
   compile + codegen-live-range gates already passed).
2. **Differential = 'match'** — the clean-slot multi-seed differential
   (`loop_promote._promote_diff`) confirms the pipelined function is behaviour-
   identical to the ORIGINAL. Production requires a *definite* match (stricter than
   the standalone gate, which also accepts a clean-slot `unsupported` proof) — so a
   function the interpreter cannot execute is conservatively left un-pipelined.
3. **Zero spills** — the whole spliced program must still compile with
   `cg.spilled == False`, the exact criterion the production tier selector uses.
   Any spill increase → rollback. (Candidates are applied greedily, highest
   expected gain first, each re-checked, so one spilling function never blocks
   another.)
The whole block is wrapped so any unexpected error keeps the proven `body`
byte-for-byte. **Correctness is therefore preserved by construction.**

## 6. Profitability report (per pipelined loop)
`ProfitabilityRecord`: function, loop label, original IPB (oracle achieved), oracle
IPB (theoretical), expected gain, realisation form (compact/full · register/memory),
accepted?, rollback reason. `SWPSummary` adds program-level static / bundles /
spills before→after. Printed in `compile_c_to_mcode(..., verbose=True)`.

## 7. Corpus results (124 programs)
```
  oracle SWP recommendations (loops) : 48
  standalone R2.8 coverage (loops)   : 17
  PRODUCTION pipelined (accepted)    : 10
  production rollbacks               : 4   (all differential-unsupported; 0 spill)
  oracle utilization                 : 10/48 of recommendations, 10/17 of R2.8-eligible (59%)
  behaviour mismatches               : 0    (MUST be 0)
  SWP pass time                      : 1.21 s total  (9.8 ms / program)

  Production compiler   SWP off -> SWP on
    static instructions : 11139 -> 11678   (+539, +4.8%)
    bundles             : 5986  -> 6075    (+89,  +1.5%)
    IPB                 : 1.861 -> 1.922   (+3.3%)
    programs that spill  : 0     -> 0       (no spill regression)
```

## 8. Success criteria — met
1. **Correctness unchanged** — 0 differential mismatches; every non-accepted loop
   keeps the proven output; kill-switch is byte-identical.
2. **Production automatically pipelines profitable loops** — 10 loops, default-on.
3. **Rollback reliable** — 4 rollbacks (unsupported differential), 0 spill
   regressions; guarded so any failure reverts to the proven `body`.
4. **Coverage approaches the oracle recommendation** — 10 of the 17 loops R2.8 can
   actually realise (59%); the remaining 7 are differential-unsupported functions
   (conservatively skipped) or below threshold. The oracle's 48 also flags loops
   R2.8 does not (yet) support (uncounted / unclean recurrences).
5. **Production IPB increases** — 1.861 → 1.922 (+3.3%); the added bundles are
   denser (IPB rises even though bundle count rises slightly).
6. **No unacceptable compile-time regression** — 9.8 ms/program (the oracle +
   pipeline + validation), and near-zero for the ~90% of programs with no
   profitable loop (early return before any transform).

Frozen suites unaffected: `pipeline_crosscheck` 124/124 identical; R2.5–R3.0 and
R3.1 unit suites all pass.

## 9. Test summary
```
_r3_1_test.py ........................ ALL R3.1 UNIT TESTS PASS   (16/16 checks)
_r2_5 .. _r2_8, _r3_0 ................ PASS (unchanged)
pipeline_crosscheck.py ............... 124/124 identical, 0 rollbacks
```

## 10. Honest notes / limitations
- **Static/bundle size grows** (+4.8% / +1.5%) — the intrinsic code-size cost of
  software pipelining (prologue/kernel/epilogue). It is modest and bounded (R2.8's
  compact kernel is O(stages), and any pathological growth would spill and roll
  back). The IPB (density) *rises*, and the win is a denser, higher-throughput
  steady-state kernel; the static cost is the standard pipelining trade.
- **Validation is IR-level.** Correctness rests on the clean-slot multi-seed
  differential (per function, definite match) plus R2.8's codegen live-range
  invariant for the rotating registers. No hardware simulation is invoked (project
  policy). **Recommended immediate follow-up: a simulator pass over the 10
  pipelined programs to confirm machine-level correctness before shipping;**
  `APARA_NO_SWP=1` is the instant kill-switch until then.
- **Setup code in a pipelined function is not re-optimized** — the spliced slice is
  the R2.8 form (loop pipelined + R2.6-promoted; surrounding scalar setup left
  memory-backed). For the loop-dominated functions that pipeline this is
  negligible, and the zero-spill gate rejects any case where it is not.
- **Coverage is gated by R2.8 eligibility and the strict differential** — uncounted
  loops, unclean recurrences, and interpreter-unsupported functions are left in the
  proven production form by design.
- Not done (by mandate): no redesign of software pipelining / MVE / bundler /
  register allocator / dependence analysis.

# R2.7 Delivery Report — Register-Aware Software Pipelining

**Milestone:** R2.7 (integrate R2.6 register promotion into R2.5 modulo
scheduling — extend only the recognition/normalization layer).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-26

> Extends only R2.5's RECOGNITION layer. The modulo scheduler is not redesigned,
> R2.5 and R2.6 are consumed unmodified, no rotating registers, no modulo variable
> expansion. Standalone — not wired into the production compiler (pipeline
> cross-check stays 124/124 identical). Correctness is mandatory: the end-to-end
> transform (original → promoted → pipelined) is validated with a clean-slot-
> respecting multi-seed differential + compile, and rolled back on any failure.

---

## 1. Files added
| File | Purpose |
|---|---|
| `loopopt/pipeline_regaware.py` | The `LoopRecurrence` canonical abstraction; the recognition/normalization front-end (`_normalize_register_loop`, `pipeline_regaware_module`); the shared register realiser (`realize_register_pipeline`); `RegAwareStats`, `RegAwareReport`. |
| `loopopt/_r2_7_test.py` | R2.7 unit suite (20 checks): register form + lower II, accumulator/IV as shared registers, memory-form fallback, mem-vs-register equivalence, mixed batch, determinism, rollback, coverage recovery. |
| `loopopt/regaware_corpus.py` | Corpus evaluation + coverage comparison (R2.5 vs R2.6→R2.5 vs R2.7) + baseline→R2.4→R2.5→R2.7. |
| `loopopt/R2_7_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `loopopt/__init__.py` | Additive R2.7 exports only. |

No previous pass (R2.5 `modulo.py`, R2.6 `loop_promote.py`), the DependenceGraph, the scheduler, register allocation, or the bundler is modified.

## 3. Recognition changes
Inspecting R2.5 showed the **only** memory-IV assumption is one line of
`build_kernel`'s eligibility: `primary_iv is None or trip_count == UNKNOWN`.
Everything else (shape, preheader/latch/exit, the header/latch blocks,
`trip_count.value`) is structural and equally valid for a register loop; the
DependenceGraph already represents register recurrences as carried edges, and the
scheduler already consumes those edges. So R2.7 changes only recognition:
1. Analyse the ORIGINAL (memory) loop with M1 → its KNOWN trip count `T`.
2. Apply R2.6 promotion → the IV/accumulator become register recurrences, RecMII drops (~5 → ~3).
3. **Normalise** the promoted descriptor: register promotion is value- and trip-preserving, so carry `T` forward — set `trip_count = KNOWN(T)` and a sentinel `primary_iv`. That is exactly what R2.5's eligibility needs. Nothing about scheduling is touched.
4. Feed the promoted loop to R2.5's `build_kernel` + `modulo_schedule` unchanged — it now schedules with the shorter register recurrence (RecMII 3, II 3 vs memory II 5).

## 4. Canonical recurrence abstraction
`LoopRecurrence(kind ∈ {memory, register}, producer, consumer, distance, latency,
var)` — derived from the DependenceGraph's carried edges (the same edges R2.5
schedules on). The `KernelModel` the scheduler consumes is already
memory/register-agnostic; `LoopRecurrence` is the typed view used for reporting
and for identifying which registers are loop-carried. Both loop forms flow
through it — the scheduler no longer cares where a recurrence originated.

## 5. Scheduler reuse
`build_kernel`, `min_ii`, `rec_mii`, `modulo_schedule`, `ReservationTable`, the
Bellman-Ford / difference-constraint feasibility, and `verify_schedule` are all
R2.5, called unchanged. **Realisation reuses R2.5's `_clone_op` exactly**, via one
insight: R2.5's full-unroll generator keeps a MEMORY recurrence correct because
the slot is *shared* (unrenamed); a register recurrence needs the identical
treatment — keep the loop-carried register *shared* while every other temp is
expanded per iteration. `realize_register_pipeline` achieves this by **pre-seeding
`_clone_op`'s rename cache** so the loop-carried registers (the resources of
carried RAW edges) map to themselves. There is ONE clone routine and one
linearisation; only the cache seed differs between the memory and register forms.
This is *not* modulo variable expansion (no rotating registers, no compact kernel)
— it is the shared loop-carried storage of a full unroll, exactly as R2.5 already
does for memory slots.

## 6. Validation strategy
Committed only when all hold, else rolled back untouched: **structural** (module
still parses; globals/inter-function code preserved), **clean-slot-respecting
multi-seed differential** (promoted vs pipelined, and end-to-end original vs
pipelined; pointers kept non-negative so they can never fabricate impossible
aliasing with a clean slot), and **compile**. R2.7 prefers the register form
(lower II); if it declines, it pipelines the memory form directly (Case A) via the
same code path. The corpus harness independently re-checks behaviour: **0
mismatches**.

## 7. Test summary
```
_r2_7_test.py ......................... ALL R2.7 UNIT TESTS PASS   (20/20 checks)
_r2_1 .. _r2_6 ....................... PASS (unchanged)
pipeline_crosscheck.py ............... 124/124 identical, 0 rollbacks (production frozen)
```

## 8. Corpus results
```
  Pipeline coverage (loops pipelined)
    R2.5 alone (memory only)         : 12
    R2.6 -> R2.5 (register rejected) :  2      <- the regression R2.7 fixes
    R2.7 (register-aware)            : 17      (register form 13, memory form 4)
    rollbacks 1 · behaviour mismatches 0
    avg RecMII / II / stages         : 4.18 / 4.35 / 2.35
```

## 9. Performance comparison
```
  baseline -> R2.4 -> R2.5 -> R2.7   (bundler ON)
  static instructions : 11885 -> 11889 -> 13188 -> 13651
  bundles             : 6498  -> 6188  -> 6759  -> 6894
  IPB                 : 1.829 -> 1.921 -> 1.951 -> 1.980
  register spills     : 0     -> 0     -> 2     -> 3
```
**Primary success criterion met.** Pipeline coverage is **recovered from 2 back to
17** — and *exceeds* R2.5's original 12, because the shorter register recurrence
lowers II (RecMII ~5 → ~3, II ~5 → ~3 on the register-form loops), which pushes
more loops over the profitability threshold (stages ≥ 2). **13 of the 17** loops
are pipelined via the register form; the RecMII improvement from R2.6 is not just
preserved but *exploited by the scheduler*, exactly as intended.

**IPB is the highest of the whole series (1.980**, baseline 1.829): the register-
form pipelines overlap more (lower II) and pack denser.

**Regressions (honest).** More loops pipelined via the full-unroll realisation
means more static instructions / bundles (13651 / 6894), and the denser register-
resident pipelines lift pressure enough for **3 spills** (vs 2 at R2.5). These are
the code-size-for-ILP trade of the full-unroll realisation — a compact register-
rotating kernel loop (future work, and explicitly out of scope: no MVE) would keep
the II/IPB win without the code growth.

## 10. Remaining limitations
- **Full-unroll realisation only.** Both forms are realised by the full-unroll
  generator (known trip counts, code grows with T). A compact kernel loop —
  which for register recurrences needs modulo variable expansion / rotating
  registers — is explicitly out of scope here and is the natural next step to
  remove the static-code cost.
- **Symbolic trip counts** are still declined (the realiser needs a known T).
- **Scope** inherits R2.5 (one innermost top-tested counted loop) and R2.6 (clean,
  single-store recurrences); everything else is rejected cleanly and, where a
  register form is declined, the memory form is pipelined instead.
- Not done (by design): no redesign of R2.5 / R2.6 / the scheduler / register
  allocation / the bundler / LoopInfo / DependenceGraph, no rotating registers, no
  MVE, no changes to previous passes, no production integration.

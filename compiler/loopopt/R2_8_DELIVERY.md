# R2.8 Delivery Report — Modulo Variable Expansion + Compact Rotating Kernel

**Milestone:** R2.8 (replace R2.5/R2.7's full-unroll pipeline realisation with a
compact modulo-scheduled kernel loop; rotating registers via modulo variable
expansion).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-26

> Replaces ONLY the realisation strategy. The modulo scheduler, register
> promotion, the dependence graph, the recurrence abstraction, the bundler,
> register allocation and LoopInfo are all consumed UNCHANGED — no redesign, no
> hardware rotating registers, no changes to any previous pass. Standalone — not
> wired into the production compiler (pipeline cross-check stays **124/124
> identical, 0 rollbacks**). Correctness is mandatory and rolls back on any doubt.

---

## 1. Files added
| File | Purpose |
|---|---|
| `loopopt/pipeline_mve.py` | The compact realiser: window emission with the MVE rename, `realize_mve_kernel` (prologue/kernel-loop/epilogue), the codegen live-range invariant, the register→memory / compact→full-unroll driver, `MVEStats`, `MVEReport`. |
| `loopopt/_r2_8_test.py` | R2.8 unit suite (32 checks): kernel generation, modulo variable expansion, rotating-register seeding, recurrence/accumulator/IV preservation, symbolic-trip decline, determinism, rollback, no-coverage-regression, compaction. |
| `loopopt/mve_corpus.py` | Corpus evaluation + baseline→R2.4→R2.5→R2.7→R2.8 compiled comparison. |
| `loopopt/R2_8_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `loopopt/__init__.py` | Additive R2.8 exports only. |

No previous pass (R2.5 `modulo.py`, R2.6 `loop_promote.py`, R2.7
`pipeline_regaware.py`), the DependenceGraph, the scheduler, register allocation
or the bundler is modified. R2.8 *consumes* R2.5's `build_kernel` /
`modulo_schedule` / `_clone_op`, R2.6's promotion + gate, and R2.7's recurrence
abstraction + full-unroll realiser (as the fallback).

## 3. MVE algorithm
The linearised schedule R2.5/R2.7 emit places instance *(iteration `it`, kernel op
`o`)* in **window** `W = it + stage(o)`, `stage(o) = cycle(o)//II ∈ [0,S-1]`. In
steady state each window issues, for every stage `s`, the stage-`s` op of iteration
`w-s` — so `S` iterations are in flight at once. Rolling those windows into a
single loop makes a value defined in stage `s_A` and consumed in stage `s_B` of the
same iteration live across `s_B-s_A` back-edges; one physical register would be
overwritten before the consumer reads it.

**Modulo variable expansion** renames every per-iteration temp into bank
`b = it mod U`, with **`U = S`**. Because the maximum live span of any value is
`≤ S-1 < S = U`, two iterations that share a bank (U apart) never overlap — the
rename is conflict-free. Within one window the `S` in-flight iterations occupy `S`
*distinct* banks (`w, w-1, …, w-S+1 mod S` are all different): a rotating-register
file of constant footprint, entirely in IR (no hardware support). Loop-carried
recurrence registers (the R2.6-promoted accumulator/IV) are the one thing kept
**shared** across all banks — identical to how R2.5 keeps a memory slot shared — so
the recurrence still threads.

## 4. Rotating register mapping
`bank(instance) = iteration mod U` is applied **uniformly** in the prologue, the
kernel-loop template, the remainder and the epilogue, so a temp `name~p{b}` denotes
the same rotating slot everywhere. Because the loop-body template represents
windows `[S-1 .. S-2+U]` and every actual iteration `it ≡ template_it (mod U)`, the
banks baked into the emitted-once loop body are correct for *every* pass of the
loop. Recurrence registers map to themselves in all banks (a pre-seeded rename
cache).

## 5. Kernel generation algorithm (known trip count T)
```
U = S                                          # rotating banks
prologue    = windows [0 .. S-2]               # ramp-up; SEEDS every rotating reg
kernel loop = windows [S-1 .. S-2+U] emitted ONCE, run K = (T-S+1)//U times
remainder   = windows [S-1+K·U .. T-1]         # steady windows the loop didn't cover
epilogue    = windows [T .. T+S-2]             # drain
```
The loop is a **do-while over a fresh register counter** initialised to `K` in a new
preheader (`_mvk = K; head: <body>; _mvk = _mvk-1; if _mvk>0 goto head`). Emitted
static size is `O(S)`, independent of `T`. Example (a 64-iteration reduction,
II=3, S=2, U=2): **static 640 → 44 instructions**, `K=31` loop trips, 5 rotating
registers.

Requires `T ≥ 2S-1` (≥1 full loop period) and that the compact form is actually
smaller; otherwise it falls back to R2.7's full-unroll realiser, so **coverage
never regresses**.

## 6. Correctness argument
1. **Schedule legality** is R2.5's (`verify_schedule`) — untouched.
2. **The MVE rename is conflict-free**: max live span `≤ S-1 < U`, so no two
   iterations sharing a bank have overlapping live ranges. The looped emission
   reproduces the flat mod-U schedule exactly because bank = `it mod U` is uniform
   and the steady state is byte-identical every `U` windows.
3. **Recurrences** stay shared (register: the carried-RAW resource kept identity
   across banks; memory: the slot is never renamed) — the R2.7/R2.5 treatment.
4. **IR semantics** are certified by the clean-slot-respecting multi-seed
   **differential** over the whole prologue/kernel/epilogue vs the original.
5. **Codegen correctness for the loop-carried rotating registers** — the one thing
   the IR differential cannot see (it does not model register allocation) — is
   certified by an explicit invariant computed with **codegen's OWN
   `_compute_last_uses`**: every rotating register the body reads-before-writes must
   have its live range extended to the back-edge (which happens iff it is defined
   before the header — and the prologue seeds exactly those). If it is not, the
   compact form is declined. Codegen's spill path is keyed by name and honours the
   same extended range, so spilled rotating registers are preserved too.
6. **Rollback**: structural + differential + compile gate + the invariant; any
   failure declines the compact form (fall back to the proven full unroll) or
   leaves the loop untouched.

## 7. Validation strategy
Consistent with the whole R2.x series, validation is **IR-level**: structural
(module still parses; globals/inter-function code preserved), clean-slot multi-seed
differential (pointers kept non-negative so they cannot fabricate impossible
aliasing with a clean slot), the production compile gate, plus the R2.8-specific
**codegen live-range invariant** (§6.5) that grounds the loop-carried-register
claim in the real allocator's code rather than a re-derivation. The corpus harness
independently re-checks behaviour: **0 mismatches**. (No hardware simulation is
invoked, per project policy; the codegen invariant is the substitute assurance for
the new back-edge-carried registers.)

## 8. Test summary
```
_r2_8_test.py ........................ ALL R2.8 UNIT TESTS PASS   (32/32 checks)
_r2_1 .. _r2_7 ....................... PASS (unchanged)
pipeline_crosscheck.py ............... 124/124 IR + code + tier identical, 0 rollbacks
```

## 9. Corpus results
```
  Pipeline coverage (loops pipelined)
    R2.5 alone (memory only)          : 12
    R2.7 (register-aware, full unroll) : 17
    R2.8 (compact-kernel)             : 17   (compact kernel 10, full-unroll fallback 7)
    rollbacks / declined              : 0 / 5      behaviour mismatches : 0
    avg II / stages                   : 4.24 / 2.35
    compact kernels: avg bank size / rotating regs : 2.20 / 4.10
    static IR on compacted loops      : 1038 -> 474   (54.3% smaller)

  Compiled comparison over 124 programs (baseline -> R2.4 -> R2.5 -> R2.7 -> R2.8)
    static instructions : 11885 -> 11889 -> 13188 -> 13651 -> 12661
    bundles             : 6498  -> 6188  -> 6759  -> 6894  -> 6583
    IPB                 : 1.829 -> 1.921 -> 1.951 -> 1.980 -> 1.923
    register spills     : 0     -> 0     -> 2     -> 3     -> 1
    R2.8 vs R2.7        : static -990 (-7.3%), bundles -311, spills -2
```
**Primary success criteria met.** Coverage is identical to R2.7 (17 loops, 0
mismatches, 0 rollbacks); **10 of the 17** are now realised as a compact rotating
kernel (54% less static IR each), and the remaining 7 fall back to the proven
full-unroll form so nothing regresses. Against R2.7 the compact realisation cuts
**static instructions −7.3% (990)**, **bundles −311**, and **spills 3 → 1** — the
denser full-unroll pressure is relieved because the kernel body is `O(S)`, not
`O(T)`.

**IPB (honest).** IPB dips 1.980 → 1.923. This is the *inverse* of R2.7's trade:
R2.7 spent code size to raise static packing density; a real kernel loop cannot
expose the same flat ILP (loop-carried deps + the counter/branch), so a little of
that density is given back. The **schedule and II are identical**, so *dynamic*
per-iteration throughput and dynamic memory operations are unchanged from R2.7 —
only the static footprint (and the pressure/spills it caused) shrinks. R2.8's IPB
still exceeds baseline (1.829) and R2.4 (1.921).

## 10. Remaining limitations
- **Known trip counts only.** The compact kernel needs `T` to place the prologue /
  loop trip / epilogue; symbolic trips are declined cleanly (§6). Extending the
  compact kernel to symbolic `T` (a runtime-computed loop trip + peeled remainder)
  is the natural next step and was scoped out here to keep correctness airtight.
- **`U = S` banks (not the minimal `MaxLive+1`).** A tighter bank count would
  shrink the kernel body further; `U = S` is chosen because it is always
  conflict-free and trivially provable.
- **`T ≥ 2S-1`** and a real size reduction are required; smaller/unprofitable loops
  fall back to the full unroll (so coverage is preserved, not compacted).
- **IR-level validation** (no hardware simulation), mitigated by the codegen
  live-range invariant for the new loop-carried registers.
- Scope inherits R2.5 (one innermost top-tested counted loop) and R2.6 (clean
  single-store recurrences).
- Not done (by design): no redesign of the scheduler / register promotion /
  dependence graph / bundler / register allocator / alias analysis / LoopInfo, no
  hardware rotating registers, no changes to previous passes, no production
  integration, no regression of any previous benchmark.

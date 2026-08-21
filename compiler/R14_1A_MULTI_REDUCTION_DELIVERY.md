# R14.1a — Generic multi-reduction vectorization

Branch `feature/r13-matmul-dot`, on top of R14.0 (`3e42e98`). Frozen tags
untouched. Nothing pushed. No automatic tiling, no unroll-and-jam, no new
scheduler, no second `$dot` emitter, no register-budget change.

## Answer to the final question

**Yes for correctness — no (yet) for performance.**

The pipeline *has* been generalized from one reduction to N structurally
independent reductions. A programmer-tiled matmul now detects, plans, lowers,
and runs with **every** output column preserved, and single-reduction behaviour
is byte-identical.

But it does **not** reproduce the measured J_TILE=4 what-if. The remaining cost
moved to address generation, and the honest numbers are below.

### Primary case — 16×16 vu8

| J_TILE | reductions preserved | `$dot` | ticks | ticks/output | bundles/output | spills | correct |
|---|---|---|---|---|---|---|---|
| 1 | 1/1 | 2 | 6143 | 23.996 | 8.00 | 0 | 256/256 |
| **2** | **2/2** | **4** | **5663** | **22.121** | **7.00** | 0 | 256/256 |
| 4 | 4/4 | 8 | 5791 | 22.621 | 7.75 | 0 | 256/256 |

`$dot` count is exactly `chunks × J_TILE` in every case — no stream is dropped.
Best is **J_TILE=2 at −7.8%**; **J_TILE=4 is worse than J_TILE=2**.

Against the R14.0 hand-written what-if (6.520 → 1.207 ticks/output, a 5.4×
kernel speedup), this delivers a small fraction. That gap is the finding, not a
detail to bury.

## Why the win is small — measured, not guessed

The vector block for J_TILE=4:

```
JT=1  fb_10:  8 bundles /  22 instrs, 1 output  ->  8.00 bundles/output
JT=2  fb_10: 14 bundles /  37 instrs, 2 outputs ->  7.00 bundles/output
JT=4  fb_10: 31 bundles /  71 instrs, 4 outputs ->  7.75 bundles/output
                            ^^^^^^^^
      of those 71: + = 42, << = 8, $ld = 8, $dot = 8, $st = 4
```

**50 of 71 instructions are address arithmetic for 8 dots.** Each output column's
B row base `(j+t)*N+k` is cloned independently, producing its own shift/add
chain. The hand-written kernel addresses all columns as `[$r28 + imm]` — one
base register plus compile-time displacements — because the columns sit at fixed
offsets from the B base.

An operand-sharing pass was added during this milestone and did help
(loads 21 → 8 for J_TILE=4, see below), but sharing the *loads* is not enough:
the *addresses* still differ per reduction.

## What was built

**Representation.** `kernel_detector.Reduction` (slot, op, value, store_index,
update_index, value_expr) and `kernel.reductions`; `vector_lowering.ReductionPlan`
(acc_slot, acc_bytes, array_slots/offs/addr/base_pre/info, array_key) and
`plan.reductions`. All singular fields alias `reductions[0]`, so every pre-R14.1a
consumer is untouched.

**Detector.** The `break` after the first reduction is gone; reductions are
sorted by store index for determinism. Verified: J_TILE=1/2/4 → 1/2/4 reductions.

**Planner.** Operands are attributed **structurally** — `_operand_loads_of` walks
each reduction's own `value_expr`, so two reductions' operands can be told apart.
Scanning every load in the body (the old behaviour) hands them all to the first
reduction. Region order is preserved so N=1 yields the identical list.
`need` is enforced **per reduction**, not just the first.

**Operand sharing.** Accesses are keyed by `(array slot, offset expression)`.
Each distinct access materialises its base once and loads once per chunk, so the
A row shared by every output column is not re-derived per column.

**Lowering.** `build_vector_body` emits one accumulator and one `$dot` stream per
reduction. For N=1 it takes the pre-R14.1a path unchanged.

**Legality — deliberately last.** `clean_slots` now admits every accumulator slot
the detector proved to be a clean scalar recurrence. This was widened **only
after** the lowering could emit N streams; widening it first is exactly what
makes legality accept loops whose extra outputs get silently dropped.

**Safety rails (reject, never mis-emit).**
- compact realisation declines N>1 (it threads ONE accumulator through the loop);
- a peeled remainder with N>1 is rejected (`PeelTemplate` has one dest slot);
- an explicit post-lowering invariant fails the build with
  `reduction-streams-lost:G-of-W` if any accumulator slot is missing from the
  emitted code.

**R13.1 interaction.** When N>1, inner-K accumulator expansion is **subsumed**
(`acc_expand_reason = 'subsumed-by-multi-reduction'`): the reductions are already
mutually independent, so expanding along chunks as well would multiply live state
for no further dependence relief. No `k × J_TILE` accumulator explosion.

## Datatype coverage — all correct, `$dot` = chunks × J_TILE

| dtype | lanes | J_TILE=2 `$dot` / t/out | J_TILE=4 `$dot` / t/out |
|---|---|---|---|
| vu8 | 8 | 4 / 22.12 | 8 / 22.62 |
| vi8 | 8 | 4 / 21.12 | 8 / 21.62 |
| vu16 | 4 | 8 / 23.68 | 16 / 23.75 |
| vi16 | 4 | 8 / 22.68 | 16 / 25.75 |

All 256/256 correct, 0 errors, **0 spills** — register pressure is not the
limiter at these widths, contrary to the R14.0 expectation that it would be.

## Regression — production unchanged

| check | Phase-0 baseline | R14.1a |
|---|---|---|
| 38-program suite | 38/38 | **38/38, metrics CSV bit-for-bit identical** |
| negative controls | 3/3 | **3/3** |
| `pipeline_crosscheck` | 124/124 | **124/124**, 0 IR / 0 code / 0 tier mismatches |
| `compiler/_r*_test.py` | 20/20 | **23/23** |
| `loopopt/_*_test.py` | 25/25 | **25/25** |
| `_r14_1a_test.py` | — | **43/43** |

Byte-identity was re-verified after **each** step (detector, per-reduction plan,
lowering, legality), not only at the end.

## Anti-bias

Renamed variables (`X/Y/p/q/r/acc`) and re-parenthesised index expressions
produce the same reduction count, same chunks/lanes/trip and same operand counts
per reduction. No `matmul16` special case exists anywhere.

## Limitations / next step

1. **Address generation is now the bottleneck**, not the dots: 50 of 71
   instructions at J_TILE=4. Columns `j+0..j+3` differ by a **compile-time
   constant** (`t * N * elem_bytes`), so they should share one base with an
   immediate displacement — R9.3's trick applied *across reductions* instead of
   across chunks. That needs `vector_affine` to expose the symbolic part of an
   offset so two accesses can be proven to differ by a constant; today it
   exposes only `const_off` and `sym_div`. **This is the single change most
   likely to unlock the measured 5.4×.**
2. J_TILE is written by the programmer; the compiler validates it. Automatic
   tiling still requires unroll-and-jam (R14.1b).
3. Multi-reduction remainder is rejected, not supported.
4. Compact realisation is unavailable for N>1.

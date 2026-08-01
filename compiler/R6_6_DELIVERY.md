# R6.6 — Vector Multiple Accumulator Expansion

**The recurrence reduction R6.6A projected is delivered exactly: `RecMII 8 → 3`,
`MII 8 → 5`, and the loop body measured at `10 → 5` bundles per iteration — the
new MII, hit precisely.** 38/38 simulator verification, 16/16 unit suites and
`pipeline_crosscheck` all pass.

**The whole-program payoff is much smaller than the loop-level result: one of 38
programs improves, by 3.4%.** §5 explains why, and the explanation turned up a
real second-order interaction that changed the design.

---

## 1. What was implemented

`reduction_accumulator_expansion.py`. When adaptive unrolling gives a
sum-reduction U copies, each copy gets its own accumulator instead of all U
chaining through one:

```
        before                              after
  acc += partial0                     acc0 += partial0     acc0 = incoming acc
  acc += partial1   <- waits          acc1 += partial1     acc1..accU-1 = 0
  acc += partial2   <- waits          ...                  (all independent)
  ...                                 ---- after the loop ----
                                      slot = ((acc0+acc1)+(acc2+acc3)) + ...
```

The epilogue folds with a **balanced tree**, not a chain — a chain would rebuild
in the epilogue the exact serial dependence the pass exists to remove.

**Correctness rests on one property:** integer addition is associative, including
under two's-complement wrap-around, so regrouping cannot change the result.
Narrow accumulators are safe for the same reason — the unexpanded loop truncates
to `acc_bytes` every iteration and this version truncates once, but
`(a+b) mod M + c == (a+b+c) mod M`, and a sign-extending reload of a truncated
value is congruent mod M.

**`vf32_t` is rejected by name**, first, before any other test, because float
addition is *not* associative. `ELEMENT_TYPES` currently has no float entry so
the case is unreachable today; the restriction is a property of the transform
rather than of the current table, so it is checked rather than assumed.

### What was reused, and what was not touched

Reused: `vector_pipeline` (unchanged), the existing reduction lowering
(`$vreduce` + integer add — the accumulate is still emitted by
`vector_lowering.build_compact_body`), adaptive unrolling, the differential
oracle, R6.1's dynamic-bundle estimator, R6.2C's `acc_bytes` width rule.

Not touched: **no scheduling change, no bundler change, no legality change, no
new IR node, no ISA change, no duplicate lowering.** `build_compact_chunk_loop`
was not modified either — it already invokes `emit_body` once per unrolled copy
in order, so the client tracks its own copy index rather than widening a contract
three other clients would have had to absorb.

## 2. Loop-level result — the projection, hit exactly

Unroll factor **pinned** so the selector is not a confound:

| kernel | arm | ops | bundles/iter | occupancy | RecMII | ResMII | MII |
|---|---|---|---|---|---|---|---|
| reduction vi32 (U=8) | HEAD | 36 | 10 | 45.0% | **8** | 5 | **8** |
| reduction vi32 (U=8) | **R6.6** | 36 | **5** | **60.0%** | **3** | 5 | **5** |
| reduction vi16 (U=4) | HEAD | 20 | 6 | 41.7% | 4 | 3 | 4 |
| reduction vi16 (U=4) | **R6.6** | 20 | **4** | **62.5%** | **3** | 3 | **3** |

Against the R6.6A projection for `reduction vi32`:

| quantity | R6.6A projected | R6.6 measured |
|---|---|---|
| RecMII | 8 → 3 | **8 → 3** ✓ |
| MII | 8 → 5 | **8 → 5** ✓ |
| bundles/iteration | 10 → 5 | **10 → 5** ✓ |

The recurrence stops binding: ResMII (5) takes over, and the loop now runs at the
resource bound. **Success criterion 2 is met, and criterion 3 exactly.**

## 3. Simulator ticks, per unroll factor

Both arms, factor pinned, so the effect of expansion alone is visible:

| kernel | U | HEAD | R6.6 | delta |
|---|---|---|---|---|
| reduction vi32 | 1 | 884 | 884 | 0 *(U=1: inert by construction)* |
| reduction vi32 | 2 | 722 | 706 | −16 |
| reduction vi32 | 4 | 690 | **645** | **−45 (−6.5%)** |
| reduction vi32 | **8** | **631** | **758** | **+127 (+20%)** |
| reduction vi16 | 2 | 642 | 634 | −8 |
| reduction vi16 | 4 | 626 | **605** | **−21 (−3.4%)** |
| reduction vi16 | 8 | 695 | 695 | 0 *(fully-unrolled realisation, no loop)* |
| reduction vi8 | any | 752 | 752 | 0 *(fully-unrolled realisation)* |

Expansion wins at U=2 and U=4 and **loses badly at U=8**, which is exactly where
the loop-level result is best (10 → 5 bundles). That contradiction is the subject
of §5 and it is the reason for the design decision in §4.

## 4. Expansion is a measured CANDIDATE, not a default

Because of §5, expansion is not applied unconditionally. It is added as a second
axis to R6.4.1's existing adaptive search: each unroll factor is built with
expansion, and — **only when the build actually expanded something** — also
without it, and the lowest estimated dynamic bundle count wins. The objective is
R6.4.1's, already validated against the simulator on 8/8 kernels.

Consequences:

* a module with no integer vector reduction builds exactly as many candidates as
  before, so **its compile time is unchanged** (measured: elementwise vi16
  1.213 s → 1.194 s);
* a reduction module pays roughly +64% (reduction vi32 0.722 s → 1.187 s);
* the selector declines expansion for `reduction vi32` (keeping HEAD's 631) and
  takes it for `reduction vi16` (605), i.e. **the per-kernel optimum in both
  cases**, with no regression anywhere.

## 5. Why U=8 costs 20% — investigated, not modelled away

The milestone asked to investigate rather than adjust the model. The measured
cause is **not** the prologue or epilogue, and **not** register spilling
(codegen reports zero spills in both arms at every factor).

Attributing every dynamic bundle to its block, `reduction vi32` at U=8:

```
  HEAD                                    R6.6 (expansion on)
  main            6 x1    =    6          main          7 x1    =    7
  fc_1            4 x65   =  260          fc_1          4 x65   =  260
  fb_2            2 x64   =  128          fb_2          2 x64   =  128
                                          fi_3          1 x64   =   64   <-- NEW
  fe_4            7 x1    =    7          fe_4          7 x1    =    7
  vcl_1_cond      4 x5    =   20          vcl_1_cond    4 x5    =   20
  vcl_2_body     10 x4    =   40          vcl_2_body    5 x4    =   20   <-- the win
                                          vcl_3_incr    3 x4    =   12   <-- NEW
  vcl_4_end       7 x1    =    7          vcl_4_end     4 x1    =    4
  main_epilogue   2 x1    =    2          fe_8          5 x1    =    5
                                          main_epilogue 2 x1    =    2
  TOTAL                      470          TOTAL                    529
```

The loop body did exactly what it was supposed to (40 → 20 dynamic bundles). The
loss is `fi_3`, **a block of the 64-iteration SCALAR initialisation loop that has
nothing to do with reductions**, worth 64 dynamic bundles on its own.

Cause: R3.2's superblock pass merges single-pred/single-succ chains and at HEAD
merges `fi_3`, `vcl_3_incr` and `fe_8` away. With expansion at U=8 it merges
**nothing at all** — confirmed by comparing labels before and after the pass:

```
  HEAD  merged away : ['fe_8', 'fi_3', 'vcl_3_incr']
  R6.6  merged away : []
```

R3.2 accepts its merges for the **whole module** (accept iff zero-spill *and*
bundles not increased). Expansion makes the vector body denser — occupancy
45% → 60% — so the module-level acceptance test fails, and *every* merge is lost
including ones in unrelated scalar loops. The transform is a local improvement
that trips a global gate.

**This is a pre-existing limitation of R3.2's all-or-nothing acceptance, exposed
rather than caused by R6.6.** Fixing it means making superblock acceptance
per-region instead of per-module, which is a scheduling/region-formation change
and explicitly out of scope here. It is recorded as the follow-on.

## 6. Whole-suite result

| | ticks |
|---|---|
| HEAD (`c2e5997`) | 136 847 |
| R6.6 as shipped | **136 826** |

**One program of 38 changes: `reduction vi16`, 626 → 605 (−3.4%).** Everything
else is tick-identical.

That is a small return for the machinery, and it should be read plainly:

* four of six reduction markers never reach the transform at all — `vi8` uses the
  fully-unrolled realisation (no loop), and the three unsigned markers do not
  vectorize as reductions;
* `vi32` is measured and the expansion is declined (§5);
* the suite is dominated by GEMM, and reductions are a few percent of its total.

The loop-level result in §2 is real and large; the program-level result is small
because the loop is a small share of these programs — the same denominator effect
R6.5 and R6.6A both documented.

## 7. Does this make SWP unnecessary for reductions?

The milestone's stated purpose. **Yes, for the recurrence — the recurrence is no
longer the limiter.** After expansion `RecMII = 3` against `ResMII = 5`, so
`MII = ResMII`: the loop is resource-bound, and R6.6A projected SWP would reach
only `MII = 8` on the unexpanded loop. Expansion gets to 5 and the measured
bundles/iteration is 5.

Software pipelining could still overlap the remaining resource-bound work across
the back edge, but it can no longer claim the recurrence as its justification for
reductions. **R6.6A's recommendation to do expansion before SWP is confirmed by
measurement.**

## 8. Success criteria

| # | criterion | result |
|---|---|---|
| 1 | no correctness regressions | **38/38 PASS**, 3 negative controls rejected, crosscheck PASS, 16/16 unit suites |
| 2 | RecMII ≈ 8 → 3 | **8 → 3 measured** (reduction vi32, U=8) |
| 3 | measured matches the R6.6A model | **exactly** — RecMII 8→3, MII 8→5, bundles/iter 10→5 |
| 4 | no impact on elementwise, AXPY, GEMM, convolution | **byte-identical IR** with expansion on and off (asserted in `_r6_6_test.py`); dot also unaffected |
| 5 | deterministic, byte-identical on non-vector programs | **`pipeline_crosscheck` PASS** |

## 9. Threats to validity

* **The suite-level gain is 21 ticks (0.015%).** The loop-level result is what
  justifies the pass; the program-level result on these benchmarks does not.
* **Only two kernels exercise the transform** (`reduction vi16` and `vi32`). The
  unsigned markers and `vi8` never reach it, so coverage of the expansion path is
  narrower than the 38-program suite suggests.
* **The U=8 regression is explained but not fixed** — it is avoided by declining
  expansion there. The underlying R3.2 whole-module acceptance gate is untouched.
* **Compile time rises ~64% for reduction modules** on top of R6.4.1's ~5×.
* **Associativity is argued, not proved by exhaustive test.** The differential
  oracle validates every candidate over 6 seeds across the full byte range, and
  the simulator verifies against a gcc golden reference, but neither is a proof.
* Ticks are the simulator's, not hardware cycles.

## 10. Follow-on

**Make R3.2's superblock acceptance per-region rather than per-module.** §5 shows
a local density increase in one loop currently costs an unrelated loop its merge,
worth 64 dynamic bundles here. That would likely let `reduction vi32` keep both
U=8 *and* expansion — the configuration with the best loop body measured
(5 bundles/iteration) but the worst whole-program tick count.

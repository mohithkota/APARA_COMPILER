# R6.7 — Region-Based Superblock Acceptance

**The interaction R6.6 identified is removed: `reduction vi32` now keeps U=8 *and*
accumulator expansion *and* the scalar-loop merges it previously lost.** Loop body
10 → 4 bundles, occupancy 45% → 75%, dynamic bundles 470 → 459, ticks 631 → 625.

38/38 simulator verification, 16/16 unit suites and `pipeline_crosscheck` all
pass, with no program regressing.

**The suite-level gain is 6 ticks.** §6 is honest about that and about the
compile-time cost, which is the real price of this milestone.

---

## 1. What changed

**The R3.2 profitability metric is untouched** — the candidate must compile, must
not introduce a spill, and must not increase the bundle count. Only the *scope* at
which it is applied changed, from whole-module to per-region.

```
  R3.2                              R6.7
  form every merge                  1. form every merge, test it  (unchanged)
  test the whole module                if it passes -> take it, done
  pass -> take all                  2. otherwise offer regions ONE AT A TIME,
  fail -> discard ALL                  each kept if it holds the same metric
```

Region formation gained a `select` parameter (`superblock.MergeCandidate`,
`merge_candidates_module`); the merge legality conditions in the enumeration loop
are byte-for-byte the pre-R6.7 ones. No vector lowering, scheduler, bundler or
legality change.

### Why step 1 is load-bearing, not an optimisation

Greedy per-region acceptance can only ever reach a **subset** of the merges, and a
subset can be worse than the full set: if merge A alone increases bundles but
A+B together do not, a greedy pass that offers A first rejects it and can never
recover it. **This was measured, not hypothesised** — an earlier version of this
milestone without step 1 regressed `gemm vi8` and `gemm vu8` by **+256 ticks each
(+3.1%)**, turning the suite +0.36% worse. Trying the whole set first makes R6.7
a strict refinement: it can only act where R3.2 previously discarded everything.

### Correctness conditions

None weakened. The scheduler's own topological check, differential validation and
per-function rollback run on every trial, and a region is adopted only after a
full codegen + bundle of the whole module — the same evidence R3.2 required, just
demanded per region instead of once.

## 2. A correction to the R6.6 report

R6.6 said the module-wide candidate "fails the acceptance test" and attributed it
to the bundle-count condition. **The actual rejection reason is `spill-increase`**
— merging all three regions at U=8 with expansion causes a spill. The consequence
R6.6 described (every merge discarded, `fi_3` lost, 64 dynamic bundles) is
unchanged, but the gate that fired was the spill gate, not the bundle gate.

## 3. The R6.6 case, resolved

`reduction vi32`, U=8, accumulator expansion on:

| | whole-module (pre-R6.7) | **per-region (R6.7)** |
|---|---|---|
| accepted | **no** — `spill-increase` | **yes** |
| regions considered / accepted | — / 0 | 3 / **2** |
| merged away | `[]` | **`['fe_8', 'fi_3']`** |
| static bundles | 44 → 44 | 44 → **40** |

`vcl_3_incr` — the one region whose merge actually causes the spill — is the only
one rejected. `fi_3`, a block of the 64-iteration **scalar** initialisation loop,
is recovered, which is exactly the unrelated-region merge R6.6 lost.

## 4. Confirming the milestone's target configuration

The milestone asked to confirm `vi32` keeps U=8 + accumulator expansion while
preserving the scalar merges. It does:

| | pre-R6.7 | **R6.7** |
|---|---|---|
| accumulator expansion | **declined by the selector** | **taken — 8 accumulators** |
| unroll factor | U=8 | **U=8** |
| vector loop body | 10 bundles | **4 bundles** |
| vector occupancy | 45.0% | **75.0%** |
| dynamic bundles | 470 | **459** |
| dynamic IPB | 1.732 | **1.804** |
| simulator ticks | 631 | **625** |

Before R6.7 the adaptive selector *declined* expansion for this kernel because the
lost merges cost more than the expansion saved. With per-region acceptance the
merges survive, so the selector now takes expansion — the two optimizations
compose instead of competing.

## 5. Whole-suite measurements

**Merged regions** (all 38 programs, superblock applied to the production IR):

| scope | programs accepted | regions merged | static bundles saved |
|---|---|---|---|
| whole-module | 33 | 105 | 157 |
| **per-region** | **34** | **107** | **161** |

**Where the whole-module gate was failing** — 5 of 38 programs:

| program | reason | recoverable? |
|---|---|---|
| reduction vi32 | `spill-increase` | **yes — 2 of 3 regions** |
| conv3 vi8 | `spill-increase` | no — no region profitable alone |
| conv3 vu8 | `spill-increase` | no |
| gemm vi32 | `compile-failed` | no |
| gemm vu32 | `compile-failed` | no |

**Simulator ticks** — one program changes, none regress:

| | ticks |
|---|---|
| R6.6 (`ac86052`) | 136 826 |
| **R6.7** | **136 820** |
| `reduction vi32` | 631 → **625 (−1.0%)** |

## 6. Cost, stated plainly

| kernel | module scope | region scope | delta |
|---|---|---|---|
| gemm vi16 | 4.451 s | 5.406 s | +21.5% |
| reduction vi32 | 1.070 s | 1.205 s | +12.6% |
| conv3 vi8 | 1.849 s | 3.311 s | **+79.1%** |
| elementwise vi16 | 1.331 s | 2.924 s | **+119.8%** |

The search runs only when the whole-module candidate is rejected, but
`vectorize_all_module` builds many candidates (unroll factor × expansion) and
`production_codegen` applies superblock to each, so a kernel whose *rejected*
candidates trigger the search pays for it even though its *chosen* candidate does
not. `elementwise vi16` and `conv3 vi8` pay the most and gain nothing.

**So: 6 ticks of benefit for up to 2.2× compile time on some kernels.** That is a
poor trade on this benchmark suite and it should be weighed before this is kept
on by default; `APARA_SUPERBLOCK_MODULE_SCOPE=1` restores the old behaviour and
the old cost. The structural argument for keeping it is that the interaction it
removes gets *worse* as more vector optimizations land — R6.6 was the first to
hit it, not the last.

Mitigations not implemented: cache trial results by IR identity across the
adaptive search; run the search only for the candidate the selector finally
chooses; leave-one-out instead of build-up when a single region is at fault.

## 7. Success criteria

| # | criterion | result |
|---|---|---|
| 1 | 38/38 simulator PASS | **38/38**, 3 negative controls rejected |
| 2 | all regression suites pass | **16/16 unit suites**, `pipeline_crosscheck` PASS |
| 3 | recover the merges lost in R6.6 | **`fi_3` and `fe_8` recovered**; 105 → 107 regions suite-wide |
| 4 | ticks improve, vector-loop correctness unchanged | **631 → 625**; 38/38 golden-reference verification |
| 5 | no regressions on non-vector programs | **`pipeline_crosscheck` PASS**; no program in the suite regresses |

## 8. Threats to validity

* **The benefit is 6 ticks on one program.** The mechanism is demonstrated and the
  merges are provably recovered, but the performance case rests on one kernel.
* **Only one of five whole-module rejections was recoverable.** Three fail for
  spill reasons no single region fixes, two fail to compile. The interaction is
  real but rarer than R6.6 might have suggested.
* **Greedy acceptance is order-dependent** and reaches a local optimum, not the
  best subset. Step 1 bounds the damage (never worse than the whole-set answer)
  but does not make the search optimal.
* **Compile time is up substantially on kernels that gain nothing** (§6).
* **The aggregate table in §5 was measured with each arm's own vectorized IR**, so
  the region-scope row includes accumulator expansion for `vi32` while the
  module-scope row does not — that is the shipped configuration in each case,
  which is the honest comparison, but it is not a controlled one.
* Ticks are the simulator's, not hardware cycles.

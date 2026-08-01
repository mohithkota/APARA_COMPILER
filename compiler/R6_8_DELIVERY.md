# R6.8 — Vector Software Pipelining (elementwise / AXPY)

**Vector loops are now modulo-scheduled. `axpy vi16` runs 1550 → 1238 ticks
(−20.1%) and `axpy vu16` 1806 → 1559 (−13.7%); dynamic IPB 1.605 → 2.005 and
vector-loop occupancy 19.6% → 33.0%.** 38/38 simulator verification, 16/16 unit
suites and `pipeline_crosscheck` all pass, and reduction, GEMM, convolution and
dot are provably untouched.

**The result is narrower than R6.6A projected — 2 programs of 12, not 12 — and
about 44% of the measured gain is not software pipelining at all.** §5 and §6
give the evidence for both statements rather than the headline alone.

---

## 1. What was built

`vector_swp.py`. **No new scheduler.** R2.5's modulo scheduler, R2.8's compact
MVE kernel realiser, the dependence graph, the memory disambiguator, the
differential oracle and the rollback discipline are reused as they stand. Two
changes were needed in shared code and both are described below.

**Where it runs:** on the production-optimized IR, after the scalar optimizer and
R3.1's scalar SWP, pipelining only the vector loop. R3.1's own approach —
pipeline the pre-optimization IR and splice whole function slices — would be
wrong here, because these programs keep the vector kernel in `main` alongside
scalar initialisation loops, and replacing the whole optimized `main` would
discard LICM, IVSR and register promotion for those loops.

## 2. Selecting exactly two families — and why not by name

**The vectorizer's own `kind` cannot separate them.** Measured:

| kernel | reported kind | reported transform |
|---|---|---|
| elementwise | `vector-add` | `elementwise` |
| **convolution** | `vector-add` | `elementwise` |
| axpy | `saxpy` | `gemm` |
| **GEMM** | `saxpy` | `gemm` |

Convolution is indistinguishable from elementwise and GEMM from AXPY. Eligibility
is therefore decided on the **emitted loop**:

| rule | excludes |
|---|---|
| header label starts with `vcl_` (a compact vector loop exists) | convolution, dot — both fully unrolled, so there is no loop |
| contains `IRVecArith` | reduction (it has `$vreduce`, not vector arithmetic) |
| contains no `IRVecReduce` / `IRVecDot` / `IRVecDot128` | reduction, dot |
| contains no `\|` or `>>` — only the R6.3 window lowering emits those | convolution |
| innermost **and** counted | **GEMM**, whose vector loop is `no-counted-iv` after optimization |

Measured verdicts on the six families:

```
elementwise vi16  eligible                     axpy vi16  eligible
reduction vi32    no-vector-arithmetic         gemm vi16  no compact vector loop
conv3 vi8         no compact vector loop       dot vi8    no compact vector loop
```

The sliding-window and `$vreduce` rules are unreachable through the current
realisation choices, so they are tested directly on constructed loop bodies in
`_r6_8_test.py` rather than left unexercised.

## 3. The blocker that had to be fixed: invariants are not rotating registers

With the blocklist relaxed, R2.8 **declined every vector loop**:

```
reason: full-unroll-fallback:unseeded-rotating-reg:_vct23~p0
compacted: False
```

and fell back to R2.7's full unroll — which is not software pipelining at all,
and which erases the loop.

`_vct23` is `&stack[FP-392]`, the address of the induction-variable slot, defined
**at index 47, immediately before the loop header at 48**. It is loop-invariant.
Modulo variable expansion was renaming it per rotating bank, producing a bank-0
copy that the kernel body reads before writing, which `_codegen_keeps_alive`
correctly refuses because codegen has nothing to keep alive.

**A value the loop never writes has no per-iteration version.** The fix
(`_kernel_invariants` + one extra argument to `_seed_cache`) shares such values
across all banks, exactly as loop-carried recurrence registers already were. That
is a renaming correction in the realiser, not a change to modulo scheduling.

With it, both families produce genuine compact kernels:

| kernel | form | II | stages | prologue | kernel body | epilogue | kernel trips |
|---|---|---|---|---|---|---|---|
| elementwise vi16 | register | 6 | 2 | 11 | 30 | 19 | 3 |
| axpy vi16 | register | 8 | 2 | 9 | 22 | 13 | 7 |

This touches code R3.1's **scalar** SWP also uses, which is why
`pipeline_crosscheck` over 124 programs is the load-bearing regression check
here. It passes.

## 4. A measurement bug that would have shipped a fake 90% win

The first profitability estimate read **702 → 76 dynamic bundles**. It was wrong.

The register form runs `promote_function` over the **whole function slice** first,
and register promotion erases the memory-slot induction variable that
`loopopt.analysis_iv` needs to prove a trip count — the known R2.6 interaction.
Frequencies for loops the pipeline never touched silently collapsed from 64 to 1:

```
before: {'fc_5': 65.0, 'fb_6': 64.0, 'vcl_1_cond': 9.0, 'vcl_2_body': 8.0, ...}
after : {'fe_4': 1.0, 'fe_8': 1.0, 'vcl_4_end': 1.0}        <- fc_5/fb_6 UNKNOWN
```

The estimator now costs a pipelined candidate with the trip counts proved on the
**unpipelined** IR, plus the realiser's own reported trip count for the new kernel
loop, and refuses to guess if that is unavailable. `_r6_8_test.py` keeps the naive
estimate as a regression test: it still reads 55 against the honest 584.

## 5. Results, and the gates that rejected most candidates

Both eligible families across all six markers:

| kernel | eligible | trip | RecMII | ResMII | MII | II | stages | outcome |
|---|---|---|---|---|---|---|---|---|
| elementwise vi8/vu8 | no — no compact loop | | | | | | | — |
| elementwise vi16 | yes | 8 | 6 | 3 | 6 | 6 | 2 | **spilled → rolled back** |
| elementwise vu16 | yes | 8 | 6 | 3 | 6 | 6 | 2 | **spilled → rolled back** |
| elementwise vi32/vu32 | yes | 8 | 6 | 4 | 6 | 8 | 2 | **spilled → rolled back** |
| axpy vi8/vu8 | no — no compact loop | | | | | | | — |
| **axpy vi16** | yes | 16 | 8 | 2 | 8 | 8 | 2 | **COMMITTED** |
| **axpy vu16** | yes | 16 | 8 | 2 | 8 | 8 | 2 | **COMMITTED** |
| axpy vi32/vu32 | yes | 8 | 24 | 4 | 24 | 24 | 2 | **spilled → rolled back** |

**The zero-spill gate is what limits this milestone**, not the scheduler: 4 of 6
eligible kernels pipeline cleanly and then spill. The compact kernel keeps
`stages = 2` banks of the body live at once, and the vector body already holds
several packed 64-bit values; with 28 allocatable registers that is enough to
overflow. `axpy` survives because its body is the smallest (11 operations against
elementwise's 15).

**Simulator ticks (38-program suite):**

| program | R6.7 | R6.8 | change |
|---|---|---|---|
| axpy vi16 | 1550 | **1238** | **−20.1%** |
| axpy vu16 | 1806 | **1559** | **−13.7%** |
| all other 36 | — | unchanged | — |
| **suite total** | 136 820 | **136 261** | **−0.41%** |

**Loop and program metrics for `axpy vi16`:**

| | R6.7 | R6.8 |
|---|---|---|
| vector loop | `vcl_2_body`, 7 bundles × 16 trips | `mve_kernel_2`, 14 bundles × 7 trips |
| loop dynamic bundles | 112 | **98** |
| loop occupancy | 19.6% | **33.0%** |
| program dynamic bundles | 726 | **584** |
| dynamic IPB | 1.605 | **2.005** |
| ticks | 1550 | **1238** |

## 6. Against the R6.6A projection — where it agrees and where it does not

R6.6A projected `axpy` 7 → 4 bundles per iteration, −43%.

**Measured: 112 → 98 dynamic loop bundles, −12.5%.** The projection was an MII
lower bound; the realised kernel does not reach it. `MII = 8` and the achieved
`II = 8`, so the *schedule* is at the bound — but a compact kernel also pays a
prologue (9 ops) and an epilogue (13 ops) that the MII figure does not model, and
it covers only 14 of the 16 iterations, the rest being handled outside the loop.

**And the program-level gain is not mostly software pipelining.** Decomposing the
726 → 584 improvement:

| stage | dynamic bundles | attributable to |
|---|---|---|
| R6.7 baseline | 726 | — |
| after `promote_function` only, no modulo schedule | 663 | **R2.6 register promotion, −63 (44%)** |
| after promotion + modulo schedule | 584 | **the modulo schedule, −79 (56%)** |

The register form promotes the whole function before scheduling, so `main`'s
scalar initialisation loops get register promotion they did not previously
receive. That is a real improvement and it is validated by the same gates, but
**it is not what this milestone set out to measure**, and reporting −20.1% as
pure software pipelining would overstate the result by nearly half.

## 7. Success criteria

| # | criterion | result |
|---|---|---|
| 1 | 38/38 simulator PASS | **38/38**, 3 negative controls rejected |
| 2 | all regression suites pass | **16/16 unit suites** (incl. new `_r6_8_test.py`) |
| 3 | pipeline crosscheck passes | **PASS** — load-bearing, since R2.8 is shared with scalar SWP |
| 4 | agrees with the R6.6A projection | **partially — see §6.** Direction and ranking confirmed (AXPY profitable, the four excluded families untouched); magnitude does not match, and the reason is identified rather than modelled away |
| 5 | no regressions outside elementwise/AXPY | **IR byte-identical** for reduction, GEMM, convolution and dot (asserted in `_r6_8_test.py`); 36 of 38 programs tick-identical |

## 8. Threats to validity

* **Two programs of 38 improve.** The mechanism works and is verified, but the
  performance case rests on `axpy` at 16-bit markers.
* **44% of the headline gain is register promotion, not pipelining** (§6). The
  two are not separable in the register form, which promotes before scheduling.
* **Four of six eligible kernels are lost to register spilling.** No attempt was
  made to reduce the pipeline's register pressure — that would mean changing the
  realiser's bank allocation, beyond this milestone.
* **Elementwise, the family R6.6A rated highest (−57% projected), does not
  pipeline at all** on any marker. It is the clearest gap between the feasibility
  model and the implementation, and the cause is register pressure, not the
  schedule (its modulo schedule is found and verified at II = MII = 6).
* **A shared realiser was changed.** `_seed_cache` and `_kernel_invariants` affect
  R3.1's scalar SWP; the 124-program crosscheck passing is the evidence that it
  did not regress, not a proof.
* Ticks are the simulator's, not hardware cycles.

## 9. Follow-on

**Reduce the compact kernel's register pressure.** Every remaining opportunity in
this milestone is behind the spill gate: elementwise at four markers and axpy at
two. Options not attempted: schedule for a longer II when that lowers the number
of simultaneously live banks, or spill-aware bank allocation in
`realize_mve_kernel`.

# R6.4 — Vector Loop Unrolling

**Result: 4× unrolling is enabled by default. Measured across the 38-program
simulator suite, total ticks fall 210359 → 138014, −34.4%.** 38/38 verification
is maintained.

The headline metrics the milestone asked to raise moved only modestly — dynamic
IPB 0.744 → 0.767, occupancy 19.7% → 20.5% — while ticks fell by a third. §6
explains why, and it matters for what should come next.

---

## 1. Design

Unrolling happens **after** vectorization, inside the existing compact chunk
loop, and reuses every existing component:

```
for (i = iv; i < chunks*lanes; i += U*lanes)
    emit_body(off + 0*w, i + 0*lanes)
    emit_body(off + 1*w, i + 1*lanes)      w = lanes*eb = one packed word
    ...                                    U copies, all independent
```

`emit_body` is the **unchanged** R6.3 lowering. No new vector instruction, no new
legality analysis, no new address generation, no scheduler/bundler change. Only a
factor that **divides** the chunk count is accepted, so there is no remainder
path and the loop guard stays exact.

**One contract change was required.** `emit_body(off)` became
`emit_body(off, iv_index)`. A client that addresses chunks directly uses the byte
offset, but a client that re-emits the loop's own address computation through
`clone_offset` — GEMM's row base, a shifted convolution window — **ignores the
byte offset entirely** and re-derives the address from the induction variable. In
the first implementation every unrolled copy therefore re-derived the *same*
address and the copies were identical. The differential oracle caught it
(`differential:mismatch`) before any measurement was taken; passing the element
index per copy fixes it.

## 2. Profitability model

The existing framework is reused rather than extended:

* **correctness gate** — the packed differential oracle validates every unrolled
  candidate exactly as before; a wrong unroll rolls back to scalar rather than
  shipping;
* **cost model** — `build_compact_chunk_loop` returns instructions per *chunk*,
  normalised by the unroll factor, so every client's existing `chunks × per_iter`
  dynamic model stays correct with no client change;
* **realisation choice** — R4.2.5's size probe still measures compact against
  fully-unrolled and keeps the smaller, so unrolling competes on measured size
  rather than being imposed.

## 3. Unroll-factor comparison (38-program simulator suite)

| factor | suite ticks | vs 1× | notes |
|---|---|---|---|
| 1× | 210 359 | — | R6.3.2 baseline |
| 2× | 351 992 | **+67.3%** | **GEMM stops vectorizing** and falls back to scalar |
| **4×** | **138 014** | **−34.4%** | **best; nothing loses vectorization** |
| 8× | 139 032 | −33.9% | no further gain over 4× |

2× is not merely worse, it is pathological: all four GEMM kernels go
`vectorized 1 → 0` and run scalar (+175%). 4× and 8× keep every kernel
vectorized. 8× buys nothing over 4×, so **4× is the recommendation**.

## 4. Per-kernel measurements at 4× (simulator ticks)

| kernel | 1× | 4× | change |
|---|---|---|---|
| gemm vu32 | 38 904 | 15 495 | **−60.2%** |
| gemm vi32 | 38 136 | 15 495 | **−59.4%** |
| gemm vu16 | 24 559 | 11 905 | **−51.5%** |
| gemm vi16 | 23 791 | 11 905 | **−50.0%** |
| reduction vi32 | 884 | 690 | −21.9% |
| conv3 vi32 | 1 184 | 943 | −20.4% |
| elementwise vi32 | 1 969 | 1 571 | −20.2% |
| elementwise vu32 | 2 097 | 1 699 | −19.0% |
| conv3 vu32 | 1 326 | 1 088 | −17.9% |
| axpy vi32 | 1 827 | 1 539 | −15.8% |
| reduction vi16 | 724 | 626 | −13.5% |
| dot vi8 / vu8 | 1 680 | 1 609 | −4.2% |
| **elementwise vu16** | 1 806 | 1 922 | **+6.4%** |
| **axpy vu16** | 1 806 | 1 920 | **+6.3%** |
| **elementwise vi16** | 1 678 | 1 858 | **+10.7%** |
| **axpy vi16** | 1 550 | 1 792 | **+15.6%** |

**Four kernels regress**, all of them 16-bit axpy/elementwise. They are reported
because they are real: 4× is a net win by a wide margin, not a uniform one.

## 5. Convolution kernel in detail (guarded harness)

| factor | instr/output | bundles/output | ticks | dyn IPB | occupancy |
|---|---|---|---|---|---|
| 1× | 2.125 | 1.125 | 1798 | 0.744 | 19.7% |
| 2× | 1.812 | 0.625 | 1687 | 0.751 | 19.7% |
| 4× | — (fully unrolled) | — | **1561** | **0.767** | **20.5%** |
| scalar | 13.000 | 7.000 | 3077 | — | — |

Against scalar, the convolution kernel is now **−49.3% ticks**.

Every number above comes from a harness that refuses to report unless the kernel
vectorized, the emitted mcode contains `$v` **and** funnel-shift instructions,
simulator verification passed, **and** the tick count differs from the scalar
baseline. No scalar-fallback measurement is possible.

## 6. Dynamic IPB and occupancy — an honest reading

The milestone's stated goal was to raise dynamic IPB and occupancy. They rose,
but barely: **IPB 0.744 → 0.767 (+3.1%)**, **occupancy 19.7% → 20.5% (+0.8pp)**,
while ticks fell 13% on that kernel and 34% across the suite.

That is not a contradiction, and it is worth stating plainly: **unrolling removed
work rather than packing it denser.** Each unrolled iteration amortises one
compare, one branch and one induction-variable update across four chunks instead
of one, so the dynamic instruction count falls (1337 → 1198 on the convolution
kernel) and the bundle count falls with it. The remaining bundles are just as
sparse as before.

So R6.1's diagnosis still stands: ~80% of vector issue slots are idle, and
unrolling by itself did not fill them. The dependence chain within a chunk
(`load → shift → or → vadd → vadd → store`) is unchanged, and the copies, though
independent, are not being interleaved into the same bundles. Reaching high
occupancy needs the scheduler to interleave them — software pipelining, or a
scheduler that looks across the unrolled copies — which is explicitly out of
scope here.

## 7. Final recommendation

**Adopt 4× (done, it is the default).** Keep `APARA_VECTOR_UNROLL` as the
override.

Do **not** adopt 2×: it silently costs GEMM its vectorization.

**Adaptive per-kernel selection is the natural follow-on and was not
implemented.** The evidence for it is in §4: a per-kernel choice would keep the
50–60% GEMM win while leaving the four 16-bit axpy/elementwise kernels at 1×,
avoiding their 6–16% regressions. Implementing it means extending R4.2.5's size
probe to the unroll dimension — but note its current objective is *static size*,
which is the wrong objective for unrolling (it would always prefer 1×). A correct
adaptive selector needs a dynamic objective, which is a design decision rather
than a mechanical extension, and I did not want to guess at it.

## 8. Regression

| check | result |
|---|---|
| simulator verification, 1× / 2× / 4× / 8× | **38/38 PASS at every factor** |
| `pipeline_crosscheck` | **PASS 124/124** |
| unit suites (all 15) | **all pass** |

Two suites needed updating, both for R6.4's *intended* static-size trade rather
than for correctness, and both by pinning the factor rather than dropping the
check:

* `_r4_2_5` asserts the realisation choice reduces static size versus
  always-unrolled. Unrolling deliberately trades size for ILP, so that property
  is now verified with the factor pinned to 1×, where it is the R4.2.5 property
  being tested.
* `_r4_3` asserts the compact realisation remains *reachable* — that the probe
  measures rather than hard-codes. With unrolling the compact form is larger and
  is chosen less often, so this too is checked at 1×.

## 9. Threats to validity

* **The 2× GEMM pathology is unexplained.** GEMM loses vectorization at 2× but
  not at 4× or 8×. It is reported and avoided, not diagnosed; something about
  that factor fails validation or register allocation for GEMM's chunk counts.
* **4× is a global default justified by an aggregate**, and four kernels regress
  under it. That is the case for adaptive selection, not against 4×.
* One kernel shape drove the detailed convolution measurements; the suite figures
  cover 38 programs across all six packed markers.
* Ticks are the simulator's, not hardware cycles.

# R15.0 — Hand-written parity analysis

Compiler at `c2849fa` (R14.8 active). **ANALYSIS ONLY — 0 production `.py`
changed.** Frozen tags untouched, nothing pushed.

## Correction to an earlier figure

I previously quoted the compiler at **3.00 ticks/output kernel-only**. That
counted only the hot block `fb_10` and silently ignored the surrounding
loop-control blocks (`fc_9`, `fb_6`, `fe_12`, `fc_5`), which cost another
**456 ticks**. The matched figure is **4.79 ticks/output**, and the gap to
hand-written is **3.96×**, not 2.5×. The table below is the corrected accounting.

## 1. Matched workloads

Both compute the **same 256 outputs** from the **same inputs**
(`A[i]=(i*7+3)%17`, `Bt[i]=(i*11+5)%19`), 16×16 `vu8`, verified against the same
`expected.result`: **256 PostConditions, 0 errors, both**.

The one legitimate asymmetry: the hand-written kernel gets its data **preloaded
via `data.map`**, the compiler **builds it with an init loop**. So the comparison
below is the **matmul nest only**, with the compiler's init excluded — otherwise
it is not like-for-like.

| | hand-written | compiler (matmul nest) |
|---|---|---|
| ticks | **309** | **1225** |
| **ticks/output** | **1.207** | **4.79** |
| instructions | 1160 | — |
| bundles (static) | 21 | 12 (`fb_10`) + 21 (control blocks) |
| **IPB** | **3.754** | 3.667 (`fb_10`) |
| occupancy | 46.9% | 45.8% (`fb_10`) |
| spills | 0 | **0** |
| correctness | 256/256 | 256/256 |

**On density they are level (IPB 3.667 vs 3.754). On speed the compiler is
3.96× behind.** Same packing, ~4× the cycles — which is exactly why IPB is not
the metric.

## 2. Where the compiler's 1225 ticks go

Attributed from the simulator trace by bundle PC, not estimated:

| block | ticks | share | role |
|---|---|---|---|
| `fb_10` | **768** | 63% | the vector work — 12 bundles × 64 tiles |
| `fc_9` | **176** | 14% | tile-loop test — **3 single-instruction bundles** |
| `fb_6` | **128** | 10% | row prologue, re-derives addresses per row |
| `fe_12` | **96** | 8% | accumulator write-back to stack slots |
| `fc_5` | 52 | 4% | row-loop control |

**37% of the matmul nest is loop control and bookkeeping outside the hot block.**

For context on the *whole* program: init is **3331 of 4575 ticks (73%)**. The
hand-written kernel has none of it.

## 3. The structural difference

**The compiler runs 64 tile iterations of 4 outputs; the hand-written kernel
runs 16 row iterations of 16 outputs.** Per-iteration overhead — loop test, row
prologue, accumulator write-back — is therefore paid **4× more often**.

`fc_9` is the clearest example. Compiler, 3 serial bundles:

```
- $r10 ($i64) $r31 16          compute the trip test
? ($i64) $r10 < $goto fb_10    conditional branch
? ($i64) $r0  == $goto fe_12   unconditional fall-through branch
```

Hand-written, **zero extra bundles** — the test folds into the last store bundle
and uses the ISA's auto-decrement:

```
? ($i16) $r31 > --1 $goto row_loop | + $r29 ($i64) $r29 128
```

## 4. Widening J_TILE does NOT help — measured

The obvious inference from §3 is "process more columns per iteration". Measured,
it is wrong:

| J_TILE | total ticks | `fb_10` bundles/output | verdict |
|---|---|---|---|
| **4** | **4575** | 3.00 | **best** |
| 8 | 7950 | 2.25 | −74% worse |
| 16 | 11287 | 2.30 | −147% worse |

The hot block *does* get denser per output, but at J_TILE=8 two large new blocks
appear — `fi_3` (768 ticks) and `fe_16` (704) — and init grows 3331 → 4868. Net
regression. All correct (256/256), so this is a cost, not a bug.

**J_TILE=4 is the compiler's measured optimum.** "Raise J_TILE" is a dead end
until whatever generates `fi_3`/`fe_16` is understood.

## 5. Lower bounds for `fb_10`

| bound | bundles |
|---|---|
| memory lanes ((8 ld + 4 st) / 4) | 3 |
| issue width (44 instrs / 8) | 6 |
| **RAW dependence height** | **9** |
| **shipped** | **12** |

The binding constraint is **dependence**, and there are **3 bundles of slack**.
Per-bundle occupancy `[5,5,1,1,4,8,7,5,1,1,1,5]` — five single-instruction
bundles, all address chains: the B-row address (b3–b4) and the store base
(b9–b11).

**The hand-written kernel is not at a lower bound either** — R14.0 measured that
issuing 8 `$dot`s per bundle instead of 4 takes it from 309 → **241 ticks**. It
leaves ~22% on the table. Parity with it is therefore not the ceiling.

## 6. Gap table, ranked by measured ticks

| gap | compiler | hand-written | ticks | share of nest | pass responsible | generic? |
|---|---|---|---|---|---|---|
| tile-loop control | 3 serial bundles | folded, 0 bundles | **176** | 14.4% | loop-control codegen | **yes — every counted loop** |
| `fb_10` slack | 12 bundles vs height 9 | — | **192** | 15.7% | scheduler / allocator | yes |
| row prologue | 8 bundles × 16 | ~1 | **128** | 10.4% | LICM / address gen | yes |
| accumulator write-back | 6 bundles × 16 | none | **96** | 7.8% | mem2reg / vector acc | yes |
| iteration count | 64 tiles | 16 rows | — | — | J_TILE selection | **blocked (§4)** |

The two already investigated: the store-base chain inside that slack was
attacked by **R14.9 (LICM — premise invalid)** and **R14.10 (IVSR — blocked by
`_decompose`)**. Both correctly stopped.

## 7. Answers

> **"What prevents the current compiler-generated matmul from being as tight as
> the hand-written kernel?"**

Not vector quality — the hot block matches on IPB and is within 3 bundles of its
dependence lower bound. It is that the compiler **produces 4 output columns per
loop iteration where the hand-written kernel produces 16**, and then pays a
**structurally more expensive per-iteration cost**: a 3-bundle loop test the hand
kernel folds into an existing bundle, a row prologue that re-derives addresses,
and accumulator write-backs the hand kernel does not have. Together that is
**37% of the compiler's matmul nest**. The obvious fix — widen J_TILE — is
**measured to regress**.

> **"What is the ONE compiler change with the highest measured potential?"**

**Collapse the counted-loop exit test from three bundles to one.** `fc_9` is
3 single-instruction bundles executed 58 times = **176 ticks, 14.4% of the
matmul nest**, for what the ISA can express in one instruction folded into an
existing bundle (`? ($i16) $r31 > --1 $goto`, using the auto-decrement the
hand-written kernel uses and the assembler already supports).

It is measurable, repeated, compiler-controlled, and **generic to every counted
loop in every program** — not a matmul special case.

### Quantified

| | value |
|---|---|
| compiler ticks/output (matmul nest) | **4.79** |
| hand-written ticks/output | **1.207** |
| compiler bundles (`fb_10`) | 12 |
| hand-written bundles | 21 (whole kernel) |
| dependence lower bound (`fb_10`) | **9** |
| remaining gap | **3.96×** |
| estimated gain from the proposed change | **~116 ticks** = 9.5% of the matmul nest, **≈2.5% whole-program** |

## 8. Recommendation: **FREEZE**

Not because the gap is small — 3.96× is not small — but because **no remaining
lever is worth a broad compiler transformation**:

- every identified item is worth **2–4% whole-program**;
- the matmul nest is only **27%** of the program (init is 73%), which caps any
  of them;
- the one structural lever (J_TILE) **measurably regresses**;
- the two natural attacks on the largest slack were already analysed and
  correctly stopped (R14.9, R14.10);
- and the hand-written reference is itself **~22% off its own optimum**, so
  "parity" is not a principled target.

The largest whole-program lever is not an optimization at all: **init is 73% of
ticks**, and `--dmem-init` already exists to preload globals instead of building
them at runtime. That changes the benchmark's shape (it needs globals with
initializers rather than locals), so it is a **measurement-methodology** choice,
not a compiler change — and it is worth far more than anything above.

**No implementation. R16 not started.**

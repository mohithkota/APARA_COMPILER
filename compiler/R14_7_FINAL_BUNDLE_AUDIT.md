# R14.7 — Final instruction-level bundle audit

Compiler at R14.6 (`a492fc4`). **ANALYSIS ONLY — 0 production `.py` files
changed.** Frozen tags untouched, nothing pushed.

## Answer to the final question

> "For every instruction in the hot matmul block, why is it in its current
> bundle, and which exact dependencies prevent it from being packed into an
> earlier/lower bundle?"

Two populations, and they behave completely differently:

* **The vector work (bundles 1–8) is at its dependence lower bound.** Every
  `$dot` sits exactly at its ASAP position (slack 0). The only slack is 1 bundle
  on four loads, which the bundler used to fill an already-open bundle.
* **The scalar epilogue (bundles 9–24) is not blocked by data dependence at
  all.** Its instructions have ASAP positions of 2–9 but are placed in bundles
  9–24 — **slack up to 18 bundles on a single instruction**. They are serialized
  by **register reuse between four otherwise independent store chains**, which a
  non-reordering greedy bundler cannot undo.

**Dependence height 9. Shipped bundles 24. Slack 15 — 62% of the hot block.**

## 1–4. Instruction → bundle mapping (primary kernel, complete)

`ASAP` = earliest bundle permitted by RAW alone. `slack` = shipped − ASAP.

```
vu8_t 16x16 JT=4   block fb_10   24 bundles, 55 instructions
====================================================================================================
  I#   B  sl class              instruction                                          RAWpreds     ASAP slack
   1   1   0 scalar-arith/addr  + $r10 ($i64) $r31 0                                 []              1     0
   2   1   1 scalar-arith/addr  + $r5 ($i64) $r0 0                                   []              1     0
   3   1   2 scalar-arith/addr  + $r6 ($i64) $r0 0                                   []              1     0
   4   1   3 scalar-arith/addr  + $r7 ($i64) $r0 0                                   []              1     0
   5   1   4 scalar-arith/addr  + $r8 ($i64) $r0 0                                   []              1     0
   6   2   0 scalar-arith/addr  << $r11 ($i64) $r10 4                                [1]             2     0
   7   2   1 scalar-arith/addr  + $r16 ($i64) $r0 $r5                                [2]             2     0
   8   2   2 scalar-arith/addr  + $r18 ($i64) $r0 $r6                                [3]             2     0
   9   2   3 scalar-arith/addr  + $r20 ($i64) $r0 $r7                                [4]             2     0
  10   2   4 scalar-arith/addr  + $r10 ($i64) $r0 $r8                                [5]             2     0
  11   3   0 scalar-arith/addr  + $r12 ($i64) $r11 0                                 [6]             3     0
  12   4   0 scalar-arith/addr  + $r3 ($i64) $r15 $r12                               [11]            4     0
  13   5   0 vector-ld/st       $ld ($u64) $r4 [$r3 + 0]                             [12]            5     0
  14   5   1 vector-ld/st       $ld ($u64) $r2 [$r3 + 16]                            [12]            5     0
  15   5   2 vector-ld/st       $ld ($u64) $r17 [$r3 + 32]                           [12]            5     0
  16   5   3 vector-ld/st       $ld ($u64) $r19 [$r3 + 48]                           [12]            5     0
  17   6   0 dot                $dot $accumulate $r16 ($vu8) $r29 $r4                [7, 13]         6     0
  18   6   1 dot                $dot $accumulate $r18 ($vu8) $r29 $r2                [8, 14]         6     0
  19   6   2 vector-ld/st       $ld ($u64) $r21 [$r3 + 8]                            [12]            5     1
  20   6   3 dot                $dot $accumulate $r20 ($vu8) $r29 $r17               [9, 15]         6     0
  21   6   4 vector-ld/st       $ld ($u64) $r11 [$r3 + 24]                           [12]            5     1
  22   6   5 dot                $dot $accumulate $r10 ($vu8) $r29 $r19               [10, 16]        6     0
  23   6   6 vector-ld/st       $ld ($u64) $r12 [$r3 + 40]                           [12]            5     1
  24   6   7 vector-ld/st       $ld ($u64) $r4 [$r3 + 56]                            [12]            5     1
  25   7   0 dot                $dot $accumulate $r16 ($vu8) $r30 $r21               [17, 19]        7     0
  26   7   1 dot                $dot $accumulate $r18 ($vu8) $r30 $r11               [18, 21]        7     0
  27   7   2 dot                $dot $accumulate $r20 ($vu8) $r30 $r12               [20, 23]        7     0
  28   7   3 scalar-arith/addr  + $r2 ($i64) $r9 $r31                                []              1     6
  29   7   4 dot                $dot $accumulate $r10 ($vu8) $r30 $r4                [22, 24]        7     0
  30   7   5 scalar-arith/addr  + $r31 ($i64) $r31 4                                 []              1     6
  31   8   0 scalar-arith/addr  + $r17 ($i64) $r2 0                                  [28]            2     6
  32   8   1 scalar-arith/addr  + $r5 ($i64) $r0 $r16                                [25]            8     0
  33   8   2 scalar-arith/addr  + $r21 ($i64) $r2 1                                  [28]            2     6
  34   8   3 scalar-arith/addr  + $r6 ($i64) $r0 $r18                                [26]            8     0
  35   8   4 scalar-arith/addr  + $r11 ($i64) $r2 2                                  [28]            2     6
  36   8   5 scalar-arith/addr  + $r7 ($i64) $r0 $r20                                [27]            8     0
  37   8   6 scalar-arith/addr  + $r4 ($i64) $r2 3                                   [28]            2     6
  38   8   7 scalar-arith/addr  + $r8 ($i64) $r0 $r10                                [29]            8     0
  39   9   0 scalar-arith/addr  << $r19 ($i64) $r17 3                                [31]            3     6
  40  10   0 scalar-arith/addr  + $r3 ($i64) $r19 0                                  [39]            4     6
  41  11   0 scalar-arith/addr  + $r3 ($i64) $r28 $r3                                [40]            5     6
  42  12   0 mem                $st ($i64) [$r3 + 0] $r5                             [32, 41]        9     3
  43  13   0 scalar-arith/addr  << $r3 ($i64) $r21 3                                 [33]            3    10
  44  14   0 scalar-arith/addr  + $r12 ($i64) $r3 0                                  [43]            4    10
  45  15   0 scalar-arith/addr  + $r12 ($i64) $r28 $r12                              [44]            5    10
  46  16   0 mem                $st ($i64) [$r12 + 0] $r6                            [34, 45]        9     7
  47  17   0 scalar-arith/addr  << $r12 ($i64) $r11 3                                [35]            3    14
  48  18   0 scalar-arith/addr  + $r16 ($i64) $r12 0                                 [47]            4    14
  49  19   0 scalar-arith/addr  + $r16 ($i64) $r28 $r16                              [48]            5    14
  50  20   0 mem                $st ($i64) [$r16 + 0] $r7                            [36, 49]        9    11
  51  21   0 scalar-arith/addr  << $r16 ($i64) $r4 3                                 [37]            3    18
  52  22   0 scalar-arith/addr  + $r17 ($i64) $r16 0                                 [51]            4    18
  53  23   0 scalar-arith/addr  + $r17 ($i64) $r28 $r17                              [52]            5    18
  54  24   0 mem                $st ($i64) [$r17 + 0] $r8                            [38, 53]        9    15
  55  24   1 scalar-control     ? ($i64) $r0 == $goto fc_9                           []              1    23

  per-bundle counts: [5, 5, 1, 1, 4, 8, 6, 8, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2]
  mean 2.29  median 1  min 1  max 8
  dependence height (RAW ASAP) = 9   shipped bundles = 24   slack = 15
  class counts: {'scalar-arith/addr': 34, 'vector-ld/st': 8, 'dot': 8, 'mem': 4, 'scalar-control': 1}
  execution frequency = 64  =>  weighted ticks = 1536

====================================================================================================```

Instruction roles, from the table above:
* **bundle-starting**: I1, I6, I11, I12, I13, I17, I25, I31/I32, I39, I40, I41,
  I42, I43 … I54 — in the epilogue *every* instruction starts its own bundle.
* **bundle-filling**: I2–I5, I7–I10, I14–I16, I18–I24, I26–I29, I33–I38.
* **dependency-blocked**: I17–I29 (true RAW on the loads and the accumulators).
* **independent filler**: I28, I30 (loop bookkeeping, ASAP 1, parked at bundle 7
  to fill spare slots — correct behaviour).

## 4b. Split reasons — from the bundler's own instrumentation

`APARA_BUNDLE_STATS=1`, block `fb_10`:

| kernel | splits | RAW | Label | WAW / MemAlias / MemPhase / MemLane / FUnit / BundleFull |
|---|---|---|---|---|
| vu8 16×16 | 8 | **7 (87.5%)** | 1 | **0** |
| vu8 32×32 | 10 | **9 (90.0%)** | 1 | **0** |
| vi16 16×16 | 11 | **10 (90.9%)** | 1 | **0** |
| vi16 32×32 | 39 | **38 (97.4%)** | 1 | **0** |

**No hardware limit is ever reached** — zero MemLane (4 ld/st), zero FUnit, zero
BundleFull. The block is purely dependence-split. The `Label` split is the block
entry itself.

## 3. Instructions per bundle

| kernel | per-bundle counts | mean | median | min | max |
|---|---|---|---|---|---|
| vu8 16×16 | 5,5,1,1,4,8,6,8, then **1×15**, 2 | 2.29 | **1** | 1 | 8 |
| vu8 32×32 | 5,4,1,2,4,8,8,8,6,7,2, then **1×14**, 2 | 2.73 | **1** | 1 | 8 |
| vi16 16×16 | 5,3,2,1,2,4,8,8,8,6,7,2, then **1×14**, 2 | 2.67 | **1** | 1 | 8 |
| vi16 32×32 | 5,3,2,2,1,1, then 2×29, 3,3,2,1 | 2.08 | 2 | 1 | 5 |

The **median of 1** in three of four kernels is the whole story: the mean hides a
bimodal distribution — a dense vector head (up to 8/bundle) and a long tail of
single-instruction bundles.

## 5–6. Scalar epilogue and the result-store chains

Four output stores, each built as a 4-instruction chain:

```
<<  rX, rIdx, 3        index * 8              (address)
+   rY, rX, 0          copy                   (address)
+   rY, $r28, rY       add global base         (address)
$st ($i64) [rY + 0], rV                        (result store)
```

| store | value producer | address chain | shipped bundle | ASAP | slack |
|---|---|---|---|---|---|
| `results[..+0]` | I32 (from I25 `$dot`) | I39→I40→I41 | **B12** | 9 | 3 |
| `results[..+1]` | I34 (from I26) | I43→I44→I45 | **B16** | 9 | **7** |
| `results[..+2]` | I36 (from I27) | I47→I48→I49 | **B20** | 9 | **11** |
| `results[..+3]` | I38 (from I29) | I51→I52→I53 | **B24** | 9 | **15** |

**All four have identical ASAP = 9.** They are genuinely independent — different
output elements, no memory aliasing (the bundler reports zero MemAlias here).
They finish 12 bundles apart purely because of ordering.

**Cause — register reuse, measured:**

| register | bundles it appears in (epilogue) |
|---|---|
| `$r3` | 10, 11, 11, 12, **13, 14** |
| `$r12` | 14, 15, 15, 16, **17, 18** |
| `$r16` | 18, 19, 19, 20, **21, 22** |
| `$r17` | 9, 22, 23, 23, 24 |

Each chain's address register is **immediately reused by the next chain**. Chain
2 cannot start until chain 1's store has read `$r3`. The bundler is a
**greedy forward pass that does not reorder** (`_pack_bundles`), so it cannot
interleave them, and it has **no WAR rule** — the reasons it can emit are RAW,
WAW, MemAlias, MemPhase, MemLane, FUnit, Control, Call, Label, BundleFull. The
serialization is therefore imposed *before* the bundler, by instruction order
plus allocation.

This is a **register-allocation artifact, not a true dependence**.

## 7. Address-generation table

| address value | consumer | bundle | invariant in block? | shared? | constant-delta partner |
|---|---|---|---|---|---|
| `$r3 = $r15 + $r12` (B row base) | all 8 packed loads | B4 | yes (computed once) | **yes — R14.2** | the 8 loads use `[+0/8/16/24/32/40/48/56]` |
| `$r29`, `$r30` (A row halves) | all 8 `$dot` | hoisted **outside** the block | yes | yes | — |
| `<<`,`+`,`+` per output store ×4 | the 4 `$st` | B9–B23 | no (depends on i,j,t) | **no** | **yes — deltas 0/8/16/24, unexploited** |

R9.3 `[reg+imm]` and R14.2 cross-reduction sharing are **fully active on the
loads** (one base, eight immediates, one bundle). Neither reaches the stores,
because the stores are source-level statements outside the vector region.

## 8. Vector vs scalar

vu8 16×16, 55 instructions:

| class | count | share |
|---|---|---|
| **scalar-arith/addr** | **34** | **62%** |
| vector load | 8 | 15% |
| `$dot` | 8 | 15% |
| result store | 4 | 7% |
| scalar control | 1 | 2% |

| region | bundles | instrs | instrs/bundle |
|---|---|---|---|
| vector head (B1–B8) | 8 | 42 | **5.25** |
| scalar epilogue (B9–B24) | 16 | 13 | **0.81** |

## 9. Dependence DAG

| kernel | dependence height (RAW ASAP) | shipped bundles | **slack** |
|---|---|---|---|
| vu8 16×16 | 9 | 24 | **15** |
| vu8 32×32 | 11 | 26 | **15** |
| vi16 16×16 | 12 | 27 | **15** |
| vi16 32×32 | 15 | 39 | **24** |

**The scheduler is NOT at the dependence lower bound.** In the vector head it is
(slack 0 on every `$dot`); across the whole block there is 15–24 bundles of
slack, all of it in the epilogue.

## 10. "Why can't this instruction move?" — bundles under 4 instructions

| bundle | instrs | next blocked instruction | exact blocking dependency | independent work available? |
|---|---|---|---|---|
| B3 | 1 | I12 `+ $r3,$r15,$r12` | **true RAW** on `$r12` (I11) | no — everything else already issued |
| B4 | 1 | I13 `$ld [$r3+0]` | **true RAW** on `$r3` (I12) | no |
| B9 | 1 | I40 | RAW on `$r19` (I39) | **YES** — I43/I47/I51 are ready (ASAP 3) but write `$r3`/`$r12`/`$r16`, reused |
| B10 | 1 | I41 | RAW on `$r3` (I40) | **YES**, same reuse |
| B11 | 1 | I42 `$st` | RAW on `$r3` (I41) | **YES**, same reuse |
| B13 | 1 | I44 | RAW on `$r3` (I43) | **YES** — chains 3 and 4 ready, blocked by `$r12`/`$r16` reuse |
| B17 | 1 | I48 | RAW on `$r12` (I47) | **YES** — chain 4 ready |
| B21 | 1 | I52 | RAW on `$r16` (I51) | **YES** — nothing left but it *could* have run at ASAP 3 |

**B3 and B4 are mandatory** — true RAW, no independent ready instruction.
**B9–B23 are not**: independent work exists at every one of them, and it is
blocked by a reused address register rather than by data.

## 11. Frequency-weighted cost

| kernel | block bundles | executions | **weighted ticks** | whole-program ticks | block share |
|---|---|---|---|---|---|
| vu8 16×16 | 24 | 64 | **1536** | 5343 | 29% |
| vu8 32×32 | 26 | 256 | **6656** | 22175 | 30% |
| vi16 16×16 | 27 | 64 | **1728** | 5023 | 34% |
| vi16 32×32 | 39 | 256 | **9984** | 31264 | 32% |

Of each block's weighted cost, the epilogue is **16/24 = 67%** (vu8 16×16), i.e.
~1024 of 1536 weighted ticks. Prologue bundles are counted once and are
negligible; the figures above are hot-loop only.

## 12. Cross-kernel comparison

| pattern | vu8 16 | vu8 32 | vi16 16 | vi16 32 | verdict |
|---|---|---|---|---|---|
| splits are ~90–97% RAW, zero resource splits | ✓ | ✓ | ✓ | ✓ | **high confidence** |
| vector head at dependence lower bound | ✓ | ✓ | ✓ | ✓ | **high confidence** |
| 4-instruction store chain per output | ✓ | ✓ | ✓ | ✓ | **high confidence** |
| store chains serialized by register reuse | ✓ | ✓ | ✓ | ✓ | **high confidence** |
| slack ≥ 15 bundles | 15 | 15 | 15 | 24 | **high confidence** |
| median instructions/bundle = 1 | ✓ | ✓ | ✓ | ✗ (2) | kernel-dependent detail |

Every load-bearing pattern reproduces in all four kernels.

## 14. Classification and the single recommendation

**Primary: D — register-allocation artifact** (four independent store chains
given overlapping address registers), compounded by **C — unnecessary scalar
instructions** (each store address rebuilt from scratch instead of sharing a base
with the constant deltas 0/8/16/24 that `constant_delta` already proves).

Not A (the vector head is the only true-dependency region and is already
optimal), not E (zero MemAlias), not F (zero MemLane/FUnit — no ISA limit is
reached), not G (one control instruction in 55).

### ONE recommended next step

**Give the independent result-store chains disjoint address registers, and/or
share one base with constant displacements.**

The evidence: identical ASAP for all four stores, 15–24 bundles of slack, the
exact reused registers, and the same pattern in all four kernels. Upper bound if
the epilogue reached its ASAP: 24 → ~11 bundles for vu8 16×16, i.e. **~54% of
the hot block**, worth roughly 29% × 54% ≈ **16% of whole-program ticks**.

**This is not a FREEZE recommendation** — a clear, repeated, quantified
opportunity exists. But it lives in the **register allocator / scalar optimizer**,
not in the vector pipeline, which this audit shows is already at its dependence
lower bound.

**Not implemented. No production file changed.**

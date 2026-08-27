# R17.3 — Final live-state and instruction-cost analysis

Compiler at `4937f2f` (R17.2). **ANALYSIS ONLY — 0 production `.py` changed,
nothing pushed, no tag touched.** Every what-if is a hand edit of emitted mcode,
assembled and executed on the real simulator against the same golden reference.

## The gap is 458 ticks, not 554

The brief's 554 is the pre-R17.1 figure (795 − 241). Re-measured here:

| | compiler JT=8 | hand-written JT=8 |
|---|---:|---:|
| ticks | **699** | **241** |
| static bundles | 54 | 17 |
| executed instructions | 2715 | 1160 |
| IPB | 3.884 | 4.813 |
| `$dot` / bundle | 4 | 8 |
| peak live registers | **19** | **22** |
| spills | 0 | 0 |
| correctness | 256/256 | 256/256 |

**Gap = 458 ticks.**

## 1. What exact state makes the hand-written kernel tighter?

Not register *count* — the hand-written kernel holds **more** live state than the
compiler (22 vs 19). What differs is **lifetime disjointness**.

Live registers per bundle, measured by cyclic backward dataflow over each loop
body (VLIW semantics: all operands read before any result is written):

```
HAND-WRITTEN row body (14 bundles, 16 output columns)
  b0  row_loop   live= 5   acc_live=0
  b1  load1      live= 6   acc_live=0      <-- LOAD PHASE: no accumulator live
  b2  load2      live=14   acc_live=0
  b3  dotA       live=22   acc_live=0      <-- 8 B pairs live, accumulators NOT yet born
  b4  dotB       live=22   acc_live=8      <-- B lo-halves dead, accumulators now live
  b5  store1     live=14
  ...
COMPILER fb_6 (12 bundles, 8 output columns)
  b0             live= 6   acc_live=0
  b1             live=14   acc_live=7
  b2             live=15   acc_live=8      <-- accumulators live from here to the end
  b3  ld+dot     live=19   acc_live=8
  ...
  b10            live=15   acc_live=8
```

**The hand-written kernel's accumulators are live in 6 of 14 bundles and are
never live during its load phase. The compiler's are live in 9 of 12.**

The cause is one lowering decision: **the hand-written kernel's *first* `$dot` of
each accumulation chain is a plain `$dot`, which *defines* the accumulator.** The
compiler emits `$dot $accumulate` for every chunk, so it must zero-initialise all
eight accumulators first, and they are then live across the whole body.

That is the exact state difference. Its consequence is register *availability
during the load phase*: the hand-written kernel has 8 registers free there and
spends them on 8 wide B pairs (16 registers).

## 2. Live-value classes, side by side

Per iteration of each loop body:

| class | compiler | hand-written | lifetime | why live |
|---|---:|---:|---|---|
| accumulators | 8 | 8 | **compiler: whole body (9/12 bundles); hand-written: after the first dot only (6/14)** | compiler zero-inits them; hand-written defines them with a non-accumulating `$dot` |
| A operands | 2 (`$r30`, `$r31`) | 2 (`$r2:$r3` pair) | whole body, both | hoisted out of the inner loop by both |
| B operands | **4–8 narrow, reloaded** | **16 (8 wide pairs)** | compiler: 1–2 bundles each; hand-written: lo dies at dotA, hi at dotB | compiler reloads in two waves; hand-written holds a whole row-block |
| address / base | 5 (`$r1`,`$r6`,`$r7`,`$r18`,GBASE) | 4 (`$r28`,`$r29`,`$r30`,`$r31`) | whole body, both | compiler **recomputes**; hand-written **increments** |
| store values | reuse accumulators | reuse accumulators | — | same |
| loop / control | `j` + `i` (2 loops) | 1 counter | whole body | compiler has a j-loop; hand-written has none |
| copies | 3 executed | 0 | — | R16.5 + R17.1 already removed the rest |
| **peak live** | **19 / 28** | **22 / 31** | | |

## 3. Compiler-only instructions, classified

Executed instruction counts, whole program (R17.0's method, re-measured at R17.1):

| class | compiler | hand-written | extra |
|---|---:|---:|---:|
| `$dot` (compute) | 512 | 544 | −32 |
| result stores | 257 | 272 | −15 |
| loads | 690 | 289 | +401 |
| **address / bookkeeping** | **~620** | **37** | **+583** |
| **accumulator zero-init** | **274** | **0** | **+274** |
| **accumulator write-back** | **162** | **0** | **+162** |
| loop control | 134 | 18 | +116 |
| **total** | **2715** | **1160** | **+1555** |

## 4. The decisive finding: most compiler-only instructions are FREE

`fb_6` occupancy is `[8, 2, 4, 8, 8, 8, 7, 1, 1, 1, 4, 5]` — **12 bundles, 57
instructions, 96 slot-capacity, 39 free slots (41%)**. Instructions that land in
those free slots cost **nothing**.

Two what-ifs prove it, both measured:

| what-if | instructions | bundles in `fb_6` | ticks | verdict |
|---|---:|---:|---:|---|
| R17.1 baseline | 57 | 12 | **699** | — |
| **remove 8 accumulator zero-inits** (first `$dot` non-accumulating) | **49** | **12 (unchanged)** | 795* | **0 bundles saved** |
| R17.2 wide loads (18 narrow → 2+8 wide) | −256 executed | 13 | 891 | **worse** |

*The 795 is an artifact of hand-editing: `mcode_align` re-padded (14 → 17 pad
bundles) because the emitted bundle widths changed. The load-bearing measurement
is the **bundle count, which did not move**: `[8,2,...]` became `[1,1,...]`.

Why zero-init is free: `b0` holds `<< $r2 = $r18 << 4` and `b1` holds
`+ $r7 = $r6 + $r2`. Those are a **serial RAW dependence** and can never share a
bundle. Both bundles exist regardless; the eight zero-inits merely fill slots
that would otherwise be `$null`.

**This is the same phenomenon R17.2 found for loads** — 12 of 16 narrow loads
ride free alongside `$dot`s in full 8-slot bundles.

## 5. Why the compiler cannot reach 8 `$dot` per bundle

**Not register count.** The reason is the 4-per-bundle memory-lane cap
(`bundler.py:751`) interacting with narrow loads:

| schedule | load bundles | dot bundles | total |
|---|---:|---:|---:|
| **compiler today**: 4 `$dot` + 4 `$ld` co-issued, 8 slots full | — (co-issued) | 4 + 1 prologue | **5** |
| 8-wide `$dot`, narrow loads | 16 loads / 4 lanes = **4** | 16 dots / 8 = **2** | **6** |
| 8-wide `$dot`, wide loads | 8 loads / 4 lanes = **2** | **2** | **4** |

A bundle cannot hold 4 loads *and* 8 dots (12 > `ISSUE_WIDTH = 8`), so with
narrow loads the load and dot bundles are disjoint and 8-wide is **strictly
worse** (6 > 5). **The compiler's 4-wide schedule is optimal for narrow loads.**
8 `$dot` per bundle is only reachable with wide loads, which halve the mem-op
count to 2 bundles.

## 6. What the 241-tick schedule actually requires — quantified

| requirement | needed | compiler has |
|---|---|---|
| 8 accumulators | yes | yes (8) |
| **8 B operand pairs live at once** | **16 registers** | needs 8 free during the load phase |
| A operand pair | 2 registers, even-aligned | `$r30:$r31` — already even-aligned |
| **accumulators NOT live during loads** | **required** | **no — zero-init makes them live** |
| special register placement | even-aligned pairs for `$ld ($u128)` | supported (`borrow_pair`) |
| **absence of ABI-reserved registers** | hand-written reserves only `$r0` (31 available) | compiler reserves `$r0`/FP/SP/GBASE (28) |
| peak live | **22** | 28 available |

**Peak live is 22 — well inside the compiler's 28.** The ABI reservation is *not*
what blocks the schedule.

### Correction to R17.2

R17.2 stated the schedule needs "8 acc + 16 B + 2 A + 4 control = 30 > 28, short
by exactly two — FP and SP." **That count was wrong**: it summed classes that are
never simultaneously live. The hand-written kernel's measured peak is **22**,
because its accumulators are not live while its B pairs are.

The corrected statement: the register block is **real but conditional**, and the
condition is the compiler's own lowering, not the ABI —

* with zero-init (today): 16 B + 2 A + 4 bases + **8 accumulators** = **30 > 28** ✗
* with a non-accumulating first `$dot`: 16 B + 2 A + 4 bases = **22 ≤ 28** ✓

**R17.2's conclusion still stands** — wide loads measured 891 vs 699 — but its
*third reason* was misattributed. The true reasons are #1 (wide loads relax a
non-binding constraint) and #2 (the loads already ride free), both of which
R17.2 measured correctly.

## 7. Could the compiler reproduce the 17-bundle schedule?

**No, not without changing the loop structure**, and register count is not why.

| hand-written property | compiler | reachable? |
|---|---|---|
| no j-loop (16 columns per body) | J_TILE=8, two j-iterations per row | **measured worse**: J_TILE=16 = 823 ticks vs 795 (R17.0) |
| running pointers `[$r29 + const]`, `$r29 += 128` once per row | recomputes `results + (i*16+j)*8` every iteration | **R14.9 (LICM) and R14.10 (IVSR) both attempted and STOPPED** |
| no accumulator write-back | `loop_reg` writes 9 slots at every j-loop exit | **yes — measured below** |
| accumulators not live during loads | zero-init forces them live | yes, in principle |
| 8 wide B pairs | needs the above | conditional |

## 8. Gap decomposition and a realistic lower bound

| component | compiler | hand-written | gap | status |
|---|---:|---:|---:|---|
| productive load + dot + store bundles | 224 | 204 | **+20** | near-optimal |
| **result-address chain** (`fb_6` b7–b9, 1 instr/bundle) | 96 | 0 | **+96** | R14.9/R14.10 stopped |
| **j-loop control** (`fc_5`) | 112 | 0 | **+112** | J_TILE=16 measured worse |
| **B-row address chain** (`fb_6` b0–b1) | 64 | 0 | **+64** | same class as above |
| **accumulator write-back** (`fe_8`) | 64 | 0 | **+64** | **removable — measured** |
| row prologue (`fb_2`) | 80 | 17 | +63 | A-row load, largely necessary |
| i-loop control (`fc_1`) | 35 | 18 | +17 | small |
| prologue / alignment padding | 24 | 2 | +22 | `mcode_align` |
| **total** | **699** | **241** | **458** | |

**96% of the gap (438 of 458 ticks) is overhead the hand-written kernel does not
execute at all.** Only 20 ticks is its productive work being faster.

**Realistic lower bound under the current 28-register ABI and loop structure:**

| | ticks |
|---|---:|
| today | 699 |
| − dead accumulator write-back (measured) | **651** |
| − wide loads, *if* the full machinery existed (projected, and R17.2 measured the partial form worse) | ~619 |
| − address chains (blocked: R14.9, R14.10) | ~460 |
| − j-loop control (blocked: J_TILE=16 is worse) | ~350 |
| hand-written | **241** |

**Parity with 241 is not reachable without the hand-written kernel's structure**:
no j-loop, running pointers, and an ABI with no FP/SP/GBASE.

## 9. The one justified remaining opportunity

**Dead accumulator write-back elimination.** Measured on the real simulator:

| | ticks | `fe_8` bundles | correctness |
|---|---:|---:|---|
| R17.1 baseline | 699 | 4 | 256/256 |
| **write-back removed** | **651 (−6.9%)** | **1** | **256/256** |

`loop_reg` emits a write-back at every loop exit: 9 address computations and 9
`$st ($i32)` storing `j` and `s0..s7` back to their stack slots, 4 bundles, 16
times. **The accumulator slots are never read again** — the results already went
to `results[]`. DCE keeps them because they are stores to memory and the escape
analysis cannot prove the slots dead after the loop.

It qualifies where the other candidates do not:

* **it removes bundles, not just instructions** — `fe_8` collapses 4 → 1, which
  is why it is worth 48 ticks where the zero-inits (8 instructions) and the wide
  loads (256 executed instructions) were worth **zero and −192**;
* **measured**, not projected — 699 → 651, 256/256 correct;
* **generic** — every `loop_reg`-promoted accumulator in every reduction kernel
  gets one of these blocks; nothing about matmul, tile width or datatype;
* **not previously attempted** — R14.9 and R14.10 attacked the *address chain*,
  not the write-back.

It is not free: it needs a real liveness result — proving a promoted stack slot
is not read on any path after the loop — which `loop_reg` does not currently
compute. That is a genuine dataflow addition, not a peephole.

## Answers

**1. What exact state makes the hand-written kernel tighter?**
Lifetime disjointness, not register count. Its accumulators are not live during
its load phase (its first `$dot` per chain *defines* the accumulator instead of
accumulating into it), which frees 8 registers to hold 8 wide B pairs. It holds
**more** live state than the compiler at peak (22 vs 19), not less.

**2. Why can the compiler not reproduce it?**
Three structural reasons, in order of measured cost: it runs a **j-loop** the
hand-written kernel does not have (112 ticks; J_TILE=16 measured *worse*); it
**recomputes** both addresses every iteration instead of incrementing pointers
(160 ticks, in serial one-instruction bundles — attempted and stopped by R14.9
and R14.10); and it **writes accumulators back** to stack slots at every loop
exit (64 ticks). Its `$dot` width is limited to 4 by the 4-per-bundle memory-lane
cap with narrow loads, not by registers.

**3. How much of the 458-tick gap is actually compiler-removable?**
**48 ticks (10.5% of the gap, 6.9% of runtime) are removable and measured.**
A further ~32 are conditionally reachable via wide loads, but only after the
zero-init change, and R17.2 measured the partial form 192 ticks *worse*. The
remaining ~380 ticks are behind transforms already attempted and correctly
stopped (R14.9, R14.10) or measured counter-productive (J_TILE=16).

**4. Justified next optimization, or freeze?**
**One justified optimization: dead accumulator write-back elimination, −48 ticks
(−6.9%), measured, bundle-reducing, generic, unattempted.** After it, at ~651
ticks, **freeze.** Everything else remaining is either blocked by a transform
already stopped, measured counter-productive, or worth zero because the hot loop
has 41% free slots and instruction removal there buys nothing.

## Status

**R17.3 COMPLETE — analysis only. No production file changed.** Corrects R17.2's
register-count reasoning (peak is 22, not 30; the block is conditional on the
compiler's zero-init lowering, not on the ABI) while leaving R17.2's conclusion
intact. Tags untouched. Nothing pushed.

**Do not start R17.4 automatically.**

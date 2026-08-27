# R17.4 — Dead accumulator write-back: implemented, measured, **NOT SHIPPED**

Implemented on `3a4db8d` (R17.3), measured in full, then **reverted**.
**Production is unchanged at R17.1.** Nothing pushed, no tag touched.

## Answer

> **Can dead accumulator write-back be removed using generic post-loop liveness,
> producing a real bundle/tick reduction while preserving required write-backs?**

**Yes on all three counts — and it still must not ship.**

| primary: fixed-DMEM 16×16 `vu8` JT=8 | R17.1 | R17.4 |
|---|---:|---:|
| **ticks** | **699** | **648** (−7.3%) |
| **static bundles** | 54 | **48** |
| **executed instructions** | 2715 | **2425** |
| ticks / output | 2.730 | **2.531** |
| IPB | 3.884 | 3.742 |
| peak live registers | 25 / 28 | 25 / 28 |
| **spills** | 0 | **0** |
| **correctness** | 256/256 | **256/256** |

`fe_8` collapsed from **4 bundles / 20 instructions / 64 ticks** to **1 bundle /
2 instructions / 16 ticks**, beating R17.3's 651-tick what-if. Required
write-backs were correctly preserved (§ Negative cases). The 38-program suite
passed **38/38 with 3/3 negative controls**.

**It is not shipped because stop condition #4 fired**: two suite programs regress
~5%, and the suite regresses **+0.68% net**.

## Why it was stopped

| | ticks |
|---|---:|
| suite total, R17.1 | 66116 |
| suite total, R17.4 | 66565 (**+0.68%**) |
| regressions | +499 |
| improvements | −50 |
| **`gemm vu32` + `gemm vi32` alone** | **+482** |
| **the other 36 programs together** | **−33** (a net improvement) |

**Two programs account for more than the entire net regression.** 18 programs
improve, 11 regress, all 38 stay correct.

### The mechanism — a bundler ripple, not a bad transformation

`gemm vi32`: 4637 → 4878 ticks. The write-back removal happens in the *outer*
blocks (`fb_6`, `fe_12`), but the cost lands in the innermost loop:

| block | R17.1 | R17.4 |
|---|---|---|
| `fb_10` (inner loop, 256 trips) | **9 bundles**, 48 instrs, 2304 ticks | **10 bundles**, **48 instrs**, 2560 ticks |
| `fb_6` | 6 bundles, 96 ticks | 5 bundles, 80 ticks |
| `fe_12` | 6 instrs, 48 ticks | 4 instrs, 48 ticks |

**`fb_10` has the identical 48 instructions and one more bundle**, occupancy
`[7,4,3,4,6,8,7,6,3]` → `[7,4,4,4,7,7,6,5,2,2]`. Verified in the compiler's own
source mcode, not just the aligned output — so this is the **compiler's
scheduler/bundler**, not `mcode_align` padding.

Removing dead instructions in an outer block changed register allocation, which
changed the hazards in an unrelated inner loop, which packed one bundle worse.
+256 ticks in `fb_10` against −16 elsewhere.

**The transformation is correct and beneficial; the downstream bundler is
sensitive to register-assignment changes it should be indifferent to.** Fixing
that is a bundler milestone, not this one — and the campaign is freezing.

## What was implemented (reverted; recorded so it can be resurrected)

`loop_reg` writes **every** promoted slot back at **every** loop exit. R17.4 added
`_slots_dead_at_exits()` — real backward liveness over the function CFG with the
**stack slots as the variables**:

* `gen`: an `IRLoad` of the slot, at any width;
* `kill`: an `IRStore` **only at the exact promoted width** — a narrower store
  leaves old bits a wider load would still see (the R6.2C/D2 hazard);
* `IRCall` / `IRIndirectCall`: **gen everything**, so a slot that somehow
  outlived the escape analysis is never dropped;
* an exit label not found in the slice: **keep all write-backs**.

It is exact for one reason, and the reason is a precondition `_promote_one`
has **already proved**: every candidate offset is *clean* function-wide — its
address never escapes, and it is touched only through the `IRLoadAddr` →
immediate `IRLoad`/`IRStore` pattern. So the loads and stores it finds are the
complete set of accesses that can exist.

It runs on the **pre-insertion** instruction list, so this promotion's own
preheader load and write-back cannot make its own slot look live. That detail is
load-bearing: the promoted accumulator's preheader load *is* a real read of the
slot, and analysing the post-insertion list would have made every slot look live
and removed nothing.

`writeback_block(skip)` then emits only the live slots. Kill switch
`APARA_NO_DEAD_WRITEBACK`. 103 added lines in one file; no scheduler, bundler,
allocator or codegen change.

## Negative cases — required write-backs were preserved

Four programs, compared with the pass on and off using a **register-invariant**
instruction-shape multiset (removing instructions reshuffles allocation, so a
text diff compares noise — the R17.1 lesson):

| case | what happens after the loop | removed | verdict |
|---|---|---|---|
| `used_after` | `s`, `t` read three times after the loop | **only FP−8** (`i`) | accumulators **kept** ✓ |
| `second_loop` | `s` feeds a second loop | **only FP−8** (`i`) | accumulator **kept** ✓ |
| `returned` | accumulator is the return value | **only FP−8** (`i`) | accumulator **kept** ✓ |
| `dead_acc` | accumulator dead after each iteration | nothing | unchanged ✓ |

In every negative case the **only** write-back removed was the loop counter at
FP−8, which is genuinely dead — the accumulators at FP−16 and FP−24 kept theirs.
All four correct, 0 errors. The removed shapes were exactly
`+ $r ($i64) $r -8` and `$st ($i32) [$r + 0] $r`.

## Change attribution (primary target)

`fe_8` before → after: 4 bundles / 20 instructions → 1 bundle / 2 instructions
(the `i` increment and the branch). `fe_4` also lost the `i`-counter write-back.
**Every other block is tick-identical** — `fb_6` 384, `fc_5` 112, `fb_2` 80,
`fc_1` 35. 699 − 648 = 51 = 48 (`fe_8`) + 3 (`fe_4`).

## Final gap to the hand-written kernel

Production stays at R17.1, so the gap is unchanged:

| | compiler (R17.1) | hand-written | gap |
|---|---:|---:|---:|
| ticks | **699** | **241** | **458** |
| bundles | 54 | 17 | 37 |
| instructions | 2715 | 1160 | 1555 |
| IPB | 3.884 | 4.813 | — |
| peak live | 19 | 22 | — |
| spills | 0 | 0 | — |

Had R17.4 shipped, the gap would have been **648 − 241 = 407 ticks**.

What remains, from R17.3's decomposition: result-address chain (+96) and B-row
address chain (+64), both behind transforms **R14.9 and R14.10 already attempted
and correctly stopped**; j-loop control (+112), where **J_TILE=16 measured worse**
(823 vs 795); accumulator write-back (+64), **this milestone**; row prologue
(+63); i-loop (+17); padding (+22). **96% of the gap is overhead the hand-written
kernel never executes**, and its own advantage rests on an ABI with no FP, SP or
GBASE — 31 registers against the compiler's 28.

## Status

**R17.4 STOPPED under condition #4 — production unchanged at R17.1.** No
`_r17_4_test.py` was created: there is no shipped behaviour to test. The
implementation is recorded above and in this milestone's commit message in
enough detail to rebuild.

**THE COMPILER OPTIMIZATION CAMPAIGN IS FROZEN AT R17.1.**

Final production state: fixed-DMEM 16×16 `vu8` JT=8 = **699 ticks, 54 bundles,
2715 instructions, IPB 3.884, 25/28 peak live, 0 spills, 256/256 correct**;
38/38 suite with 3/3 negative controls; `pipeline_crosscheck` 124/124.

**Do not start R17.5 or R18.**

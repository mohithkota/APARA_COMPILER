# R14.3 — Hoist J-tiled invariant bases out of the inner K loop: **STOPPED**

Branch `feature/r13-matmul-dot`, on top of R14.2 (`dee8954`). **No production
`.py` changed.** Frozen tags untouched, nothing pushed.

## Answer to the final question

> "Does hoisting the shared J-tiled row bases out of the K loop materially
> improve throughput...?"

**The question cannot be answered as posed, because there is nothing to hoist.**
The milestone's premise — "shared B bases are derived every K iteration despite
being invariant with respect to K" — is measurably false.

**There is no inner K loop.** The chunk dimension is *fully unrolled*: the vector
body is straight-line, and `fb_10` contains exactly **one** control transfer (its
own loop-back to the j-loop control). Every base is already materialised
**exactly once** per block entry, and every packed load already reads it as
`[reg + immediate]` off **one shared base register** — which is precisely what
R14.2 delivered.

Stop conditions **4** (existing analysis cannot express the transformation
because the transformation is vacuous) and **5** apply. Per Phase 13 I stopped
rather than start another optimization.

## What the block actually looks like (16×16 vu8, J_TILE=4)

`fb_10`, 24 bundles, splits cleanly in two:

| part | bundles | instrs | 1-instr bundles | **IPB** |
|---|---|---|---|---|
| **b1–b8 — vector work** | 8 | 42 | 1 | **5.25** |
| **b9–b24 — scalar epilogue** | **16** | 21 | **14** | **1.31** |

**The vector work is already good.** 8 loads and 8 `$dot`s in 8 bundles at
IPB 5.25 against a machine width of 8; one bundle holds all four packed loads
off `[$r3 + 0/16/32/48]`, another holds 8 instructions. The base setup (b1–b4)
runs once and feeds all 8 loads.

**Two thirds of the block is the `results[]` stores.** Each output rebuilds its
address from scratch and then serializes:

```
b13  << $r3  ($i64) $r21 3          index * 8
b14  +  $r12 ($i64) $r3  0          copy
b15  +  $r12 ($i64) $r28 $r12       add global base
b16  $st ($i64) [$r12 + 0] $r6      store
```

Confirmed identical in shape on `vi16 16×16` (epilogue 16 bundles, IPB 1.38) and
`vu8 32×32` (16 bundles, IPB 1.38) — this is not a vu8 or a 16×16 artifact.

## Why the epilogue is slow — two compounding causes

1. **The four store addresses are not shared.** They are
   `gbase + results_off + (i*N+j)*8 + t*8`, so they differ by the compile-time
   constants 0/8/16/24 — *exactly* the relation `vector_affine.constant_delta`
   already proves for loads in R14.2. But these stores are the source's
   `results[...] = s_t;` statements, which live **outside** the vector region;
   the vector lowering never sees them, and the scalar optimizer does not
   collapse them.

2. **Register reuse serializes four independent chains.** `$r3` is written at
   b10/b11 and *rewritten* at b13; `$r12` at b14/b15 then rewritten at b17;
   likewise `$r16`, `$r17`. The four chains are mutually independent (different
   output elements) but the allocator gave them overlapping registers, creating
   WAR/WAW that force 16 bundles where ~4–5 would do. **Spills are 0** — this is
   allocation choice under pressure, not spilling.

## Remaining gap to the hand-written reference

Kernel-only 16×16 vu8: **6.00 bundles/output** vs the hand-written **1.207**.

Attribution of the residual, measured rather than guessed:

| cause | share |
|---|---|
| **scalar epilogue stores** (address rebuild + register serialization) | **16 of 24 bundles = 67%** |
| vector work (loads, dots, base setup, accumulator init) | 8 of 24 = 33% |

It is **not** ISA-bound, **not** register-pressure-bound (0 spills), and **not**
addressing-bound in the vector region — R9.3/R14.2 addressing is fully active
there. The hand-written kernel avoids the epilogue entirely by storing straight
to `[$r29 + imm]` with one live base across the row.

## Verification — production unchanged

`git status` clean at `dee8954` before this commit; **0 production `.py` files
modified**. Only `_r14_3_test.py` and this report are added.

`_r14_3_test.py` — **12/12** — pins the finding so it cannot regress silently:
one control transfer, every packed load `[reg+imm]`, all loads off ONE base, and
the vector/epilogue IPB split on three datatype×size combinations.

## Recommendation (NOT implemented, per Phase 13)

The next lever is the **scalar epilogue**, not the vector path:

1. apply the R14.2 constant-delta base sharing to the result stores — the proof
   machinery already exists and is generic; it simply is not reachable from
   outside the vector region;
2. or give the independent store chains disjoint registers so they pack.

Either is a scalar-optimizer change. I have not started one.

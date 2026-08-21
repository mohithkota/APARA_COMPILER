# R14.0 — Output-column (J-dimension) tiling: analysis

**ANALYSIS ONLY. No production change.** `git status` clean at `a264657`;
`0` production `.py` files differ; R13.1's vu8 16×16 result reproduces at
**5887 ticks**. No tiling, scheduler, bundler, datatype, `$dot` or accumulator
change was made.

## Answer

**YES — J-tiling is a large, repeatable win (classification A), but register
pressure caps it at J_TILE = 4 for a compiler implementation.**

Measured on the real simulator, 16×16 vu8, all variants **256/256 correct**:

| J_TILE | aligned bundles | ticks | **ticks/output** | real instrs | IPB | registers |
|---|---|---|---|---|---|---|
| 1 | 101 | 1669 | 6.520 | 1160 | 0.695 | 10 |
| 2 | 45 | 717 | 2.801 | 1160 | 1.618 | 13 |
| **4** | **21** | **309** | **1.207** | **1160** | 3.754 | **19** |
| 8 (4 dots/bundle) | 21 | 309 | 1.207 | 1160 | 3.754 | 31 |
| 8 (8 dots/bundle) | 17 | **241** | **0.941** | 1160 | 4.813 | 31 |

**The instruction count is IDENTICAL (1160) at every J_TILE.** J-tiling changes
only the schedule, so every tick saved comes from removing dependence, not from
doing less work. That is the cleanest possible evidence that the remaining gap
was dependence-bound.

## 1. Current matmul loop structure (confirmed from mcode, not source)

`fb_10` in the R13.1 vu8 16×16 build: **7 bundles, 2 `$dot`, 1 `$st`** — i.e.
**one output element per inner-loop trip**, entered 256 times.

```
for i:  for j:  { for k(chunks): dot into k-accumulators ; fold ; store C[i][j] }
```

R13.1 parallelises the **K/chunk** dimension (the 2 `$dot`s now issue together).
The **j** dimension is still fully serial: one output column at a time.

## 2–3. Why the what-if had to be built at mcode level

Source-level J-tiling was tried first and **cannot be measured through the
existing pipeline**:

- Hoisting `a = A[i*N+k]` into a scalar **kills vectorization entirely**
  (`$dot = 0`, even at J_TILE=1): the A multiplicand becomes an INVARIANT load
  of a scalar slot, so `plan_lowering` extracts only one array and `need = 2`
  is never met.
- Keeping `A[i*N+k]` inline, J_TILE=1 vectorizes (2 `$dot`), but **J_TILE ≥ 2 is
  rejected by legality: `illegal:unproven-aliasing`.** The detector still
  classifies it `matmul`; the memory disambiguator cannot prove the J_TILE
  accumulator slots disjoint from the B-row loads.

So the prototype is a **parameterised hand-written mcode generator** on the real
ISA and real simulator. At J_TILE=8 with 4 dots/bundle it reproduces the
validated reference **exactly (309 ticks, 1160 instrs, IPB 3.754)**, which
validates the generator.

## 4. Where the knee is — and a correction to the reference

- **J_TILE 1 → 4 is a 5.4× kernel speedup** (1669 → 309 ticks).
- **J_TILE 4 → 8 gains NOTHING** as the reference writes it (both 309 ticks):
  with only 4 dots per bundle the extra columns buy no issue slots.
- **J_TILE 8 pays only if the dots issue 8-wide**: 309 → **241 ticks (−22%)**.

The hand-written reference therefore is **not optimal**. Same 1160 instructions,
same data, but packing 8 `$dot`s per bundle instead of 4 gives **0.941
ticks/output vs 1.207**. Worth knowing independently of the compiler work.

## 5. Register pressure — the binding constraint

| J_TILE | distinct registers | fits compiler's 28-register pool? |
|---|---|---|
| 1 | 10 | yes |
| 2 | 13 | yes |
| **4** | **19** | **yes** |
| 8 | **31** | **NO** |

The hand-written kernel reaches J_TILE=8 only because it is standalone and uses
`r26`–`r31` freely. The compiler reserves `r0`, `r26`=FP, `r27`=SP, `r28`=GBASE,
leaving a 28-register pool — so **J_TILE=8 would spill**. No spills were
observed at J_TILE ≤ 4.

Growth is ~3 registers per extra column at 16×16 vu8 (chunks=2): one accumulator
plus `chunks` B-row registers, while the A-row registers are **shared across all
J_TILE columns** — that sharing is the second benefit of tiling.

## 6. Interaction with R13.1 — they are SUBSTITUTES, not complements

R13.1 expands accumulators along **K/chunks**; J-tiling adds independence along
**j**. Both exist to break the *same* accumulator chain.

At J_TILE > 1 there are already J_TILE independent accumulator chains, so
`k > 1` becomes redundant. The requirement is **J_TILE accumulators, not
k × J_TILE** — the two do **not** multiply register pressure, provided a future
implementation picks one dimension rather than stacking both. Stacking them
naively (k=2 × J_TILE=4 = 8 accumulators plus 8 B registers) is what would push
past the pool.

## 7. Matched comparison (kernel-only, like for like)

Whole-program numbers are not comparable — the compiler build has an init loop
and a 256-element result copy; the hand-written one has data preloaded via
`data.map`. Comparing **kernel only**:

| | ticks/output (kernel) |
|---|---|
| R13.1 compiler (7 bundles × 256 trips = 1792 ticks) | **7.000** |
| hand-written J_TILE=1 | 6.520 |
| hand-written J_TILE=4 | **1.207** |
| hand-written J_TILE=8, 8 dots/bundle | **0.941** |

**The compiler today sits essentially at the J_TILE=1 level** (7.00 vs 6.52) —
confirming the whole remaining gap is output-column parallelism, not lowering
quality, addressing, or padding (R9.3 `[reg+imm]` and R9.5 alignment are both
already active; `fb_10` has **0 pad bundles**).

Projected whole-program effect for vu8 16×16 if J_TILE=4 were implemented:
kernel 1792 → 309 ticks, so 5887 → ~4404 ticks = **23.00 → ~17.2
ticks/output (−25%)**. The dilution is the init/copy scaffolding, which is 70%
of that program.

## 8. Classification and blockers

**A — large, repeatable win**, bounded by register pressure.

Three concrete blockers a future implementation must clear:

1. **`illegal:unproven-aliasing`** — the disambiguator must prove J_TILE
   accumulator slots disjoint from the operand loads. This is the *first* thing
   that must be solved; it currently rejects the shape outright.
2. **No J-dimension tiling mechanism exists.** R13.0 Phase 7 was deferred for
   exactly this reason; nothing in the framework tiles the iteration space.
3. **28-register pool caps J_TILE at 4** (19 registers used; J_TILE=8 needs 31).

## 9. Recommendation

Implement generic J-dimension tiling as the next milestone, targeting
**J_TILE = 4** as the default upper bound with the width chosen by the existing
legality/profitability machinery rather than hardcoded — J_TILE must fall out of
register pressure and matrix dimensions, not from a constant. J_TILE=8 should be
considered only if a scheduler change makes 8-wide `$dot` issue reachable AND
the register pool allows it; neither holds today.

**Not started. Awaiting approval.**

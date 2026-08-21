# R16.0 — J_TILE=8 register-pressure decomposition

Compiler at `96f1ed1` (R14.8 active). **ANALYSIS ONLY — 0 production `.py`
changed.** Frozen tags untouched, nothing pushed.

## Premise corrections

Two figures in the brief conflate different artifacts:

1. **"The compiler's J_TILE=8 what-if is much faster than J_TILE=4."** It is
   **slower**. Measured on the matched workload: JT=4 **4575**, JT=8 **7950**,
   JT=16 **11287** ticks — all correct 256/256. The 241-tick figure belongs to
   the **hand-written** 8-dot schedule, not to any compiler output.
2. **"J_TILE=8 requires 31 registers."** 31 is the *distinct-register* count of
   the hand-written schedule. The number that must fit a budget is **peak
   simultaneous liveness**, which is **29**.

## 1. Matched configurations

Same inputs, same 256 outputs, all verified 256/256, 0 errors.

| | ticks | ticks/output | IPB | hot-block regs | spill traffic |
|---|---|---|---|---|---|
| compiler JT=4 | 4575 | 17.87 | 3.667 | 23 distinct | none |
| compiler JT=8 | **7950** | 31.05 | — | **30 distinct** | none *(see §5)* |
| hand 4-dot | 309 | 1.207 | 3.754 | 18 distinct / **17 peak** | none |
| **hand 8-dot** | **241** | **0.941** | **4.813** | 30 distinct / **29 peak** | none |

## 2. Full live-range inventory — the 8-dot schedule

All 31 registers accounted for (30 allocatable + `$r0`):

| class | count | registers | role |
|---|---|---|---|
| **B/C packed operands** | **18** | `$r2`–`$r19` | A-row pair + **8 B-column pairs** (`$u128` loads write register pairs) |
| **A output accumulators** | **8** | `$r20`–`$r27` | one per output column, live 49 instrs each |
| **D/G bases & control** | 4 | `$r28` B base, `$r29` C pointer, `$r30` A offset, `$r31` row counter | |
| hardwired zero | 1 | `$r0` | not allocatable |

**Peak simultaneous live = 29**, at the last B high-half load: 18 operands +
8 accumulators + 3 (`$r28`, `$r29`, `$r31`). `$r30` is already dead there.

## 3. JT=4 → JT=8 differential

| class | JT=4 peak | JT=8 peak | delta |
|---|---|---|---|
| packed operands | 10 | **18** | **+8** |
| accumulators | 4 | **8** | **+4** |
| bases / control | 3 | 3 | 0 |
| **total peak** | **17** | **29** | **+12** |

The +12 is entirely operands (+8) and accumulators (+4) — the address/control
state does **not** grow. R14.2/R14.8 already made addressing width-independent.

## 4. Can the 8-dot schedule fit in 28? **YES — proven**

Peak 29 vs a 28 pool: deficit **exactly 1**.

`$r28` holds the constant B base (256). Every B column offset is 0…240, so the
absolute addresses 256…496 fit the load immediate field. Folding the base into
the immediate removes the register entirely.

**Measured what-if** (`hand8_nobase`): B loads rewritten to `[$r0 + absolute]`,
the `$set $r28` removed:

| | peak live | ticks | correctness |
|---|---|---|---|
| 8-dot, B base in a register | **29** | 241 | 256/256 |
| 8-dot, B via `[$r0 + abs]` | **28** | **241** | **256/256** |

**The 8-dot schedule fits the existing 28-register budget with no cost.**

## 5. But register pressure is NOT what stops the compiler

The compiler's JT=8 shows **no classic spill traffic** yet regresses 74%. The
trace attribution explains it, and it is an accumulator problem, not an address
problem:

| block | ticks | what it is |
|---|---|---|
| `fb_10` | 1152 | hot block — denser per output (2.25 vs 3.00 bundles/output) |
| `fi_3` | 768 | init-loop increment split into its own 3-bundle block |
| **`fe_16`** | **704** | **the result-store epilogue, serialized** |

**`fe_16` is the finding.** R14.8 *did* fire — all 8 stores share `$r12` with
immediates 0/8/16/24/32/40/48/56, so the addressing is correct. What serializes
them is the **values**:

```
[1/8] $ld ($i32) $r4  [$r21 + 0]      <- reload accumulator s1
[1/8] $st ($i64) [$r12 + 8]  $r4
[1/8] $ld ($i32) $r6  [$r22 + 0]      <- reload accumulator s2
[1/8] $st ($i64) [$r12 + 16] $r6
...                                    (14 bundles of this ladder)
```

At JT=8 the hot block already uses **30 registers**, so the eight `int s0..s7`
accumulators cannot stay resident. They live in stack slots and are **reloaded
one at a time**, each load feeding its store through a RAW edge — 8 stores across
~14 bundles instead of the 2 the memory lanes allow. At JT=4 the accumulators
stay in registers and the four stores pack into **one** bundle.

So the pressure is real; it simply manifests as accumulator memory round-trips
rather than as detectable spill code.

## 6. Why the compiler cannot use the §4 escape

The hand-written kernel's arrays sit at **fixed DMEM addresses** (A at 0, B at
256, C at 512), which is what makes `[$r0 + absolute]` legal and lets `$r28` go.

The compiler's `A`, `Bt`, `C` are **stack locals** at FP-relative offsets beyond
the immediate field, so each array base must be materialised into a register.
The compiler therefore cannot reclaim the register that makes 8-dot fit — not
because of allocation quality, but because of **where the data lives**.

## 7. Register floor

| | value |
|---|---|
| 8 accumulators | unavoidable — one per output in flight |
| 18 packed operands | unavoidable *for an 8-wide dot bundle*: 8 columns × 2 halves + A pair |
| C pointer, row counter | unavoidable |
| B base | **removable** (absolute addressing) — the 1 register that closes the gap |
| **floor for an 8-dot schedule** | **28** |

Halving operand pressure (two waves of 4 columns) would free 8 registers, but it
also halves the dots per bundle — that is J_TILE=4, which is what the compiler
already does.

## 8. What the 241-tick schedule actually wins

| | 4-dot (309) | 8-dot (241) |
|---|---|---|
| bundles | 21 | **17** |
| instructions | 1160 | **1160** |
| IPB | 3.754 | **4.813** |

**Identical instruction count.** The 22% win is purely `$dot` packing — 8 per
bundle instead of 4. It is **not** an addressing win, **not** a loop-amortisation
win, and **not** an accumulator-parallelism win (both keep 8 columns in flight).

## 9. Answer

> **"Can the compiler reach the 8-dot / bundle schedule without exceeding the
> existing 28-register budget?"**

**The schedule fits 28 — proven, at 241 ticks and 256/256 correct. The compiler
cannot currently produce it, and the blocker is not allocation quality.**

Two independent obstacles:

1. **Data placement.** The one reclaimable register (the B base) is only
   reclaimable because the hand kernel's arrays are at fixed DMEM addresses. The
   compiler's are stack locals needing register-held bases.
2. **Accumulator residency.** At JT=8 the compiler's hot block needs 30
   registers, so the 8 accumulators round-trip through memory and serialize the
   epilogue (704 ticks) — which alone more than erases the denser hot block.

### Recommended next target — *if* this line is resumed

**Put the matrices at fixed DMEM addresses (globals + `--dmem-init`) instead of
stack locals.** It is the enabling change for both obstacles at once: it removes
the register-held array bases (freeing the register that makes 8-dot fit) and it
removes the init loop, which R15.0 measured at **73% of whole-program ticks**.

That is a **benchmark/methodology change, not a compiler optimization** — the
same conclusion R15.0 reached from a different direction, which is why it is
worth stating twice.

**No production change. R16.1 not started.**

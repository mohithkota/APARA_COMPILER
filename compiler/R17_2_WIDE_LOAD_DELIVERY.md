# R17.2 — `$u128` wide loads: legal now, but **not profitable**. STOPPED.

Compiler at `848653e` (R17.1). **ANALYSIS ONLY — 0 production `.py` changed,
nothing pushed, no tag touched.** Every what-if is a hand edit of emitted mcode,
assembled and executed on the real simulator against the same golden reference.

## Answer

> **Can the existing `$u128` wide-load capability now be used generically in the
> fixed-DMEM matmul path, and does doing so materially improve throughput?**

**Legal: yes — R8.0's blocker really has expired.** Wide loads on fixed-DMEM
globals assemble and execute **correctly, 256/256**, at the alignments R16.2's
global arrays provide. That half of the hypothesis is confirmed by execution,
not by argument.

**Profitable: no — measurably the opposite.**

| primary: fixed-DMEM 16×16 `vu8` JT=8 | R17.1 | wide-load what-if |
|---|---:|---:|
| **ticks** | **699** | **891** (+27.5%) |
| static bundles (aligned) | 54 | **60** |
| **executed instructions** | 2715 | **2459** (−256, −9.4%) |
| **dynamic IPB** | 3.884 | **2.760** |
| **`$dot` per bundle** | 4 | **4 (unchanged)** |
| narrow `$ld ($u64)` | 18 | 2 |
| wide `$ld ($u128)` | 0 | 8 |
| peak live registers | 25 / 28 | 25 / 28 |
| spills | 0 | 0 |
| correctness | 256/256 | **256/256** |

This is stop condition **#6** exactly — *"reduce instructions but do NOT reduce
bundles or ticks."* The what-if executes **256 fewer instructions and takes 192
more ticks.** IPB collapses from 3.884 to 2.760 because the schedule gets
*sparser*, not denser.

**STOPPED at Phase 5. No production code changed.**

## Why — three independent reasons, each measured or proved

### 1. Wide loads relax a constraint that is not binding

The hot block `fb_6` ships **12 bundles / 57 instructions**, occupancy
`[8, 2, 4, 8, 8, 8, 7, 1, 1, 1, 4, 5]`.

| bound on `fb_6` | narrow (today) | with wide loads |
|---|---:|---:|
| issue width (`ISSUE_WIDTH = 8`) | ⌈57/8⌉ = **8** | ⌈49/8⌉ = **7** |
| memory lanes (4 `$ld`/`$st` per bundle) | ⌈24/4⌉ = **6** | ⌈16/4⌉ = **4** |
| **shipped** | **12** | — |

**The schedule is four bundles above its issue-width bound and six above its
memory-lane bound.** It is dependence-bound — three of its twelve bundles still
hold a single instruction each (the serial result-address chain R16.3 found and
R17.1 only partly shortened). Halving the load count moves a bound from 8 to 7
when the code is sitting at 12. There is nothing for it to buy.

This argument is schedule-independent: it does not depend on my hand-written
what-if being a good schedule.

### 2. The compiler's existing schedule already hides its loads for free

Twelve of the sixteen narrow loads are **already co-issued with `$dot`s** in
full 8-slot bundles:

```
||  $ld ×4                                     <- only 4 loads have a bundle to themselves
||  $ld ×4  +  $dot ×4      = 8 slots, full
||  $dot ×4 +  $ld ×4       = 8 slots, full
||  $ld ×4  +  $dot ×4      = 8 slots, full
||  $dot ×4 +  3 bookkeeping
```

Narrow loads cost **nothing** here — they ride in slots the dots cannot use.
Replacing them with wide loads *removes the co-issue opportunity* and forces
dedicated load bundles, which is precisely what the measurement shows.

### 3. The register budget cannot reach 8-wide `$dot` issue

Getting 8 `$dot` into one bundle requires 8 B-row operand pairs resident at once
(each wide load yields the lo and hi half of one row, and the two halves feed the
**same** accumulator, so they cannot share a bundle — a RAW hazard). That needs:

| | registers |
|---|---:|
| 8 accumulators | 8 |
| A row, lo:hi pair | 2 |
| **8 B-row pairs** | **16** |
| B row base, result base, `j`, `i` | 4 |
| **total** | **30** |

The compiler's pool is **28** (`$r1`–`$r25`, `$r29`–`$r31`); `$r0` (zero),
`$r26` (FP), `$r27` (SP) and `$r28` (GBASE) are reserved. The hand-written
kernel's pool is **31** — it reserves only `$r0`, because it has no calling
convention — and it uses **30** of them.

**The compiler is short by exactly two registers, and those two are FP and SP.**
This is an ABI property, not an optimization the compiler declined to do. With
only four even-aligned pairs available it can hold four B rows, which yields
4 `$dot` per bundle — the number it already achieves with narrow loads.

That is the direct answer to Phase 7: **`$dot`/bundle stays at 4, and the load
structure was never what limited it.**

## The existing `$u128` implementation costs 3 instructions, not 1

Independently of the above, a *direct substitution* using the existing facility
would increase instruction count. `codegen._gen_IRLoadWide` borrows an
even-aligned register pair, issues the load, then **copies each half out into an
ordinary register** and releases the pair. Confirmed by building the intrinsic:

```c
__ld128(&lo, &hi, &A[0]);
```
```
||  $ld ($u128) $r4 [$r2 + 0]
||  + $r3 ($i64) $r0 $r4          <- copy out
    + $r6 ($i64) $r0 $r5          <- copy out
```

So one wide load = **3 instructions** where two narrow loads = **2**. Those
copies are emitted *after* register allocation, so no IR-level copy-propagation
or coalescing can remove them. A naive substitution turns 16 load instructions
into 8 loads + 16 copies = **24**.

The facility is what R8.0 called it — *"load mechanics only… no vector op
involved yet"*, built for the `__ld128`/`__ld256` intrinsics. Feeding `$dot`
directly from the borrowed pair, as the hand-written kernel does, would require
the vector lowering to own register-pair lifetimes end to end — a redesign of
vector legality and lowering, which is stop condition **#8**.

## Phase 2/3 — the alignment proof and the pairing opportunity (both hold)

The opportunity itself is real and cleanly structural. From the shipped build:

```
gbase = 0x400,  A = 0x400,  Bt = 0x500,  results = 0x600      (all mod 16 == 0)
$ld ($u64) $rX [$r7 + 0]    $ld ($u64) $rY [$r7 + 8]      <- B row 0, contiguous
$ld ($u64) $rX [$r7 + 16]   $ld ($u64) $rY [$r7 + 24]     <- B row 1
   ... 8 rows, offsets 16t and 16t+8 ...
$ld ($u64) $r30 [$r8 + 0]   $ld ($u64) $r31 [$r8 + 8]     <- A row, contiguous
```

`$r7 = Bt + j*16` with `Bt = 0x500` and `j ∈ {0,8}`, so `$r7 mod 16 == 0`; every
candidate offset `16t` is a multiple of 16. **Nine 16-byte-aligned contiguous
pairs per iteration** (8 B rows + 1 A row), 18 narrow loads → 9 wide.

**R8.0's blocker is genuinely gone.** Its evidence was
`Error: Unaligned address in load nbytes= 16, addr= 32696`, caused by
`SP = 0x7FF8` with `0x7FF8 mod 16 = 8`, making *stack* objects permanently
8-byte aligned. R16.2 moved these arrays to fixed DMEM, and the what-if built
here **executed 8 `$ld ($u128)` per iteration with 256/256 correct results** —
the alignment half of the hypothesis is confirmed positively, by execution.

### Datatype and size coverage (analysis; no production change to test)

| case | row bytes | narrow loads/row | wide loads/row | base alignment | legal? |
|---|---:|---:|---:|---|---|
| `vu8`/`vi8` 16×16 | 16 | 2 | 1 | `0x500 + j*16` | **yes** |
| `vu16`/`vi16` 16×16 | 32 | 4 | 2 | `+ j*32` | **yes** |
| `vu8`/`vi8` 32×32 | 32 | 4 | 2 | `+ j*32` | **yes** |
| `vu16`/`vi16` 32×32 | 64 | 8 | 4 | `+ j*64` | **yes** |

Every supported case is legal. None changes the profitability conclusion: in all
of them the narrow loads are already co-issued with `$dot`s, and none can hold
enough pairs to widen `$dot` issue.

## What the what-if actually did

`fb_6`'s load/dot region rebuilt with four even-aligned pairs
(`$r8:$r9`, `$r10:$r11`, `$r12:$r13`, `$r14:$r15`), two waves of four B rows,
lo-dots and hi-dots in separate bundles (they share an accumulator, so they
cannot be co-issued). Accumulator init, addressing, stores, loop control and
every other block left **byte-identical**.

Result: **891 ticks, 256/256 correct** — 192 ticks worse than R17.1's 699. The
loads no longer co-issue, so the region grew by more bundles than the halved
load count removed.

## Interaction check (Phase 13)

Nothing was disabled, because nothing was changed. The what-if composed with
R16.2 global array bases (the wide loads used the global base register `$r28`
chain unmodified), R16.5's direct accumulators (all 8 stayed resident), R17.1's
identity folding (the `+ 0` residue stayed absent), and R14.2/R14.8 sharing (the
result-store bundles were carried over verbatim and still issue 4-wide).

## Comparison with the hand-written 241-tick schedule

| | R17.1 | wide what-if | hand-written |
|---|---:|---:|---:|
| ticks | **699** | 891 | **241** |
| bundles | 54 | 60 | **17** |
| executed instructions | 2715 | 2459 | **1160** |
| IPB | 3.884 | 2.760 | **4.813** |
| `$dot` / bundle | 4 | 4 | **8** |
| loads (narrow / wide) | 18 / 0 | 2 / 8 | 0 / **17** |
| pool registers available | 28 | 28 | **31** |
| pool registers used | 25 | 25 | **30** |

The hand-written kernel's advantage is **not** that it uses wide loads. It is
that it can hold **8 wide pairs simultaneously**, which needs 30 registers, which
needs an ABI with no frame pointer, no stack pointer and no global base. Wide
loads are a *consequence* of that register headroom, not a substitute for it.

## Conclusion

**Wide loads are legal here and R8.0's blocker has expired — but the
transformation is not profitable, and the reason is structural, not incidental.**

R17.0 ranked `$u128` loads as the largest remaining *structural* lever and
estimated 64–96 ticks, explicitly flagging the figure as **projected, not
measured**. R17.2 measured it: **−192 ticks, i.e. 27.5% slower.** The projection
was wrong because it counted instructions removed without checking whether those
instructions occupied bundles. They did not — twelve of the sixteen ride free in
slots the `$dot`s cannot use.

Stop conditions fired: **#6** (fewer instructions, more bundles and ticks),
**#8** (making it pay needs a vector legality/lowering redesign to own
register-pair lifetimes), **#9** (benefit is negative).

**The remaining gap to 241 is not addressable by load width.** It is the
28-vs-31 register budget and the serial address chains — and the address chains
were already attempted and correctly stopped by R14.9 and R14.10.

## Status

**R17.2 COMPLETE — STOPPED before implementation. Production unchanged.** Tags
`r10-final`, `r11-verified`, `r12.1-verified` untouched. Nothing pushed.

**Do not start R17.3 automatically.**

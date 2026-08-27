# R17.0 — Why 795 and not 241: final hand-written parity analysis

Compiler at `fc0d1bb` (R16.5). **ANALYSIS ONLY — 0 production `.py` changed,
nothing pushed, no tag touched.** Every what-if below is a hand edit of *emitted
mcode*, assembled and executed on the real simulator and checked against the
same golden reference as the baseline.

## Answer

> **Why is the current compiler JT=8 kernel 795 ticks while the matched
> hand-written JT=8 kernel is 241 ticks, despite both being correct, spill-free
> and densely bundled?**

**Because the compiler executes 2.44× the instructions for identical arithmetic,
and almost none of the excess is compute.** Both kernels issue essentially the
same necessary work — the compiler 512 `$dot` + 257 result stores, the
hand-written 544 + 272. Everything else the compiler runs is overhead the
hand-written kernel simply does not have:

| executed instruction role | compiler | hand-written | extra |
|---|---:|---:|---:|
| `$dot` (compute) | 512 | 544 | −32 |
| result stores | 257 | 272 | −15 |
| **loads** | **690** | **289** | **+401** |
| **address / bookkeeping** | **732** | **37** | **+695** |
| **accumulator zero-init** | **274** | **0** | **+274** |
| **accumulator write-back** | **162** | **0** | **+162** |
| control | 134 | 18 | +116 |
| other | 66 | 0 | +66 |
| **total** | **2827** | **1160** | **+1667** |

**73% of the compiler's dynamic instruction count is not arithmetic.** It is not
an IPB problem (3.556 vs 4.813 — and the compiler's `$dot` bundles are *full*),
not a scheduler problem, and not a register problem (25/28, zero spills).

> **What ONE compiler-controlled change has the largest measured potential to
> close that gap?**

**Algebraic identity simplification — fold `x + 0` into `x` at IR level.**
Measured on the real simulator: **795 → 731 ticks (−8.05%), 256/256 correct.**
The compiler has no algebraic simplification at all; `sccp.py` folds only when
**both** operands are constant (`if _is_const(l) and _is_const(r)`), so `x + 0`
with a variable `x` survives every pass to codegen.

## Headline numbers

| | compiler JT=8 | hand-written JT=8 |
|---|---:|---:|
| **ticks** | **795** | **241** |
| ticks / output | 3.105 | 0.941 |
| **static bundles (aligned)** | **57** | **17** |
| executed instructions | 2827 | 1160 |
| **dynamic IPB** | **3.556** | **4.813** |
| **`$dot` per bundle** | **4** | **8** |
| **peak live registers** | **25 / 28** | **31** |
| **spills** | **0** | **0** |
| correctness | 256/256 | 256/256 |
| **largest remaining measured gap** | **address/bookkeeping, 732 executed instrs** | — |

## Premise correction — the 241-tick kernel was not an artifact

The brief cites a 241-tick hand-written reference. **The only hand-written
artifact in the tree measures 309 ticks** (`matmul16_walkthrough/handwritten/
matmul_dot.mcode`, 21 bundles, 4 `$dot` per bundle, verified 256/256 here). The
241 figure comes from R14.0, which *projected* an 8-wide variant.

R17.0 **built** that variant — merging `dot1`∪`dot3` and `dot2`∪`dot4`, which are
mutually independent — and measured it: **241 ticks, 17 bundles, 1160
instructions, IPB 4.813, 256/256 correct.** R14.0's projection is confirmed, and
**8 `$dot` in one bundle is legal on this ISA** (it assembles and executes). Both
references are used below.

## Phase 1 — matched workload

The two harnesses did **not** share inputs: the hand-written `data.map` holds
`A[i]=i+1`, the compiler's C source used `(i*7+3)%17`. R17.0 decoded the
hand-written `data.map` (MSB-first) and generated a compiler program carrying
**those exact 512 byte values**, same 16×16 dimensions, same `vu8`, same J_TILE=8,
same iteration counts, `--dmem-init` for both.

Result: **795 ticks, 256/256 correct** — identical to the unmatched build, which
also proves the tick count is data-independent (no data-dependent control flow).
Static and dynamic joins reconcile at **100% tick coverage** (795/795, 241/241).

## Phase 2 — whole-kernel timeline (sums to the tick)

| compiler block | role | ticks | % | frequency |
|---|---|---:|---:|---|
| `fb_6` | vector body (loads, dots, stores, addressing) | **480** | 60.4% | 32 × 15 bundles |
| `fc_5` | **j-loop control** | **112** | 14.1% | 32 × 3 |
| `fb_2` | row prologue (A row load, promotion) | **80** | 10.1% | 16 × 5 |
| `fe_8` | **accumulator write-back** | **64** | 8.1% | 16 × 4 |
| `fc_1` | i-loop control | 35 | 4.4% | 16 × 3 |
| `main`, `apara_start`, `fe_4`, epilogue, pads | prologue/padding | 24 | 3.0% | once |
| **total** | | **795** | 100% | |

| hand-written block | role | ticks | % |
|---|---|---:|---:|
| `dotA`+`dotB`+`dotC`+`dotD` | 32 `$dot` per row | 68 | 28.2% |
| `load1`–`load4` | 16 `$ld ($u128)` per row | 68 | 28.2% |
| `store1`–`store4` | 16 result stores per row | 68 | 28.2% |
| `row_loop` | A-row load + B base | 17 | 7.1% |
| `outer_branch` | loop control + pointer bump | 18 | 7.5% |
| `initial`, pad | once | 2 | 0.8% |
| **total** | | **241** | 100% |

**The hand-written kernel has no j-loop, no k-loop, no accumulator write-back and
no row prologue.** Its 241 ticks are 84.6% loads, dots and stores.

## Phase 3 — bundle-by-bundle, the compiler's `fb_6` (15 bundles, 60 instrs)

| # | slots | contents | semantic role |
|---|---:|---|---|
| b1 | 8 | 1 `+` + 7 acc zero | address copy + **accumulator init** |
| b2 | 2 | `<<`, 1 acc zero | B-row address, **accumulator init** |
| **b3** | **1** | `+ r10 = r8 + 0` | **identity copy — solo bundle** |
| b4 | 1 | `+ r11 = r6 + r10` | B-row base |
| b5 | 4 | 4 `$ld ($u64)` | B operands |
| b6 | 8 | 4 `$dot` + 4 `$ld` | compute + operands |
| b7 | 8 | 4 `$dot` + 4 `$ld` | compute + operands |
| b8 | 8 | 4 `$dot` + 4 `$ld` | compute + operands |
| b9 | 7 | 4 `$dot`, `+`, `$set`, `+` | compute + result addr + j += 8 |
| **b10** | **1** | `+ r18 = r17 + 0` | **identity copy — solo bundle** |
| b11 | 1 | `<< r9 = r18 << 3` | result address |
| b12 | 1 | `+ r7 = r7 + r9` | result address |
| b13 | 1 | `+ r11 = r28 + r7` | result address (GBASE) |
| b14 | 4 | 4 `$st` | results |
| b15 | 5 | branch + 4 `$st` | results + back edge |

Hand-written, same 8 columns: `load1`(4 ld), `load2`(4 ld), `dotA`(8 dot),
`dotB`(8 dot), `store1`(4 st), `store2`(4 st) — **6 bundles, 32 instructions,
zero address arithmetic, zero accumulator init, zero loop control.**

**8 of the compiler's 15 bundles (b1–b4, b10–b13) are overhead carrying 11
instructions between them** — 8 × 32 = **256 of the 480 ticks in `fb_6`**. Six of
those eight bundles hold a single instruction each: pure dependence latency.

## Phase 5 — why the compiler stops at 4 `$dot` per bundle

**It is not a packing failure and not register pressure.** `ISSUE_WIDTH = 8`
(`bundler.py:611`) and the memory-lane cap is 4 `$ld`/`$st` per bundle
(`bundler.py:751`). The compiler's dot bundles hold **4 `$dot` + 4 `$ld` = 8
instructions — completely full.**

The compiler cannot reach 8 dots per bundle because it has **16 loads** to
co-issue per j-iteration where the hand-written kernel has **8**: the compiler
emits `$ld ($u64)` (8 `vu8` lanes, 8 bytes), the hand-written emits
`$ld ($u128)` (16 lanes, 16 bytes, into a register pair). Same bytes, half the
instructions, so its loads fit in two dedicated bundles and leave the dot bundles
free to run 8 wide.

**`$dot`-per-bundle is a consequence of load width, not an independent lever.**
8 dots in one bundle is legal (proved by building and running `hw8`).

## Phase 6 — load / dot scheduling

| | compiler | hand-written |
|---|---:|---:|
| executed loads | 690 | 289 |
| bytes loaded per j-iteration (8 cols) | 128 (B) | 128 (B) |
| load instructions for those bytes | **16** | **8** |
| load width | `$u64` (8 lanes) | `$u128` (16 lanes) |
| dedicated load bundles / 8 cols | 1 (rest co-issued) | 2 |
| dot bundles / 8 cols | 4 (4-wide) | 2 (8-wide) |

The compiler does **not** reload redundantly, does **not** stall, and does hoist
the A row out of the j-loop (`$r31`/`$r1`, loaded in `fb_2`). Its load *order* is
fine; its load *width* is half. No serialization defect was found.

## Phase 7 — loop control

| | compiler | hand-written |
|---|---:|---:|
| loop nests | 2 (i, j) — k fully unrolled | 1 (i) — j and k fully unrolled |
| inner-loop trips | 32 | 16 |
| loop-control ticks | `fc_5` 112 + `fc_1` 35 = **147** | `outer_branch` 18 = **18** |
| control instructions | 134 | 18 |

**+129 ticks (16.2% of 795).** Tested directly: collapsing the j-loop by writing
J_TILE=16 gives **823 ticks — worse than 795** (256/256 correct, 32 `$dot`). The
tile is already at its measured optimum; this gap is not reachable by tiling.

## Phase 8 — result / store path

R14.8's store bundling still holds: the 8 result stores issue in **2 bundles**
(b14 4-wide, b15 4-wide alongside the branch) — at the 4-per-bundle memory-lane
cap, i.e. **optimal**. The hand-written kernel also uses 2 bundles for 8 stores.
**No gap here.** What differs is the *address* feeding them (Phase 9).

## Phase 9 — address generation

| | compiler | hand-written |
|---|---:|---:|
| address/bookkeeping instructions executed | **732** | **37** |
| address bundles per j-iteration | 6 (b3, b4, b10–b13) | 0 |
| result addressing | recomputed per iteration: `j+0`, `<<4`, `+0`, `+base`, `<<3`, `+512`, `+GBASE` | `[$r29 + const]`, one `$r29 += 128` per row |
| B-row addressing | `j → +0 → <<4 → +0 → +Bt` (5-long serial chain) | `[$r28 + const]` |

The hand-written kernel keeps **running pointers** and uses immediate
displacements. The compiler **recomputes both addresses from scratch every
iteration**, through serial chains that cost one bundle per link.

Two of those links are **identity copies** (`x + 0`) — see Phase 11. The other
four are genuine arithmetic, and strength-reducing them is exactly what
**R14.9 (LICM — premise invalid)** and **R14.10 (IVSR — blocked by `_decompose`)**
attempted and correctly stopped on. That part of the gap is *not* newly available.

## Phase 10 — register lifetimes

| | compiler | hand-written |
|---|---:|---:|
| peak live | 25 / 28 | 31 |
| spills | 0 | 0 |
| accumulators resident | 8 | 8 (+8 in the second half) |
| reserved registers | `$r0`, `$r26` FP, `$r27` SP, `$r28` GBASE | `$r0` only |

**Register capacity does not explain the gap** — the compiler has 3 free
registers. The hand-written kernel gets **31 usable registers against the
compiler's 28** purely by having no calling convention (no FP, no SP, no GBASE),
which is an ABI property, not an optimization. Lifetimes are *shorter* in the
compiler (operands die into the next dot bundle); it is not holding values too
long.

## Phase 11 — redundant work, each item measured

Three candidates were found and each was **measured by hand-editing the emitted
mcode, re-assembling and re-running against the same golden**:

### (1) Identity address copies — `x + 0`

**113 executed**, in every program built. Two of them occupy a **bundle of their
own** inside `fb_6`:

```
||  + $r10 ($i64) $r8  0  ; $null ×7      <- b3,  32 executions
||  + $r18 ($i64) $r17 0  ; $null ×7      <- b10, 32 executions
```

Origin: `vector_lowering.py:469/495` calls `_clone_offset(..., Const(0))`, which
re-emits the loop's own address expression with the IV substituted by `0`. The
`+ 0` residue survives because **the compiler has no algebraic identity
simplification anywhere** — `sccp.py:137` folds only `const ⊕ const`.

**Measured: 795 → 731 ticks (−64, −8.05%), 256/256 correct.**

### (2) Dead accumulator write-back (`fe_8`)

`loop_reg`'s write-back stores `j` and `s0..s7` to their stack slots at every
j-loop exit — 9 address computations + 9 `$st ($i32)`, 4 bundles, 16 times. The
accumulator slots are **never read again** (the results already went to
`results[]`). DCE keeps them because they are stores to memory.

**Measured: 795 → 747 ticks (−48, −6.04%), 256/256 correct** — proving they are
dead.

### (3) Both together

**Measured: 795 → 683 ticks (−112, −14.1%), 256/256 correct.**

### Not redundant

Accumulator zero-init (274 instructions) is *not* removable as such — but the
hand-written kernel avoids it by making the **first** `$dot` of each chain a
plain `$dot` (which writes its destination) instead of `$dot $accumulate`. The
compiler emits `$dot $accumulate` for every chunk and must therefore zero first.
This is real, generic and unattempted, but it occupies bundles b1–b2 that also
carry address work, so its standalone value is smaller than (1) or (2) and it was
not separately measured.

## Phase 12 — theoretical lower bounds

For 256 outputs of a 16×16 `vu8` matmul, with 16 B-rows re-read per output row
(the only schedule that fits 28 registers):

| bound | value | derivation |
|---|---:|---|
| `$dot` operations | 512 | 2 per output (8 lanes × 2 = 16 elements) |
| dot issue bound | **64 bundles** | 512 / 8 slots |
| memory operations | 528 | 256 B loads (`$u128`) + 16 A loads + 256 result stores |
| **memory-lane bound** | **132 bundles** | 528 / 4 lanes — **binding** |
| result-store bound | 64 bundles | 256 stores / 4 |
| control bound | 16 | one back edge per row |

**The floor is ~132 ticks, set by the 4-per-bundle memory-lane cap, not by `$dot`
throughput.** So:

* hand-written **241** is **1.83×** the floor — it leaves ~45% on the table
  (its load and store bundles run 4-wide but never co-issue with its dot bundles);
* compiler **795** is **6.0×** the floor;
* compiler after the two measured what-ifs, **683**, is **5.2×**.

**Parity with 241 is not the ceiling, and 241 is not the floor.** The compiler's
own memory-op count (1109) implies a 278-bundle floor for the code it currently
generates — halving its loads via `$u128` would move that to ~193.

## Phase 13 — complete gap table, ranked by measured ticks

| # | source of gap | compiler | hand-written | extra ticks | extra instrs | responsible component | confidence | removable |
|---|---|---|---|---:|---:|---|---|---|
| 1 | **serial address chains in `fb_6`** | 6 bundles/iter | 0 | **192** | ~200 | vector address lowering + no algebraic simplification | **measured** (64 of it) | **64 measured; 128 blocked by R14.9/R14.10** |
| 2 | **j-loop control** (`fc_5`+`fc_1`) | 147 | 18 | **129** | 116 | loop-control codegen; J_TILE=16 measured *worse* (823) | measured | **low — tiling exhausted** |
| 3 | **row prologue** (`fb_2`) | 80 | 17 | **63** | ~90 | LICM / promotion | measured | unknown |
| 4 | **dead accumulator write-back** (`fe_8`) | 64 | 0 | **64** | 288 | `loop_reg` write-back + DCE not proving stack slots dead | **measured** | **48 (75%)** |
| 5 | **load width** `$u64` vs `$u128` | 690 loads | 289 | ~64–96 (est.) | 401 | vector legality/lowering: no 16-byte alignment rule | projected | see Phase 14 note |
| 6 | **accumulator zero-init** | 274 instrs | 0 | ≤64 (est.) | 274 | vector lowering emits `$dot $accumulate` for chunk 0 | projected | partial |
| 7 | prologue / alignment padding | 24 | 2 | 22 | 66 | `mcode_align` | measured | low |

## Phase 14 — the single best next opportunity

**Algebraic identity simplification: fold `x + 0` (and `x - 0`, `x * 1`,
`x << 0`) into a copy at IR level, then let the existing copy-propagation and
coalescing delete it.**

Why this one:

* **largest measured single saving** — 795 → **731 ticks, −8.05%**, verified
  256/256 on the simulator, not projected;
* **repeated** — 113 executions in this kernel, and identity copies appear in
  **every** program built across the whole datatype × size × tile matrix;
* **structurally generic** — it is a universal algebraic identity, not a matmul,
  tile, datatype or storage-class rule. It is a *missing basic peephole*: the
  compiler has constant folding (`sccp.py`) but no identity simplification at
  all, so `x + 0` reaches codegen as a real instruction in every program;
* **not previously attempted** — R14.9 and R14.10 attacked *strength-reducing*
  the result-address chain and correctly stopped; neither looked at folding
  identities out of it. This removes 2 of the chain's 6 bundles without touching
  what they were blocked on;
* **cheap and contained** — one IR peephole, no scheduler, bundler, allocator or
  vectorizer change, and no new analysis. The two links it deletes are solo
  bundles, so each removal is worth a full bundle of latency.

Close second, and worth doing in the same milestone: **dead accumulator
write-back elimination** (−48 ticks, −6.04%, measured). Together the two are
**795 → 683, −14.1%.**

**Note on `$u128` wide loads — a corrected premise, not this milestone's pick.**
R8.0 stopped wide memory because *"`0x7FF8 mod 16 = 8`, so no stack object can
ever be 16- or 32-byte aligned."* **That fact is now false for the arrays that
matter.** R16.2 made global arrays vectorizable, and this kernel's operands sit
at `gbase = 0x400`, `A = 0x400`, `Bt = 0x500` — **all 16-byte aligned.** R8.0's
own capability table shows IR, codegen, the register allocator (`borrow_pair`,
`_find_aligned_group`), the capability DB and the latency model are **already
ready**; only vector *legality*, *lowering*, *profitability* and the *validation
oracle* are missing. `IRLoadWide` and `IRVecDot128` already exist and codegen
already emits `$ld ($u128)`. This is the largest *structural* lever left and its
blocker has expired — but it needs four new layers, and its value here was
projected, not measured, so it is not the single pick.

## Phase 15 — freeze?

**Do not freeze.** The recommended change is worth a **measured 8.05%** alone and
**14.1%** with its companion, is a handful of lines in one IR peephole, and
requires no redesign. Freezing would bank a missing textbook simplification as a
design decision.

Diminishing returns *have* been reached for two specific things, and both were
settled by measurement here rather than assumption:

* **tiling** — J_TILE=16 is *worse* (823 vs 795); JT=8 is optimal;
* **result-address strength reduction** — R14.9 and R14.10 both stopped, and
  R17.0 found no new route to the four genuine arithmetic links.

**Parity with 241 is not achievable by peephole work.** After the two measured
what-ifs the compiler stands at 683 vs 241 (2.83×), and the residual is
structural: 16-byte-wide loads, running pointers instead of recomputed addresses,
and full j/k unrolling that costs more registers than the 28-register ABI leaves
after FP/SP/GBASE. The hand-written kernel gets 31 registers by having no ABI at
all.

## Reproduction

```bash
export APARA_TOOLS=/home/mohithkota/complier_Apara/engine_new/AjitHpcAccelRepo/\
AjitHpcAccel/engine_isp/assembler/bin      # the README default is broken
# matched compiler build (inputs decoded from the hand-written data.map)
bash apara-cc matched_jt8.c --run --dmem-init          # 795 ticks, 256/256
# hand-written references
mcode_align matmul_dot.mcode | mcode_assemble | mcode_run   # 309 ticks
mcode_align hw8.mcode        | mcode_assemble | mcode_run   # 241 ticks
```

The four analysis scripts used (`analyze.py` static/dynamic join at 100% tick
coverage, `blocks.py` per-label attribution, `roles.py` semantic classification,
`pressure.py` exact IR liveness) are analysis-only and were not committed, per
this milestone's no-`.py`-change constraint.

## Status

**R17.0 COMPLETE — measured diagnosis only. No production file changed.** The
recommended optimization is **not** implemented. **Do not start R18
automatically. Do not push.**

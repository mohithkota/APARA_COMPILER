# R16.3 — Fixed-DMEM J_TILE=4 vs J_TILE=8: final gap analysis

Compiler at `09e67c0` (R16.2). **ANALYSIS ONLY — 0 `.py` changed, nothing
pushed, no tag touched.** Both configurations built from R16.1's own sources
with identical inputs, initialization, `data.map`, simulator and compiler; only
`J_TILE` differs.

## Answer

> **Why is compiler JT=8 still 28 ticks slower than compiler JT=4?**

**Not register pressure.** JT=8 peaks at **23 simultaneously-live registers of
28** in the hot block, with **zero spills**, **zero accumulator reloads**, and
**all 8 accumulators resident** across the entire `$dot` sequence. At the exact
bundle where the schedule is issuing only 3 `$dot`, **8 pool registers are free
and 2 of 8 slots are empty.**

The 28 ticks are the net of a large win and a larger loss:

| | JT=4 | JT=8 | delta |
|---|---:|---:|---:|
| inner body `fb_6` | 640 | 416 | **−224** |
| j-loop control `fc_5` | 176 | 112 | **−64** |
| function entry `main` | 10 | 6 | −4 |
| **result-store block `fe_12`** | **0** (merged) | **224** | **+224** |
| accumulator load block `fb_2` | 64 | 96 | +32 |
| **j-increment block `fi_7`** | **0** (merged) | **32** | **+32** |
| accumulator store block `fe_8` | 48 | 64 | +16 |
| i-increment block `fi_3` | 0 (merged) | 16 | +16 |
| `fc_1`, `apara_start`, `fe_4`, `main_epilogue` | 49 | 49 | 0 |
| **total** | **987** | **1015** | **+28** |

JT=8 halves the iteration count and wins 292 ticks doing it. It then gives back
320 because **its j-body is emitted as three separate basic blocks (`fb_6` +
`fe_12` + `fi_7`, 21 bundles per iteration) where JT=4 emits one (`fb_6`, 10
bundles per iteration)**. The dominant single term is `fe_12`: a **five-deep
serial address chain, one instruction per bundle**, sitting in a block of its own
where there is no other work to overlap it with. In JT=4 the identical
address arithmetic is spread across the `$dot` bundles and costs 2 bundles, not 7.

**Verdict: B — JT=8 is blocked by compiler scheduling/order** (region formation
and operand-rotation width), not by the tile width, the register budget, or the
ISA. Quantified headroom in §9.

---

## 1. Method

Every number below is measured, not modelled.

* `mcode_run -v` emits one `Run_Machine PC=0x..` line per **tick** and one
  `RunLog[0]: 0x.. <mnemonic>` line per **issue slot**. Parsing that gives an
  instruction-exact dynamic trace — no sampling.
* `*.aligned.mcode` (the file the assembler consumes) gives every bundle's pc,
  label, slot count and contents, and names alignment padding `pad_*`.
* Joining the two attributes **every tick** to a named bundle. Coverage is
  **100%**: 987/987 and 1015/1015 ticks matched to a static bundle.
* Liveness is computed at bundle granularity with VLIW semantics (all operands
  read before any result is written), by iterative backward dataflow over the
  CFG reconstructed from labels and `$goto` targets.

**One correction to a standing tool.** The bundler's own `APARA_BUNDLE_STATS`
split-reason histogram is collected **before** the post-bundling optimizer runs,
so it does not describe the code that ships — it reports 49 instructions for
`fe_12` where the emitted block has 14. Split reasons in §6 are therefore
recomputed directly on `*.aligned.mcode` and weighted by measured execution
counts. The pre-optimizer histogram agrees on the *shape* (RAW-dominated) and is
not used for any number here.

## 2. Whole-program metrics

| metric | fixed-DMEM JT=4 | fixed-DMEM JT=8 |
|---|---:|---:|
| total ticks | **987** | **1015** |
| initialization ticks | ~0 (preloaded via `data.map`) | ~0 |
| entry + exit overhead (ticks) | 24 | 20 |
| kernel ticks | 963 | 995 |
| executed instructions | **3531** | **3339** |
| issue slots | 7793 | 8041 |
| dynamic IPB | **3.578** | **3.290** |
| slot occupancy | 45.3% | 41.5% |
| ticks / output (256 outputs) | **3.855** | **3.965** |
| static bundles (aligned) | 50 | 61 |
| static instructions | 104 | 152 |
| distinct bundles executed | 47 | 58 |
| `$dot` static / dynamic | 8 / **512** | 16 / **512** |
| peak simultaneous liveness | **21** / 28 | **23** in `fb_6`, 24 program-wide / 28 |
| spills | **0** | **0** |
| accumulator reloads in the j-body | **0** | **0** |
| ticks in empty (`$null`-only) bundles | 11 (1.1%) | 8 (0.8%) |

**JT=8 executes 192 fewer instructions than JT=4 and still takes 28 more ticks.**
That single line rules out an instruction-count explanation and points at
scheduling.

### Dynamic instruction inventory

| category | JT=4 | JT=8 |
|---|---:|---:|
| address / index ALU | 984 (27.9%) | 760 (22.8%) |
| array loads | 626 (17.7%) | 690 (20.7%) |
| **register copies** (`+ rD = r0 + rS`) | **515 (14.6%)** | **515 (15.4%)** |
| vector `$dot` | 512 (14.5%) | 512 (15.3%) |
| array stores | 355 (10.1%) | 419 (12.5%) |
| **accumulator zero-init** | **274 (7.8%)** | **274 (8.2%)** |
| control (branch) | 195 (5.5%) | 131 (3.9%) |
| address constants (`$set`) | 67 (1.9%) | 35 (1.0%) |
| call/return/halt | 3 | 3 |

Both configurations spend **789 executed instructions — 22–24% of everything
they execute — on accumulator bookkeeping** (copies + zero-init) that computes
no result. This is common to both and is *not* the source of the 28-tick delta,
but it is the largest single block of removable work in either.

### Accumulator memory traffic (the R16.0 question)

| | JT=4 | JT=8 |
|---|---:|---:|
| stack loads of `s0..s7`/`j` per i-iteration (`fb_2`) | 5 → 80 total | 9 → 144 total |
| stack stores of `s0..s7`/`j` per i-iteration (`fe_8`) | 5 → 80 total | 9 → 144 total |
| **FP-relative memory ops inside the j-body** | **0** | **0** |

The +64 loads and +64 stores in JT=8's totals are exactly this once-per-row
traffic on the C-level scalars, **not** spill code and **not** inside the hot
loop. Their cost is the +32 (`fb_2`) and +16 (`fe_8`) ticks in the table above.

## 3. Bundle-by-bundle: the JT=4 j-body (`fb_6`, 10 bundles × 64 = 640 ticks)

| pc | slots | instrs | contents | split reason | class |
|---|---:|---:|---|---|---|
| 0x0090 | 8 | 5 | `+r23=r15+0` · 4× accumulator zero-init | Control | scalar |
| 0x0098 | 8 | 7 | `<<r24=r23<<4` · **4× accumulator copy-in** · `+r23=r22+r15` · `+r15=r15+4` | RAW | scalar |
| 0x00a0 | 8 | 2 | `+r25=r24+0` · `+r24=r23+0` | RAW | address |
| 0x00a8 | 8 | 2 | `+r29=r6+r25` · `<<r25=r24<<3` | RAW | address |
| 0x00b0 | 8 | 4 | 4× `$ld` B columns `[r29+0,16,32,48]` | RAW | vector |
| **0x00b8** | 8 | **8** | **4× `$dot $accumulate`** · 4× `$ld` `[r29+8,24,40,56]` | RAW | **vector** |
| **0x00c0** | 8 | 5 | **4× `$dot $accumulate`** · `$set r31=512` | RAW | **vector** |
| 0x00c8 | 8 | 5 | **4× accumulator copy-out** · `+r31=r31+r25` | RAW | scalar |
| 0x00d0 | 8 | 1 | `+r30=r28+r31` | RAW | address |
| 0x00d8 | 7 | 5 | `$goto` · 4× `$st` results `[r30+0,8,16,24]` | RAW | store |

10 bundles, 4 outputs, **8 `$dot` in 2 bundles (4 per bundle)**. `0x00b8` is at
*both* limits simultaneously — 8/8 slots and 4/4 memory lanes. The store address
arithmetic is distributed into `0x00a8`, `0x00c0`, `0x00c8`, `0x00d0`, i.e. it
rides along inside bundles that exist anyway.

## 4. Bundle-by-bundle: the JT=8 j-body (21 bundles × 32 = 672 ticks)

### `fb_6` — 13 bundles × 32 = 416 ticks

| pc | slots | instrs | contents | split | class |
|---|---:|---:|---|---|---|
| 0x0098 | 8 | 8 | **8× accumulator zero-init** | Control | scalar |
| 0x00a0 | 8 | 8 | `+r7=r14+0` · **7× accumulator copy-in** | RAW | scalar |
| 0x00a8 | 8 | 2 | `<<r8=r7<<4` · **8th accumulator copy-in** | RAW | scalar |
| 0x00b0 | 8 | **1** | `+r9=r8+0` | RAW | address |
| 0x00b8 | 8 | **1** | `+r13=r4+r9` | RAW | address |
| 0x00c0 | 8 | 3 | 3× `$ld` `[r13+0,16,32]` | RAW | vector |
| 0x00c8 | 8 | 6 | **3× `$dot`** · 3× `$ld` `[r13+48,64,80]` | RAW | vector |
| 0x00d0 | 8 | 6 | **3× `$dot`** · 3× `$ld` `[r13+96,112,8]` | RAW | vector |
| 0x00d8 | 8 | 6 | **3× `$dot`** · 3× `$ld` `[r13+24,40,56]` | RAW | vector |
| 0x00e0 | 8 | 7 | **3× `$dot`** · 1 copy-out · 3× `$ld` `[r13+72,88,104]` | RAW | vector |
| 0x00e8 | 8 | 7 | **3× `$dot`** · 3 copy-out · 1× `$ld` `[r13+120]` | RAW | vector |
| 0x00f0 | 8 | 4 | **1× `$dot`** · 3 copy-out | RAW | vector |
| 0x00f8 | 8 | **1** | 1 copy-out | RAW | scalar |

**16 `$dot` spread over 6 bundles — 3 per bundle**, against JT=4's 4 per bundle.

### `fe_12` — 7 bundles × 32 = 224 ticks (the single largest term)

| pc | slots | instrs | contents | split | class |
|---|---:|---:|---|---|---|
| 0x0100 | 8 | 2 | `+r8=r12+r14` · `$set r15=512` | Label | address |
| 0x0108 | 8 | **1** | `+r9=r8+0` | RAW | address |
| 0x0110 | 8 | **1** | `<<r13=r9<<3` | RAW | address |
| 0x0118 | 8 | **1** | `+r15=r15+r13` | RAW | address |
| 0x0120 | 8 | **1** | `+r7=r28+r15` | RAW | address |
| 0x0128 | 8 | 4 | 4× `$st` results `[r7+0,8,16,24]` | RAW | store |
| 0x0130 | 8 | 4 | 4× `$st` results `[r7+32,40,48,56]` | MemLane | store |

**Five consecutive bundles carrying one instruction each**, 5 of 8 slots × 5
bundles = 35 wasted slots per iteration, 160 ticks total, to compute
`&results[i*16+j]`. Each step depends on the previous (RAW), and the block
contains nothing else to interleave. `+r9=r8+0` is a pure register copy — a
whole tick, 32 times, for nothing.

### `fi_7` — 1 bundle × 32 = 32 ticks

`+r14=r14+8` alone in a bundle: the `j += 8` increment as its own basic block.

## 5. Where the 28 ticks are, exactly

| classification | mechanism | ticks |
|---|---|---:|
| **control / region formation** | `fe_12` emitted as a separate block instead of merged into `fb_6` (as at JT=4) | **+224** |
| **control / region formation** | `fi_7` (`j+=8`) emitted as its own block | **+32** |
| **control / region formation** | `fi_3` (`i+=1`) emitted as its own block | **+16** |
| **register lifetime** | 4 extra `s0..s7` stack loads per row (`fb_2`) | +32 |
| **register lifetime** | 4 extra `s0..s7` stack stores per row (`fe_8`) | +16 |
| **scheduling (win)** | half as many j-iterations of the body | −224 |
| **scheduling (win)** | half as many j-loop control evaluations | −64 |
| **other (win)** | 4 fewer entry instructions | −4 |
| | **net** | **+28** |

Of the +320 charged against JT=8, **+272 (85%) is region formation** — the same
work, chopped into blocks that cannot share bundles. Nothing in the delta is
`MemAlias`, `WAW`, `FUnit`, alignment padding, or an ISA limit.

## 6. Split reasons over the shipped code, weighted by execution

| reason | JT=4 boundaries / ticks | JT=8 boundaries / ticks |
|---|---:|---:|
| RAW | 17 / 739 (74.9%) | 26 / 691 (68.1%) |
| Label (block entry) | 14 / 109 (11.0%) | 14 / **154 (15.2%)** |
| Control (branch in predecessor) | 8 / 116 (11.8%) | 8 / 84 (8.3%) |
| MemLane (4 ld/st full) | 1 / 16 (1.6%) | 3 / 64 (6.3%) |
| MemPhase | 0 | 2 / 17 (1.7%) |
| BundleFull (8 slots full) | 1 / 1 (0.1%) | 0 |
| Other | 5 / 5 | 4 / 4 |
| **WAW / WAR / FUnit / MemAlias** | **0** | **0** |

`BundleFull` is essentially never the reason a bundle ends — **the packer is not
running out of slots, it is running out of independent work**, which is the same
conclusion M11 reached for the scalar compiler, still true here.

## 7. Register analysis — JT=8 is spill-free and not register-limited

Requested explicitly, so stated explicitly:

* **Do all 8 accumulators remain resident?** **Yes.** `{r15, r17, r19, r21, r23,
  r25, r30, r1}` are live across every bundle from `0x00c0` to `0x00e8`.
* **Any reloads?** **No.** Zero FP-relative memory operations anywhere in
  `fb_6` or `fe_12`.
* **Any stores of intermediate accumulators?** **No** inside the j-body. The
  `s0..s7` stack stores happen once per *row* in `fe_8`, after all 16 j-tiles.
* **Any eviction?** **No.**
* **Peak simultaneous liveness?** **23** at the `$dot` bundles (`0x00c8`–`0x00e8`),
  24 program-wide in `fe_8`, against a 28-register pool.

At `0x00c8` the live set is `{0,1,2,3,4,6,7,8,9,10,11,12,13,14,15,17,19,21,23,25,26,28,30}`.
Excluding the fixed `r0`/`r26`/`r28`, that is **20 pool registers live, 8 free** —
and the 8 free ones are precisely `{5,16,18,20,22,24,29,31}`, the accumulators'
"variable" copies, which are dead throughout the entire `$dot` region.

**So the schedule issues 3 `$dot` per bundle while holding 8 registers free and
leaving 2 of 8 slots empty in every one of those bundles.** The boundary reason
is RAW on `r7/r8/r9` — a **3-register rotating operand set**. A 4-wide rotation
(what JT=4 uses) was both register-feasible and slot-feasible and was not chosen.
This is an ordering decision, not an allocation limit.

**R16.0's `fe_16` finding does not survive into this configuration.** R16.0
observed 8 accumulators being reloaded one at a time in a load/store ladder —
that was the *stack-local* build, where register-held array bases crowded the
file. With fixed-DMEM addressing the ladder is gone entirely. R16.0's mechanism
was real and R16.2 removed it; what remains is a different, scheduling-level
problem.

## 8. Comparison with the hand-written 8-dot kernel

Using R16.0's published figures for the hand kernel (241 ticks, 1160 executed
instructions, IPB 4.813, 8 `$dot` per bundle, 29 peak live — 28 in R16.0's
absolute-addressing variant, same 241 ticks) against measurements
of the compiler output:

| | hand-written | compiler JT=4 | compiler JT=8 |
|---|---:|---:|---:|
| ticks | **241** | 987 (4.10×) | 1015 (4.21×) |
| executed instructions | **1160** | 3531 (3.04×) | 3339 (2.88×) |
| IPB | **4.813** | 3.578 (0.74×) | 3.290 (0.68×) |
| `$dot` per bundle | **8** | 4 | **3** |
| peak live | 29 (28 with absolute addressing) | 21 | 23 |

**The 4.1× gap decomposes as 3.0× too many instructions × 1.35× worse packing**
(3.04 × 1.345 = 4.09; 2.88 × 1.463 = 4.21 — both reconcile exactly). The
instruction count is the larger factor. Framing this as an IPB problem would be
wrong: the compiler already achieves 68–74% of the hand kernel's packing density
while executing three times the instructions.

Semantic structure, not register names:

| aspect | hand-written | compiler JT=8 |
|---|---|---|
| loop structure | one loop over rows, straight-line body | i-loop × j-loop, body split into **3 basic blocks** |
| operand staging | **loads decoupled from dots** — B columns preloaded into a wide operand block, then pure-`$dot` bundles | **loads interleaved with dots**, 3-register rotation, so `$dot` per bundle ≤ 8 − (loads in that bundle) |
| accumulators | 8 registers, one per output | 8 registers **plus 8 shadow copies**, 16 copy instructions per iteration |
| result stores | address held/stepped, stores issued directly | **5-deep serial address chain** recomputed per iteration in its own block |
| address generation | absolute immediates off a fixed base | absolute immediates off a materialised base (R16.2) — **equivalent** |
| memory traffic | 512 packed B loads + 256 stores | 512 packed B loads + 256 stores — **identical** |
| bookkeeping | none | 789 executed instructions (22–24%) |

The two things the hand kernel does that the compiler does not: it **decouples
loading from dotting** (which is what allows 8 `$dot` in one bundle — with 4
loads in the bundle the ceiling is 4), and it **keeps no shadow copies**.

**Does compiler JT=8 achieve 8 independent `$dot` in one bundle? No — it achieves
3.** What prevents it, in order:
1. It interleaves 3 loads into every `$dot` bundle, capping `$dot` at 8 − 3 = 5
   slots, and it uses only 3 of those.
2. The operand rotation is 3 registers wide, so the next 3 dots must wait (RAW)
   for the reloads issued alongside the current 3.
3. Reaching 8 requires ~16 packed operands live at once (R16.0's 18-register
   operand block). At JT=8 the compiler holds 23 live with 8 free — the 8 free
   registers are exactly the shadow copies. Removing the shadows makes the
   hand-written structure register-feasible; keeping them makes it impossible.

Note on R16.0's accounting: its "17 bundles / 1160 instructions / 241 ticks"
cannot all be same-scope figures (1160/17 = 68 instructions per 8-slot bundle),
so only the internally consistent triple — 1160 executed instructions, 241
ticks, IPB 4.813 — is used above. The "17 bundles" figure is R16.0's static
kernel-loop size and no claim here rests on it.

## 9. Compiler-controlled headroom (maximum possible benefit)

Every figure is derived from the measured bundle inventory above, and is an
upper bound on that item alone.

| # | change | mechanism | ticks saved | JT=8 result |
|---|---|---|---:|---:|
| 1 | 4-wide operand rotation instead of 3 | 16 loads / 4 per bundle = 4 bundles instead of 6 | **−64** | 951 |
| 2 | merge `fe_12` + `fi_7` into `fb_6` | 6 of the 8 bundles are address/increment steps (five of them single-instruction) that would ride inside existing `$dot` bundles, as they do at JT=4; only the 2 store bundles are irreducible | **−192** | 759 |
| 3 | drop the 8 shadow copies (R14.6 pattern, store side) | removes 16 instructions and 2–3 whole bundles per iteration | **−64 … −96** | ~670 |
| — | **combined realistic** | | **−320 … −352** | **~663–695** |
| — | **hard floor, traffic as executed** | 1109 memory ops ÷ 4 per bundle | | **278** |
| — | **hard floor, essential traffic** | 800 memory ops (512 B loads + 256 stores + 32 A loads) ÷ 4 | | **200** |

Items 1–3 are independent and all are scheduling/emission decisions. Any one of
them alone makes JT=8 faster than JT=4's 987; together they would put JT=8 at
roughly **0.67–0.70× of today's best**, i.e. ~2.8× the hand-written kernel
rather than 4.2×.

The same three defects are present at JT=4 (it also carries 4 shadow copies, also
recomputes the store address serially — just cheaply enough to hide), so item 3
in particular is not JT=8-specific: **789 of the 3531 instructions JT=4 executes
are bookkeeping.**

## 10. Decision

**B — JT=8 is blocked by compiler scheduling/order.**

Specifically: (i) region formation leaves the j-body as three basic blocks at
JT=8 and one at JT=4, which alone accounts for +272 of the +320 penalty; and
(ii) the operand rotation is 3 registers wide where 4 was free, capping `$dot`
density below JT=4's.

Explicitly **not**:
* **not A** — no ISA limit is reached; `BundleFull` and `FUnit` are ~0, and both
  configurations sit 3.7–4.0× above their own memory-lane floor;
* **not C** — 23 of 28 live at peak, 8 free registers at the constrained bundle,
  0 spills, 0 reloads, all 8 accumulators resident;
* **not D** alone — address generation is a *symptom* (the 5-deep chain is
  serial because it is alone in a block), though the redundant `+r9=r8+0` copies
  are real and removable;
* **not E** alone — loop control is *cheaper* at JT=8 (−64 ticks), as expected;
* **not F** — JT=4 does remain the right tile *today*, and nothing here changes
  that operationally, but the JT=8 deficit is compiler-controlled, so rejecting
  JT=8 on principle would bank a defect as a design decision.

## 11. Reproduction

```bash
cd <scratch>
cp <r161>/fixed_jt{4,8}.c .
bash apara-cc fixed_jt4.c --run --dmem-init
bash apara-cc fixed_jt8.c --run --dmem-init
# 987 / 1015 ticks, 256/256 PostConditions each
```

The four analysis scripts used here (`trace.py` — log → per-tick bundle profile;
`bundles.py` — static/dynamic join with 100% tick coverage; `liveness.py` —
bundle-granularity VLIW liveness; `splits.py` — split reasons recomputed on the
shipped bundles) are analysis-only and were not committed, per this milestone's
"no `.py` changes" constraint. Their method is specified in §1 in enough detail
to rebuild them; each is under 100 lines.

## 12. Status

**R16.3 COMPLETE — measured diagnosis only. No fix implemented, no scheduler,
bundler, vectorizer, allocator, IVSR or codegen change, no register-budget
change. Do not start R16.4 automatically.**

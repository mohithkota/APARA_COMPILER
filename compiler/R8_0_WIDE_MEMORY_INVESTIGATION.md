# R8.0 — Wide Vector Memory Operations ($u128 / $u256)

## STOPPED AT PHASE 1. The premise does not hold.

**No code was written.** The milestone instructs: *"If investigation shows … register
pressure does not decrease — stop immediately and explain why."* Three independent
measurements say stop, and one of them is a hard illegality.

| # | premise | measured verdict |
|---|---|---|
| 1 | wide loads are legal on our data | **NO** — packed stack arrays are 8-byte aligned; `$u128` needs 16, `$u256` needs 32. The simulator rejects it: `Error: Unaligned address in load nbytes= 16, addr= 32696` |
| 2 | wide loads reduce register pressure | **NO — it more than doubles.** 7 live registers vs 3 for identical work |
| 3 | wide loads relieve the binding constraint | **NO** — every vector region is **width**-bound (27–40% memory ops), not memory-lane-bound |

**I also have to correct myself.** At the end of R7.1 I said wide loads were "the
highest-leverage change… simultaneously raises the ceiling to 8 *and* cuts register
pressure." The ceiling claim survives; **the register-pressure claim was wrong**, and
§5 shows the measurement that disproves it. It was speculation stated as fact, and
this milestone is what caught it.

---

## Phase 1.1 — The ISA

Quoting `compiler_support_documents/APARA_ISA_Reference.tex` and
`APARA_Reference_Manual.pdf`.

**Encoding and destination layout** (ISA Reference §"Memory instructions"):

```
$ld  ($u128) rd  [rs1 + rs2]           ; loads 2 words -> rd, rd+1
$ld  ($u256) rd  [rs1 + rs2]           ; loads 4 words -> rd..rd+3
$st  ($u128) [rs1 + rs2]          rd   ; stores rd, rd+1 (2 words)
$st  ($u256) [rs1 + rs2]          rd   ; stores rd..rd+3 (4 words)
```
Type codes: `$u128` = `01111` (128-bit, 2 regs), `$u256` = `10000` (256-bit, 4 regs).

**Note the addressing mode:** the wide forms take `[rs1 + rs2]` — **register+register
only**. The narrow forms additionally offer `[rs1 + <imm10>]`. A wide access therefore
always needs its offset materialised in a register.

**Do loads fill consecutive registers?** Yes, and the engine confirms it — 
`McodeLoadInstruction::Update_Register_Usage` writes `Get_Rd() + I` for `I` in
`0..nregs-1`, and `McodeAccelerator::Read_Data_Line` fills `ret_array[I]` from
`__data_memory[dword_index + I]`, asserting `n_dwords <= 4`.

**Does the vector ALU consume that layout? Is unpacking required?** This is the one
premise that *does* hold. Reference Manual:

> **"$u128 / $u256 — 128-bit and 256-bit values occupying 2 or 4 consecutive
> registers. Used only by wide load/store to move 2/4 memory words at once.
> **Not an arithmetic type.**"**

So a wide load is *not* a 256-bit vector operand — it is N ordinary 64-bit registers,
each of which `$v` consumes directly. **No unpacking is required**, and no `$slice`/
`$pack` is needed. A 4-word load feeds four independent `$v` operations.

**Alignment.** ISA Reference: *"Addresses are byte addresses; accesses must be
aligned."* and for stores *"Aligned accesses only."* The engine enforces this in
`___execute_load_operation___` / `___execute_store_operation___` via
`AddrIsAligned(base_byte_addr, transfer_size_in_bytes)`.

**A simulator bug that matters for safety** (`McodeUtils.cpp:564`):

```c
case 16: return ((byte_addr & 0xf) == 0); break;
case 32: return ((byte_addr & 0x1) == 0); break;   // <-- should be 0x1f
```

`$u128` (16 bytes) is checked correctly. **`$u256` (32 bytes) is checked against
2-byte alignment**, so the simulator silently accepts misaligned 256-bit accesses that
hardware would reject. Any wide-memory work validated only on this simulator would
appear correct and be broken on hardware. (This is the same defect recorded for the
professor's tree in `ENGINE_CPP_CHANGES_FOR_PROF.md`; it is present in our build too.)

**Architectural restrictions.** No register-number alignment requirement is stated in
any available ISA document, and none is enforced in the assembler or engine. Our
`codegen.RegAlloc.borrow_pair/borrow_quad` docstrings cite *"ISA doc 12.2"* for
even-index / multiple-of-4 register groups, but **that section does not exist in the
`.tex` or `.pdf` sources in this repository** — the only "multiple of 4" text is about
*bundle* addressing. So the compiler is being **stricter than any documented rule**.
That is safe, but it is an unverified constraint that makes wide allocation harder
than it may need to be, and it should be confirmed against the authoritative ISA
document before anyone builds on it.

## Phase 1.2 — Compiler support, by layer

| layer | supports wide memory? | notes |
|---|---|---|
| IR (`ir.py`) | **yes** | `IRLoadWide(dests[], base, offset)`, `IRStoreWide(base, offset, srcs[])`; `len==2` → `$u128`, `len==4` → `$u256` |
| codegen (`codegen.py`) | **yes** | `_gen_IRLoadWide` / `_gen_IRStoreWide`; emits `$ld ($u128)/($u256)` |
| register allocator | **yes** | `has_free_pair/quad`, `borrow_pair/borrow_quad`, `_find_aligned_group` |
| front end (`ir_gen.py`) | **yes, intrinsics only** | `__ld128 / __ld256 / __st128 / __st256`; explicitly documented as "load mechanics only… no vector op involved yet" |
| capability DB | **yes** | `load_wide` / `store_wide`, `widths {128:2, 256:4}`, `group: aligned-register-group`, marked validated |
| latency model | **yes** | `$ld ($u128)` classified `VLOAD`, asserted in `_r6_1_test.py` |
| LICM / copyprop | **yes (analysis)** | recognise and correctly handle both nodes |
| **vector legality** | **NO** | no alignment/contiguity rule for 16/32-byte access; `word_aligned()` proves 8-byte only |
| **vector lowering** | **NO** | every packed access is a single-word `IRLoad`/`IRStore` (`_packed_load`, `packed_load_at`, `slot_load/store`) |
| **vector profitability** | **NO** | no wide realisation to cost |
| **validation oracle** | **partial** | `PackedVectorInterp` models packed single-word gather/scatter; no `IRLoadWide` path |
| **simulator** | **yes, with the `$u256` alignment bug above** | |

**The plumbing is complete from IR down. The gap is exactly one layer: the vectorizer
never constructs an `IRLoadWide`.**

## Phase 1.3 — Existing usage: proven zero

* No vectorizer module constructs `IRLoadWide`/`IRStoreWide`. The only references
  outside `ir.py`/`codegen.py`/`ir_gen.py` are in `licm.py` and `copyprop.py`, which
  *consume* the node types for analysis and never create them.
* Compiling all **38** verification programs and scanning the emitted mcode:
  **0 of 38 contain `$u128` or `$u256`.**
* They are reachable only by writing `__ld128()`/`__ld256()` explicitly in C, which no
  benchmark or corpus program does.

## Phase 1.4 — The alignment blocker (decisive, and experimental)

Rather than reason from the frame layout, I ran it — the R6.2D method. A probe using
the existing `__ld128`/`__ld256` intrinsics on a packed stack array `vi8_t a[64]`
compiles to real wide instructions:

```
$ld ($u128) $r4 [$r6 + 0]
$ld ($u256) $r4 [$r12 + 0]
```

and on the simulator:

```
Error: Unaligned address in load nbytes= 16, addr= 32696
```

`32696 = 0x7FB8`, and `32696 mod 16 = 8`. The cause is structural:
`codegen.startup_code` sets `SP = stack_top = 0x7FF8` and `FP = SP`, and
**`0x7FF8 mod 16 = 8`**, so the frame base is odd-multiple-of-8 aligned. Every stack
slot is `FP + offset` with `offset` a multiple of 8, so **no stack object can ever be
16- or 32-byte aligned** under the current frame layout.

The `$u256` load in the same probe produced **no error** — solely because of the
`case 32: & 0x1` bug. `32696 mod 32 = 24`, so it is misaligned too and would fault on
hardware.

Making this legal requires changing the frame base and rounding frame sizes to 32
bytes — an ABI change that moves every stack address in every program. That is
outside "compiler optimization only", and it would break the byte-identical
`pipeline_crosscheck` guarantee across all 124 corpus programs.

## Phase 1.5 — Register pressure: it rises, and that is fatal

This is the blocker that would matter **even if alignment were solved**.

Measured with the R7.0 allocator prober on two programs that perform identical work —
move four contiguous packed words — one using `$ld ($u256)`, one using four
`$ld ($i64)`:

| form | peak live registers | composition at peak |
|---|---|---|
| **`$ld ($u256)` × 1** | **7 / 28** | 5 scalar + 2 address |
| `$ld ($i64)` × 4 | **3 / 28** | 2 scalar + 1 address |

**A wide load more than doubles peak pressure for the same work**, and the reason is
inherent, not an artifact: a wide load makes all N words live *simultaneously* in an
N-consecutive register group, whereas narrow loads let the allocator load one word,
consume it, and free the register before the next. Wide memory trades *sequential*
register usage for *parallel* register usage.

It also trades a soft constraint for a hard one: those N registers must be
consecutive (and, under the compiler's current rule, N-aligned), so a 28-register pool
becomes fragmented in a way the existing allocator has no mechanism to recover from.

**Why this is decisive:** R7.0 established that register pressure is *the* binding
constraint on this compiler. Pipelined kernels demand **35–37 registers against a pool
of 28**, and R7.1's rematerialization removed the memory spills without changing that
demand. Wide memory would push demand *higher*. It makes the actual bottleneck worse
in order to relieve a different one.

## Phase 1.6 — And the constraint it relieves is not binding

The bundle lower bound is `max(⌈N/8⌉, ⌈M/4⌉)`. Measured on the shipped vector regions:

| kernel (vi8) | N | M | memory % | binding term |
|---|---|---|---|---|
| elementwise | 82 | 33 | 40.2% | **width** ⌈N/8⌉ |
| axpy | 91 | 33 | 36.3% | **width** |
| dot | 72 | 25 | 34.7% | **width** |
| reduction | 52 | 16 | 30.8% | **width** |
| conv3 | 149 | 44 | 29.5% | **width** |
| gemm | 18 | 7 | 38.9% | **width** |

**All six are width-bound, none memory-lane-bound.** Memory lanes have ≥ 2× headroom
everywhere. Collapsing four memory ops into one lowers `M` — a term that is not
setting the bound — while leaving `N` almost unchanged (the four `$v` operations
remain). The bundle lower bound would barely move.

The 8.00 ceiling I computed for wide memory in the previous session assumed the
*essential* instruction mix, where memory *is* the binding term. That analysis was
correct for a hypothetical perfect compiler, but it does not describe the code this
compiler emits today, where address arithmetic and loop scaffolding already dilute the
memory fraction to 27–40%.

## Phases 2–5 — not performed

Phase 2 (design), Phase 3 (implementation), Phase 4 (validation) and Phase 5
(measurement) were not carried out, because Phase 1 disproved the premise. Building a
wide realisation would have required an ABI change to make it legal, and would then
have made the compiler's binding constraint worse. The milestone's stop condition is
explicit and this is it.

No compiler source was modified. `git status` is clean apart from this report.

## Final report

Current IPB is whole-program dynamic IPB from the R7.1 verification run (`vi8`
markers). "Wide-memory IPB" is **not measured** — nothing was implemented; the value
shown is the *bundle-bound* estimate from the shipped instruction mix with memory ops
collapsed 4×, which is what wide memory would actually buy. "Theoretical IPB" is the
essential-mix ceiling with wide memory available and perfect scheduling.

| kernel | current IPB | wide-memory IPB (est.) | theoretical IPB | remaining bottleneck |
|---|---|---|---|---|
| elementwise vi8 | 0.836 | ~0.84 (unchanged) | 8.00 | register pressure (demand 35 vs pool 28); loop-carried chain |
| axpy vi8 | 0.836 | ~0.84 | 8.00 | register pressure (demand 36); SWP admits only 16-bit markers |
| dot vi8 | 0.825 | ~0.83 | 8.00 | fully unrolled — no loop; dependence chain on the accumulator |
| reduction vi8 | 0.698 | ~0.70 | 8.00 | fully unrolled; accumulator chain (R6.6 expanded it, R6.6A limit) |
| conv3 vi8 | 0.814 | ~0.81 | 8.00 | fully unrolled; 6 funnel-shift ops per chunk dominate `N`, not memory |
| gemm vi8 | 1.143 | ~1.15 | 8.00 | scalar-dominated program; vector loop lacks a counted IV |

The wide-memory column is ≈ unchanged because every kernel is width-bound (§1.6):
removing memory instructions does not remove the `$v`, shift, or address instructions
that set `⌈N/8⌉`.

### "Does this move the compiler materially closer to the 6 IPB target?"

**No.**

Measured reasons, not speculation:

1. **It is illegal on our data today.** `Error: Unaligned address in load nbytes= 16,
   addr= 32696`. `FP = 0x7FF8 ≡ 8 (mod 16)`, so no stack object is 16- or 32-byte
   aligned. Legalising it means changing the frame base and frame-size rounding — an
   ABI change, not a compiler optimization, and one that breaks the byte-identical
   124-program crosscheck.
2. **It makes the binding constraint worse.** 7 live registers vs 3 for identical
   work. R7.0 proved register pressure is what blocks every remaining opportunity
   (demand 35–37 vs pool 28); wide memory raises demand and adds a consecutive-group
   allocation constraint on top.
3. **It relieves a constraint that is not binding.** All six vector regions are
   width-bound at 27–40% memory operations. `⌈M/4⌉` is not what sets the bundle count,
   so shrinking `M` does not shrink bundles.
4. **The simulator cannot be trusted to validate it.** `AddrIsAligned` checks `$u256`
   against 2-byte alignment (`& 0x1`), so misaligned 256-bit accesses pass in
   simulation and would fail on hardware. Any measured "success" here would have been
   an artifact.

**What would actually move IPB toward 6**, on the evidence accumulated through R6.5–R7.1:
the gap is not instruction mix, it is **not enough independent work in flight to fill
8 slots**, and the thing preventing more in-flight work is the 28-register file. The
ranked levers are (a) fix the R6.8 pipelined-`axpy` defect R7.1 exposed, so software
pipelining can actually be admitted; (b) reduce the pipelined kernel's register demand
from 35–37 toward 28 (R7.0 ranked live-range splitting next after rematerialization,
for the non-rematerializable rotating-bank values); (c) only then revisit memory
width, once regions are dense enough for `⌈M/4⌉` to become the binding term.

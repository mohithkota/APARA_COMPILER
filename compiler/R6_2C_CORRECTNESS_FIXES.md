# R6.2C — Remaining Correctness Defects (D1 + D2)

**Correctness only.** No performance work. The success criterion was
architecturally correct code verified on the simulator, and every change below is
measured against that. Where a fix moved a bundle count or a realisation, it is
reported as a cost to be explained — not as a win.

**Result: the verification suite goes from 27/38 to 38/38 — RESULT: PASS.** All
three R6.2A defects are now closed (D3 in R6.2B, D1 and D2 here), and two further
defects of the same families were found and fixed along the way.

---

## 1. D1 — root cause

The convolution lowering emitted packed 64-bit loads at byte offsets
`0, 1, 8, 9, 16, 17, …`. Confirmed on the simulator:

```
Error: Unaligned address in load nbytes= 8, addr= 32689     (0x7FB1)
Error: Unaligned address in load nbytes= 8, addr= 32697
...
Error: PostCondition Mem[0x80] = 0x0, expected 0x3
```

`32689 − 1 = 32688 = 0x7FB0` is 8-aligned, so the array base was fine and the
`+1` element shift broke it.

**The compiler emitted an illegal address because the legality layer had no rule
against it.** Both framings in the milestone converge on the same fact: nothing
between recognition and emission ever asked whether an address the packed
lowering would produce is representable. Traced end to end:

| stage | what it did |
|---|---|
| IR | `in[i]`, `in[i+1]`, `in[i+2]` — three contiguous accesses |
| legality | `vector_affine.classify_access` → all three **CONTIGUOUS** → accepted |
| lowering | tap 0 via a constant chunk offset; taps 1,2 via `clone_offset` |
| offsets | `*(_vcb31+0)`, `*(_vcb34+_vgo33)` → byte offsets 0, 1, 2 (+8 per chunk) |
| mcode | `$ld ($i64) $r15 [$r1 + 0]`, `$ld ($i64) $r16 [$r2 + 1]`, … |
| simulator | `AddrIsAligned(addr, 8)` fails → error, then reads the **containing** word |

The last step is why this survived: `McodeExecute.cpp` reports the error and
carries on, reading `base & 0xfffffff8`, so the program produces plausible wrong
numbers instead of stopping. The constraint is structural, not a simulator
nicety — a scalar load performs exactly one `Read_Data_Dword(base & ~7, …)`, so
an 8-byte access spanning two words is not expressible in the datapath.

## 2. D1 — compiler stage responsible

`vector_legality.analyze_legality_loop`. Recognition, affine analysis and
lowering all behaved as designed; the legality layer simply had no ISA alignment
predicate to apply.

## 3. D1 — before / after

The rule is a property of the ACCESS, not of any kernel: the lowering substitutes
the induction variable with a multiple of the packed word, so alignment reduces
entirely to the **invariant** part of the offset.

`vector_affine` already computed that decomposition and discarded everything but
the coefficient. It now also reports:

* `const_off` — the compile-time constant part, in bytes
* `sym_div` — an integer that provably divides every symbolic term (`0` = no
  symbolic part, `1` = present but nothing proven)

and exposes `word_aligned(acc, word=8)`, which is true only when
`const_off % 8 == 0` **and** the symbolic part is provably a multiple of 8.
Unproven means not aligned, because an unproven address is exactly the one that
must not be lowered to a wide access. This was the minimum extension needed: the
rule cannot be stated without the invariant, and no other analysis has it.

`analyze_legality_loop` then rejects any kernel with a contiguous access that is
not provably word-aligned, for every client.

Measured on the predicate itself:

| access | `const_off` | `sym_div` | aligned |
|---|---|---|---|
| `a[i]` | 0 | 0 | **yes** |
| `in[i+1]` (vi8) | 1 | 0 | no |
| `in[i+2]` (vi8) | 2 | 0 | no |
| `in[i+2]` (vi32 → 8 B) | 8 | 0 | **yes** |
| `in[i*32+j]` (2-D row) | 0 | 32 | **yes** |
| `in[i*32+j+1]` | 1 | 32 | no |

So it is not a blanket ban on shifts or on 2-D: a shift that is a whole packed
word stays legal, and a 32-byte row base is proved aligned.

Lowering, before and after, for `out[i]=in[i]+in[i+1]+in[i+2]`:

```
BEFORE   $ld ($i64) $r15 [$r1 + 0]      aligned
         $ld ($i64) $r16 [$r2 + 1]      ILLEGAL
         $ld ($i64) $r17 [$r3 + 8]      aligned
         $ld ($i64) $r18 [$r4 + 9]      ILLEGAL      ... 2 of every 3

AFTER    declined: illegal:unaligned-packed-access:byte-offset-1
         (compiles scalar, and is correct)
```

## 4. D1 — simulator validation

| | before | after |
|---|---|---|
| `conv3` vi8 / vu8 / vi16 / vu16 / vi32 / vu32 | **6 FAIL** | **6 PASS** |
| `Unaligned address in load` errors in conv3 vi8 | 14 | **0** |
| previously-passing programs changed | — | **none** |

**The rule also caught a defect the suite had missed.** A GEMM whose row stride
is not a multiple of the packed word is misaligned for every odd row. Measured
directly on a 17×17 vi8 GEMM:

```
alignment gate DISABLED (pre-R6.2C)   FAIL  -- 1428 PostCondition comparisons,
                                              i.e. ~1428 alignment errors
alignment gate ENABLED                PASS
```

This is why `_r4_4` shape `N=20` and `_r4_4_5` shapes `N=17/20/30` are now
declined. There is a structural consequence worth recording: in an i-k-j GEMM
the inner trip **is** the row stride, and `N*elem_bytes % 8 == 0` forces
`N % lanes == 0` at every width — so **an aligned 2-D GEMM cannot have an inner
remainder**. Those shapes were not merely untested; they are unreachable for
legal code.

---

## 5. D2 — root cause

Packed GEMM produced all-zero output for vi16/vu16/vi32/vu32 while vi8 was
correct. **The element width was a red herring.** Sweeping shapes on the
simulator separated it cleanly:

```
vi8_t   M=4  K=4  N=16   unrolled   PASS        vi16_t M=4 K=4 N=8    unrolled  PASS
vi8_t   M=16 K=16 N=16   unrolled   PASS        vi16_t M=4 K=4 N=16   compact   FAIL
vi32_t  M=4  K=4  N=4    unrolled   PASS        vi32_t M=4 K=4 N=8    compact   FAIL
```

Every **unrolled** GEMM passes; every **compact** GEMM fails. vi8 at N=16 has
only 2 chunks and is realised unrolled, which is the entire reason it looked like
a 16/32-bit bug.

The compact chunk loop deliberately **reuses the scalar loop's own induction
variable slot** — that is what lets a scalar remainder resume over the same slot
with no fix-up. But it accessed that slot 8 bytes wide, while the slot belongs to
an `int` that the scalar code accesses 4 bytes wide. Measured on the slot:

```
FP-1552   IRStore elem_bytes=4    <- scalar `j = 0` init
FP-1552   IRLoad  elem_bytes=8    <- compact loop GUARD
FP-1552   IRLoad  elem_bytes=4    <- cloned address code (clone_offset)
```

A DMEM word is 64 bits and a 4-byte access occupies one half of it, so the
8-byte guard read a word whose high half was the initialised 0 and whose low half
was stale. The guard `iv < 16` therefore failed immediately and **the vector loop
body never executed at all**, leaving C at its initial zeros. The simulator
confirms the mechanism — the corrected code executes ~3× more instructions
because the broken one was skipping the loop entirely:

```
gemm vi16   dynamic non-null instructions   9103 -> 26154
```

GEMM was the client that exposed it because `build_compact` ignores the loop's
own offset temp and re-reads the slot through `clone_offset`; the 1-D clients use
the loop's value temp and never notice the wrong width.

The IR differential oracle could not see any of this: it models memory as a flat
byte dictionary with no word structure, so a 4-byte store followed by an 8-byte
load returns the same value there.

## 6. D2 — compiler stage responsible

`vector_compact_loop.slot_load` / `slot_store`, which hardcoded `elem_bytes=8`
for slots that are shared with scalar code. The same hardcoding appeared in four
further places for the reduction accumulator (`_acc_addr_load`, `_acc_store`, the
`PeelScalar` peel template, and the compact accumulator path).

## 7. D2 — before / after

`slot_width(instrs, lo, hi, slot)` reports the one width the surrounding scalar
code uses for a slot (`None` if unused or inconsistent), and the plan records
`iv_bytes` / `acc_bytes` and threads them through. No GEMM-specific logic: the
fix is in the shared compact-loop builder and the shared accumulator helpers.

```
BEFORE   widths used on the compact-loop IV slot:  {8: 4, 4: 2}
AFTER    widths used on the compact-loop IV slot:  {4: 6}
```

**A latent defect of the same root cause, found by looking for it:** a reduction
with an `int` accumulator (rather than `long long`) was also wrong —
`results[0] = 0x0, expected 0x60` — and now passes.

**And a rollback that was never really a rollback.** R4.1 recorded the
narrow-32-bit-accumulator case as a differential rollback ("the vector form
over-accumulates"). The real cause was this defect: the accumulator was written 8
bytes wide, so it was never truncated at all. With the slot at its true width the
vector form truncates once per chunk, and truncation mod 2³² commutes with the
additions and multiplications being accumulated, so chunk-wise and element-wise
accumulation agree bit-for-bit. Verified on the simulator against gcc with inputs
that genuinely overflow 32 bits (64 products of ~30000×20000, sum ≈ 3.8 × 10¹⁰):
**PASS**. That case now vectorizes correctly instead of rolling back.

## 8. D2 — simulator validation

| | before | after |
|---|---|---|
| GEMM shape sweep (11 shapes, 3 widths, both realisations) | 5 FAIL | **11 PASS** |
| `gemm` vi16 / vu16 / vi32 / vu32 | **4 FAIL** | **4 PASS** |
| reduction with `int` accumulator | FAIL | **PASS** |
| narrow accumulator with real 32-bit overflow | (rolled back) | **PASS, vectorized** |

---

## 9. Regression summary

| check | result |
|---|---|
| **verification harness (simulator, 38 programs)** | **38/38 PASS** (was 27/38) |
| negative controls | all 3 still rejected |
| 124-program corpus, bundled mcode hashes | **0 changed** (byte-identical) |
| `pipeline_crosscheck` | **PASS 124/124** |
| unit suites (all 15 `_r*_test.py`) | **all pass** |
| kernel suite: realisation / bundle changes | **0** — apart from the 8 declines below |
| simulator: ticks changed **without** a correctness change | **NONE** |

Status changes across the whole milestone chain, versus the R6.2A baseline:
`conv3` ×6 and `gemm` ×4 go 0 → 1, plus `gemm vu8` from R6.2B. Nothing regressed.

### The cost of D1, measured and explained

Eight kernels no longer vectorize. This is a real coverage loss and the only
metric movement in the milestone:

| | before (illegal) | after (correct) |
|---|---|---|
| convolution / 2-D kernels vectorized | 8 | **0** |
| their static bundles | 329 | **249** (−24%) |
| `conv3` simulator ticks (6 programs) | 5854 | **9895** (+69%) |

Static size *improves* (the vectorized convolution bodies were large unrolled
windows); the cost is dynamic, about 69% more ticks on those kernels. The
"before" column is code that computed **wrong answers**, so this is not a
slowdown against a working baseline — it is the price of replacing incorrect
fast code with correct slower code, and it sizes what an aligned convolution
lowering could recover.

`APARA_R62C_NO_ALIGN_GATE=1` reproduces the pre-R6.2C behaviour for measurement.
It re-enables generation of illegal addresses and must never be set for code that
will be run.

---

## 10. Remaining known limitations

* **Convolution and 2-D stencils are no longer vectorized** when their taps shift
  by less than a packed word — which is the common case (`in[i+1]` on vi8 is a
  1-byte shift). Recovering them needs a genuinely different lowering: load
  aligned words and combine adjacent ones with shifts. That is new lowering work,
  not a legality tweak, and it was explicitly out of scope here.
* **Aligned 2-D GEMM cannot have an inner remainder**, so the GEMM half of the
  R4.4.5 peeling framework is unreachable for legal code. The framework itself is
  unaffected and still covered by the AXPY case.
* **Alignment must be *proven*.** An invariant with an unknown symbolic term
  (`sym_div == 1`) is rejected even if it happens to be aligned at run time. This
  over-rejects rather than under-rejects, which is the correct direction, but it
  is not exact.
* **`vf32_t` remains untested** — floating-point vector support is still recorded
  as under construction, and no FP kernel is in the suite.
* **D4 is still open** (from R6.2B): casts to packed marker types do not narrow,
  so `(vi8_t)300` does not truncate. Unrelated to alignment or slot widths.
* **`slot_width` returns `None` when a slot's accesses disagree**, in which case
  the previous default is used. No kernel in the corpus hits this, but it is a
  silent fallback and would be better as a decline.
* The alignment argument assumes **frame slots and global objects are 8-byte
  aligned**, which holds for the current frame layout but is not asserted
  anywhere in the compiler.

---

## 11. Reproduction

```sh
cd compiler
python3 -m verification                       # 38/38 on the simulator
python3 loopopt/pipeline_crosscheck.py        # 124/124
for t in _r*_test.py; do python3 $t | tail -1; done
APARA_R62C_NO_ALIGN_GATE=1 python3 -m verification   # reproduces the D1 failures
```

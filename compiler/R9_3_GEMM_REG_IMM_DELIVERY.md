# R9.3 — GEMM invariant row base + `[reg + imm]` addressing

Baseline for every number: **`50e2b67` (R9.2, tag `r9.2-verified`)**.
Kill switch **`APARA_NO_GEMM_REG_IMM=1`** reproduces that baseline
**byte-for-byte** (verified on matmul16's full mcode).

**Headline: suite ticks 131 424 → 108 960 (−17.09%). 6 kernels improved, 0
regressed, 32 unchanged. matmul16 −30.3%.**

---

## 1. Before/after address DAG

`build_unrolled` used to call `clone_offset` **once per chunk**, re-emitting the
whole address computation and leaving the result in a register:

```
BEFORE (per chunk c, 4 chunks)
   _lr(i) ──<<4──▶ t ──+ (c*lanes)──▶ t ──<<1──▶ off_c ─┐
                                                        ├─▶ $ld [Xbase + off_c]
   (identical second chain for Y)                       └─▶ $ld/$st [Ybase + off_c]

   9 address instructions in matmul16's hot block; 4 chain links before each load;
   every offset lives in a REGISTER.

AFTER (once per region, then a constant per chunk)
   _lr(i) ──<<4──▶ t ──<<1──▶ off0 ──+ Xbase──▶ xaddr ─▶ $ld [xaddr + 0/8/16/24]
                              off0 ──+ Ybase──▶ yaddr ─▶ $ld/$st [yaddr + 0/8/16/24]

   2 address instructions + one base add per array; the per-chunk part is a
   COMPILE-TIME CONSTANT.
```

In the shipped output the C-row base is additionally hoisted out of the `k` loop
by the existing LICM (it depends only on `i`), so the hot block sees it as a
live-in register.

## 2. The lowering change

`gemm_lowering.py`
* `_row_base()` — clones the offset expression **once** at chunk 0 and adds it to
  the array base, producing one address temp per array.
* `_build_unrolled_imm()` — emits `[addr + c*lanes*eb]` per chunk, and **emits
  every chunk's loads before any chunk's stores**.
* `build_unrolled()` falls back to the previous per-chunk register-offset form
  whenever the new path declines.

`vector_compact_loop.py`
* `packed_load_at_imm()` / `packed_store_at_imm()` — the same packed access,
  addressed as `base + constant`. No new recognizer; `_vec_pack` is set exactly
  as the register-offset builders do.

Nothing else changed. No new scheduler, no bundler change, no new alias rule, no
change to R6.2, and the `[reg+imm]` code generation is the pre-existing
`codegen._gen_IRLoad` / `_gen_IRStore` path.

### Why the constant delta is sound
`plan_axpy` only accepts **CONTIGUOUS** accesses, which `vector_affine` defines
as `coeff == elem_bytes`. So the offset is exactly `invariant + j*eb`, and
substituting `j = c*lanes` gives `off(c) = off(0) + c*lanes*eb`. The delta is
licensed by the contiguity test that already had to pass.

### Why hoisting the loads is sound
`vector_legality._aliasing_ok` **rejects the kernel** if any carried array memory
edge exists between the store and the read arrays. The chunks are unrolled
iterations of that loop, so "no carried array edge" is exactly "chunk c's store
cannot alias chunk c'>c's load". The packed load already depends on this — one
`$ld` reads `lanes` elements before any lane store happens.

## 3. Immediate-range legality

Offsets used are `c*lanes*eb` for `c < chunks`; `lanes*eb` is one packed 64-bit
word = **8 bytes**, so matmul16 uses `0, 8, 16, 24`. The path is declined unless
`(chunks-1)*lanes*eb` fits `[FP_IMM_LO, FP_IMM_HI]` = `[-512, 511]`, imported
from `rematerialization` so one constant governs every pass that cares — the same
field `codegen._gen_IRLoad` encodes and the same one R9.2 folds branches into.
It also declines when `chunks < 2` and when `clone_offset` fails, falling back to
the previous form.

## 4. Memory-disambiguation effect — the point of the milestone

Store → later-access edges R6.2 keeps in matmul16's hot block:

| | edges | what remains |
|---|---|---|
| R9.2 `[reg+reg]` | **17** | every accumulator pair, plus cross-array |
| `[reg+imm]`, chunk-serial | **6** | cross-array only (C store vs A load) |
| `[reg+imm]`, loads first (**shipped**) | **0** | no store precedes any load |

The four accumulators share a base but held their offsets in four different
registers, which R6.2 cannot compare. As constants off one base it proves them
pairwise disjoint and 11 of the 17 edges disappear.

## 5. Before/after dependence height — and the trap in between

matmul16 hot block `fb_10`, measured with the bundler's own rules:

| config | instrs | bundles | height | height (registers only) | mem edges |
|---|---|---|---|---|---|
| **A** R9.2 production | 37 | **16** | 16 | 11 | 17 |
| **C** `[reg+imm]`, chunk-serial | 29 | **20** | 20 | 8 | 6 |
| **D** `[reg+imm]`, loads first — SHIPPED | 29 | **8** | 8 | 8 | 0 |

`bundles == height` in all three: the bundler was already at the dependence lower
bound and stays there. The addressing change alone drops register-dataflow height
11 → 8 and edges 17 → 6, but **chunk-serial emission made the whole block WORSE
(16 → 20 bundles, matmul16 10 099 → 12 163 ticks)** because the 6 surviving
cross-array edges chain the four chunks end to end. Hoisting the loads removes
them and the block lands exactly on its register floor.

**Disambiguation is necessary but not sufficient — the emission order is what
converts it into bundles.**

## 6. Before/after pad bundles

matmul16 `fb_10`, per iteration, from the aligned image (all of it executes — the
backedge branch is in the last bundle):

| | aligned bundles | real | pad | ×256 iterations |
|---|---|---|---|---|
| R9.2 | 25 | 16 | **9** | 6 400 ticks, 2 304 pad |
| R9.3 | 13 | 8 | **5** | 3 328 ticks, **1 280 pad** |

**Executed pad bundles in the hot loop: 2 304 → 1 280 (−44.4%).** Padding is
still 38% of the hot loop, so the aligner remains unmodelled — but the saving
survived it, unlike R9.2's reduction case.

Whole-image static bundle / pad counts:

| kernel | aligned bundles | pad bundles |
|---|---|---|
| gemm vi8 | 82 → 80 | 27 → 26 |
| gemm vu8 | 83 → 81 | 27 → 26 |
| gemm vi16 | 91 → 80 | 29 → 26 |
| gemm vu16 | 94 → 83 | 31 → 28 |
| gemm vi32 | 113 → 86 | 37 → 28 |
| gemm vu32 | 116 → 89 | 39 → 30 |

## 7. Simulator ticks and 8. IPB

38-program suite:

| metric | R9.2 | R9.3 | delta |
|---|---|---|---|
| **simulator ticks** | 131 424 | **108 960** | **−22 464 (−17.09%)** |
| dynamic instructions | 165 911 | 149 781 | −16 130 (−9.72%) |
| static bundles | 2 058 | 2 004 | −54 (−2.62%) |
| static instructions | 5 007 | 4 867 | −140 (−2.80%) |

Per kernel (every kernel that moved; all six are GEMM):

| kernel | ticks | Δ | Δ% | Δ dyn instr | Δ static bundles | Δ IPB |
|---|---|---|---|---|---|---|
| gemm vi32 | 14 211 → 7 043 | −7 168 | **−50.4%** | −5 552 | −18 | +0.838 |
| gemm vu32 | 14 979 → 7 811 | −7 168 | −47.9% | −5 552 | −18 | +0.712 |
| gemm vi16 | 10 093 → 7 037 | −3 056 | −30.3% | −2 224 | −8 | +0.297 |
| gemm vu16 | 10 861 → 7 805 | −3 056 | −28.1% | −2 224 | −8 | +0.247 |
| gemm vi8 | 8 045 → 7 037 | −1 008 | −12.5% | −289 | −1 | +0.110 |
| gemm vu8 | 8 301 → 7 293 | −1 008 | −12.1% | −289 | −1 | +0.110 |

**matmul16** (standalone, gcc-verified): ticks **10 099 → 7 043 (−30.3%)**,
dynamic instructions 14 258 → 12 034, IR instructions 111 → 92, static mcode
151 → 132, static bundles 66 → 58, IPB 1.412 → 1.709, 4/4 PostConditions.

Unlike `R9_3_LOCAL_GVN_WIP.md` — where −4.63% dynamic instructions bought 0 ticks
— here the instruction reduction is accompanied by a real bundle reduction, which
is why it converts.

## 9. Register pressure and spills

| | R9.2 | R9.3 |
|---|---|---|
| distinct registers in `fb_10` | 26/28 | **25/28** |
| spills (`cg.spilled`) | False | False |
| memory spills (`cg.spilled_to_memory`) | False | False |
| selected tier | IVSR+LICM+loop-reg+superblock | same |

Checked for all six GEMM markers and matmul16 in both arms: **zero spills
everywhere, same tier selected.** Pressure improves because four offset registers
are replaced by one base.

## 10. Regression results

| check | result |
|---|---|
| 38-program simulator suite | **38/38 PASS** |
| negative controls | **3/3 rejected** |
| unit suites | **21/21 PASS** (R3.0–R9.2) |
| `pipeline_crosscheck` | **PASS — 124/124** identical |
| matmul16 vs gcc golden | **4/4 PostConditions** |
| 124-program corpus vs R9.2 | **byte-identical** — 0 programs changed, 0 opcode deltas |
| kill switch `APARA_NO_GEMM_REG_IMM=1` | **byte-identical to R9.2 HEAD** |
| new spills | **none** |

The corpus being byte-identical is also the evidence for requirement 10: no
non-GEMM kernel is touched, because only `gemm_lowering.build_unrolled` calls the
new path.

## 11. Divergence from the hand-written model

| | hand model | production |
|---|---|---|
| hot block bundles | 16 → 11 | 16 → **8** |
| matmul16 ticks | −1 280 projected (−12.7%) | **−3 056 (−30.3%)** |

Production **beat** the model, for two reasons the model did not capture:
1. LICM hoists the C-row base out of the `k` loop entirely, which the
   hand-written block could not show;
2. emitting all loads first removes the cross-array edges the model still
   carried, taking the block to its register floor (height 8) rather than the
   model's 10.

The model was also **wrong in one direction that mattered**: it assumed the
`[reg+imm]` form alone would help. Emitted chunk-serially it is a **17%
regression**. The projected numbers were not used to justify anything — every
figure above is from the production compiler.

### Configurations that were requested but could not be isolated
* **"row-base hoisting only" (B)** is not reachable: materialising the constant
  delta into a register is undone by the existing copy-propagation, which folds
  it straight back into the offset. A patched arm-B compiler produced output
  identical to the full form (7 043 ticks).
* **`APARA_NO_MEMDISAMB=1`** changes neither arm's ticks (10 099 / 7 043 either
  way), so it cannot be used to weigh the disambiguation contribution. The
  edge-count and height table in §4/§5 is the direct evidence instead.

## 12. Success criteria

| # | criterion | result |
|---|---|---|
| 1 | all correctness tests pass | **yes** |
| 2 | no new spills | **yes** — zero in both arms |
| 3 | executed pad bundles decrease materially | **yes** — 2 304 → 1 280 (−44%) |
| 4 | GEMM dynamic bundles decrease | **yes** — 6 400 → 3 328 ticks in the hot loop |
| 5 | simulator ticks improve | **yes** — suite −17.09% |
| 6 | directionally consistent with the what-if | **yes**, and larger |

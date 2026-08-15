# R9.2 — Fold conditional-branch constants into the subtract immediate

**Status: COMPLETE, verified, committed.** Supersedes `R9_2_WIP.md`.

Baseline for every number below: `ac0d159` (R9.1), i.e. the pre-R9.2 codegen
preserved at `wip_r9_2/codegen_PRE_r9_2_baseline.py`.

---

## 1. The change

One `elif` in `codegen._emit_cond_branch`. When the right operand of a
conditional branch is a constant in `[-512, 511]`, it is folded into the
subtract's **immediate field** instead of being materialised in a register:

```
before (3 instructions)                 after (2 instructions)
  + rC   ($i64) $r0  16                   - scr  ($i64) l_reg 16
  - scr  ($i64) l_reg rC                  ? ($i64) scr <  $goto body
  ? ($i64) scr >  $goto body
```

The removed `+ rC, $r0, K` is **loop-INVARIANT yet was recomputed every
iteration**, sitting at the head of the loop header's RAW chain so the subtract
could not share its bundle.

`[-512, 511]` is not a new constant: it is the same immediate window
`_load_const` (codegen.py:498) already uses to decide a value fits an ALU
immediate. `_r9_2_test.py` asserts the two agree, so they cannot drift apart.

**This is the first time the compiler ever emits `<` / `<=`** — 0 occurrences
across the entire corpus before R9.2. Not an ISA gap: `Eval_Branch_Condition`
cases 3 and 5 exist. The old code flipped them to `>` / `>=` by computing
`r - l`, which stranded the constant on the LEFT of the subtract where there is
no immediate field. Folding requires the constant on the right, so the flip
goes away and the native operator is emitted.

The transformation is **unconditional**: it replaces 3 instructions with 2 and
can never add work. It is deliberately NOT gated to the cases that win on ticks
— see §4.

## 2. Verification

| check | result |
|---|---|
| 38-program simulator suite (golden vs gcc) | **38/38 PASS** |
| negative controls | **3/3 rejected** (corrupt value, no golden, over-declared count) |
| unit suites R3.0–R9.2 | **21/21 PASS** |
| `pipeline_crosscheck.py` | **PASS — 124/124** IR, code and selected tier identical; 0 verifier failures, 0 rollbacks |
| 124-program A/B vs pre-R9.2 codegen | **0 programs larger, 0 unexplained differences** |
| new spills | **none** — `$st`/`$ld` counts identical in all 124 programs |
| matmul16 vs gcc | **4/4 PostConditions**, both arms |

Unit suites run: R3.0, R3.1, R3.2, R4.0, R4.1, R4.2, R4.2.5, R4.2.6, R4.2.8,
R4.3, R4.4, R4.4.5, R4.5, R4.6 (incl. R4.6.1 stencil recognition), R6.1, R6.2,
R6.6, R6.8, R7.1, R9.1, R9.2.

`_r9_2_test.py` (new, 105 checks) pins: the `[-512, 511]` window at both
boundaries and both first out-of-range values; agreement with `_load_const`;
`K == 0` keeping its shorter pre-existing path; native `<` / `<=`; **exactly one
instruction saved per branch** measured against the real pre-R9.2 codegen loaded
side by side; byte-identical output for out-of-window constants; and 1092
(left, op, K) combinations executed instruction-by-instruction and compared
against C semantics.

## 3. Measured result

**38-program verification suite, real simulator:**

| metric | pre-R9.2 | R9.2 | delta |
|---|---|---|---|
| simulator ticks | 131 743 | 131 424 | **−319 (−0.242%)** |
| dynamic instructions | 172 579 | 165 911 | **−6 668 (−3.864%)** |
| static bundles | 2 096 | 2 058 | −38 (−1.81%) |
| static instructions | 5 087 | 5 007 | −80 (−1.57%) |
| memory spills | 0 | 0 | unchanged |

Dynamic instruction count falls in **all 38 programs and rises in none.**
Ticks: 4 improved, 4 regressed, 30 unchanged.

**matmul16** (16×16 packed vector matmul, verified against gcc):

| metric | pre-R9.2 | R9.2 | delta |
|---|---|---|---|
| ticks | 10 371 | 10 099 | **−272 (−2.62%)** |
| dynamic instructions | 14 804 | 14 258 | −546 |
| static bundles | 68 | 66 | −2 |
| static instructions | 154 | 151 | −3 |

**124-program corpus** (`testing/`, `new_isa_tests/`, `demo_prof/`,
`isa_coverage_tests/`), static, production pipeline both arms:

| metric | pre-R9.2 | R9.2 | delta |
|---|---|---|---|
| static instructions | 13 616 | 13 564 | −52 |
| static bundles | 7 022 | 6 996 | −26 |
| programs changed | — | 27 | 0 got larger |

The **only opcode whose count changes in any program is `+`** (the removed
constant materialisations). Every other opcode — including `$st` and `$ld` — is
unchanged in all 124 programs, which is what rules out a new spill.

Per-kernel ticks, all 8 that moved:

| kernel | pre-R9.2 | R9.2 | delta |
|---|---|---|---|
| gemm vi16 | 10 365 | 10 093 | −272 (−2.62%) |
| gemm vu16 | 11 133 | 10 861 | −272 (−2.44%) |
| axpy vi16 | 1 238 | 1 237 | −1 |
| axpy vu16 | 1 559 | 1 558 | −1 |
| scalar bubblesort | 19 923 | 19 955 | **+32** |
| reduction vu8 | 1 192 | 1 257 | **+65** |
| reduction vu16 | 1 543 | 1 608 | **+65** |
| reduction vu32 | 1 543 | 1 608 | **+65** |

## 4. The four regressions are an ALIGNMENT artifact — proven, not argued

Every regression executes **strictly fewer instructions** and takes **more
ticks**, by exactly the same amount: reduction vu8/vu16/vu32 −65 instructions
/ +65 ticks; bubblesort −32 / +32. That 1:1 correspondence is the fingerprint,
and the aligned images show the mechanism directly.

`mcode_align` inserts `pad_*` bundles of `$null` so a size-N bundle starts at a
PC that is a multiple of N. In the regressing kernels the folded constant was
sharing a **2-slot bundle**, so removing it does not remove a bundle — it makes
that bundle 1-slot, and the aligner then inserts one extra pad bundle to restore
the alignment of everything downstream.

`scalar bubblesort`, block `fc_5`, both arms (nulls elided):

```
  pre-R9.2                                    R9.2
  fc_5: // pc=0xb0                            fc_5: // pc=0xb0
    || + $r23 ($i64) $r26 -264                  || + $r23 ($i64) $r26 -264 ;
       + $r25 ($i64) $r0  31 ;                pad_1__34: // pc=0xb1   <-- NEW
  pad_2__34: // pc=0xb2                       pad_2__36: // pc=0xb2
  pad_4__36: // pc=0xb4                       pad_4__38: // pc=0xb4
    || $ld ($i32) $r24 [$r23 + 0] ;             || $ld ($i32) $r24 [$r23 + 0] ;
    || - $r29 ($i64) $r25 $r24 ;                || - $r25 ($i64) $r24 31 ;
    || ? ($i64) $r29 > $goto fb_6 ;             || ? ($i64) $r25 < $goto fb_6 ;
```

One instruction out, one pad bundle in, **every downstream block lands at
exactly the same PC in both arms** (`fb_6`, `fe_8`, `main_epilogue` are
identical). The block runs 32 times → −32 instructions, +32 ticks.
`reduction vu8` is the same shape in its own `fc_5` (constant 64, block runs 65
times → −65 / +65).

Statically for `reduction vu8`: real bundles **41 in both arms**, pad bundles
23 → 24, total 64 → 65.

**This corrects `R9_2_WIP.md` §4**, which described the effect as saved bundles
being "converted one-for-one into padding" on the strength of frequency-weighted
estimates. The estimates pointed the right way but the mechanism is sharper and
is now shown from the aligned images and the simulator alone: **in the
regressing cases no real bundle is saved at all** — the instruction leaves a
multi-slot bundle and the aligner answers with one extra pad bundle. The WIP's
frequency-weighted totals (which ran ~2.7× above measured ticks because
`label_frequencies` is not an exact dynamic count) are not needed for the
argument and are not repeated here.

Where the fold *does* collapse whole bundles the saving survives: matmul16 and
gemm vi16/vu16 lose 2 static bundles each and win 2.6%.

**No semantic regression is involved**: all four regressing programs PASS
against their gcc golden reference, in both arms, with the full declared
PostCondition count.

Gating the fold to the winning cases would be fitting to alignment noise — the
same mistake R8.1 made — and was rejected.

## 5. Correctness questions asked and closed

* **Overflow.** `l - K` can overflow for extreme `l`. The pre-existing general
  path computes the same subtract on the same pair (`l - r` for `>`,`>=`,`==`,
  `!=`; `r - l` for `<`,`<=`). Exposure is **unchanged**, not introduced.
* **Signedness.** The branch is tested `($i64)` unconditionally — pre-existing
  throughout `_emit_cond_branch`, including for unsigned source comparisons.
  Not introduced by R9.2.
* **Immediate width.** `[-512, 511]` is the same 10-bit signed field R7.1 ships
  against (`rematerialization.FP_IMM_LO/HI`) and `_load_const` uses; the
  assembler rejects out-of-range, and `_r9_2_test.py` pins both boundaries.
* **`K == 0`.** Still takes the pre-existing ONE-instruction path; R9.2 must not
  and does not lengthen it.

## 6. Known gap

R9.2 has **no kill switch** (R9.1 has `APARA_NO_AVN`). The A/B mechanism used
throughout this report is the restore point pair in `wip_r9_2/`, which
`_r9_2_test.py` loads directly, so the before/after comparison stays
reproducible without one.

`wip_r9_2/` is a local working artifact and is **not committed** (it is a 70 KB
copy of `codegen.py`, which would rot). `_r9_2_test.py` §[4] — the A/B that
proves exactly one instruction is saved per branch — therefore prints
`SKIP -- baseline not present` if that directory is removed. The other 5
sections, including the 1092-case semantic check, are self-contained. To restore
the A/B on a fresh tree: `git show ac0d159:compiler/codegen.py >
compiler/wip_r9_2/codegen_PRE_r9_2_baseline.py`.

## 7. Not claimed here

`mcode_align` padding is **unmodelled by every pass in the compiler** — the
scheduler and bundler optimise SOURCE bundles, and §4 shows that is the wrong
objective when the aligner can answer a saved instruction with an extra bundle.
Statically there are 52–61 pure `pad_*` bundles per program (27–46% of all
bundles), but **reachability is not established** (a pad after an unconditional
branch never executes), so this is NOT a claimed bottleneck. Measuring that
reachability is the first step if the thread is picked up. Do not confuse it
with R6.1's 54.2% empty issue slots, which is INTRA-bundle emptiness and is
already accounted for.

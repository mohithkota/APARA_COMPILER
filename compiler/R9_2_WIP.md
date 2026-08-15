# R9.2 — Fold loop-bound constants into the branch subtract (WORK IN PROGRESS)

**State: implemented, correctness-validated, NOT committed, NOT fully regression-tested.**

Paused deliberately. Everything needed to resume is in this file and `wip_r9_2/`.

---

## 1. What the change is

One `elif` in `codegen._emit_cond_branch` (codegen.py:621). When the right operand
is a constant in `[-512, 511]`, fold it into the subtract's **immediate field**
instead of materialising it in a register:

```
before (3 instructions, 3 bundles)      after (2 instructions, 2 bundles)
  + rC   ($i64) $r0  16                   - scr  ($i64) l_reg 16
  - scr  ($i64) rC   l_reg                ? ($i64) scr <  $goto body
  ? ($i64) scr >  $goto body
```

The old path called `_operand_reg(Const)`, which emits `+ rC, $r0, K` — an
instruction that is **loop-invariant yet recomputed every iteration**, sitting at
the head of the header's RAW chain so the subtract cannot share its bundle.

Side effect: this is the first time the compiler ever emits `<` / `<=`. There
were **0 occurrences across the entire corpus** before, not because the ISA lacks
them (`Eval_Branch_Condition` cases 3 and 5) but because the old code flipped them
to `>` / `>=` by computing `r - l`, which stranded the constant on the LEFT of the
subtract where no immediate field exists.

## 2. Validation status

| check | result |
|---|---|
| 38-program verification suite | **38/38 PASS**, 3/3 negative controls rejected |
| `_r9_1_test.py` | PASS |
| `_r7_1_test.py` | PASS |
| `_r6_6_test.py` | PASS |
| `_r6_8_test.py` | PASS |
| `_r6_2_test.py` | PASS |
| `_r4_*`, `_r3_*` unit suites | **NOT RUN** |
| 124-program crosscheck | **NOT RUN** |

## 3. Performance: net win, unevenly distributed

Suite **131743 -> 131424 ticks (-0.24%)**. matmul16 **10371 -> 10099 (-2.6%)**,
4/4 PostConditions.

4 wins / 4 regressions:

| | |
|---|---|
| gemm vi16 | -2.6% |
| gemm vu16 | -2.4% |
| axpy vi16 / vu16 | -0.1% each |
| reduction vu8 | **+5.5%** |
| reduction vu16 | **+4.2%** |
| reduction vu32 | **+4.2%** |
| scalar bubblesort | +0.2% |

## 4. The regression is EXPLAINED — alignment padding, not a defect in the fold

`reduction vu8` executes **65 FEWER instructions** (2089 -> 2024) and 65 fewer
source bundles, yet takes **65 MORE ticks** (1192 -> 1257). That contradiction is
resolved.

`mcode_align` inserts explicit `pad_N__M` bundles of `$null` so that a size-N
bundle starts at a PC that is a multiple of N. Removing an instruction shifts
every downstream PC and the aligner compensates. Frequency-weighted:

| kernel | | real bundles | pad bundles | total |
|---|---|---|---|---|
| reduction vu8 | R9.1 | 2307 | 927 | **3234** |
| | R9.2 | 2242 (**-65**) | 992 (**+65**) | **3234** |
| gemm vi16 | R9.1 | 21663 | 6367 | 28030 |
| | R9.2 | 20877 (-786) | 6127 (-240) | **27004 (-1026)** |

**The 65 saved bundles are converted one-for-one into padding.** On gemm the
saving (-786) is large enough to survive the padding perturbation, which is why
gemm wins and reduction loses. That is the entire 4/4 split.

> **Caveat, do not lose this:** these frequency-weighted totals run ~2.7x above
> measured ticks (3234 vs 1192 for reduction vu8), so the ABSOLUTE magnitudes are
> inflated — `label_frequencies` is not an exact dynamic count. Only the
> arm-to-arm DELTAS, which share the same weighting on both sides, are
> trustworthy. The argument rests only on the deltas.

Reproduce with `/tmp/padcount.py` (regenerate if cleaned — it aligns
`production_codegen` output and weights pad bundles by `label_frequencies`).

## 5. Correctness questions asked and closed

* **Overflow.** `l - K` can overflow for extreme `l`. But the pre-existing general
  path computes the same subtract on the same pair (`l - r` for `>`,`>=`,`==`,`!=`;
  `r - l` for `<`,`<=`). Exposure is **unchanged**, not introduced.
* **Signedness.** The branch is tested `($i64)` unconditionally — that is
  pre-existing throughout `_emit_cond_branch`, including for unsigned source
  comparisons. Not introduced by R9.2.
* **Immediate width.** `[-512, 511]` is the same 10-bit signed field that R7.1
  already ships against (`rematerialization.FP_IMM_LO/HI`). Consistent by
  construction, and the assembler would reject out-of-range.

## 6. Recommendation

**Commit it**, after finishing section 2's two gaps. It replaces 3 instructions
with 2 and can never add work; the suite is a net win; the regressions are an
artifact of a downstream aligner that no pass in the compiler models.

The alternative — gating the fold to the cases where it happens to win — is
fitting to alignment noise and is the same mistake R8.1 made. Do not do it.

## 7. THE BIGGER FINDING — alignment padding is unmodelled

Static, per program: **52-61 pure `pad_*` bundles**, which is **27-46% of all
bundles** in the aligned image.

| kernel | bundles | pad bundles |
|---|---|---|
| reduction vu8 | 114 | 52 |
| dot vi8 | 177 | 54 |
| axpy vi16 | 154 | 61 |
| gemm vi16 | 193 | 52 |
| elementwise vi8 | 179 | 55 |

**NOT yet a claimed bottleneck.** A pad placed after an unconditional branch is
never executed, and I have **not** established what fraction is reachable. That
is the first thing to measure if this thread is picked up.

If a meaningful fraction IS reachable, it outweighs everything R9.x has been
chasing, and **no pass in the compiler currently models bundle alignment at all** —
the scheduler and bundler optimise source bundles, which section 4 proves is the
wrong objective when the aligner can convert a saving straight back into padding.

(Do not confuse this with R6.1's 54.2% empty issue slots. That is *intra*-bundle
emptiness and is already accounted for. These are whole *extra bundles* inserted
between blocks. The 77-84% `$null` slot figure in the aligned image mixes the two
and should not be quoted.)

## 8. How to resume

Restore points (durable — `/tmp` copies will be cleaned):

```
wip_r9_2/codegen_PRE_r9_2_baseline.py   # revert target
wip_r9_2/codegen_WITH_r9_2.py           # current tree state
wip_r9_2/r9_2.patch                     # the diff alone
```

To finish and ship:
1. `for t in _r4_*_test.py _r3_*_test.py; do python3 $t; done`
2. run the 124-program crosscheck
3. commit

To abandon: `cp wip_r9_2/codegen_PRE_r9_2_baseline.py codegen.py`

To investigate the padding instead: start at section 7, first question is
**reachability** of `pad_*` bundles.

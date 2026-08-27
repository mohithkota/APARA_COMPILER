# R17.1 — Generic additive-identity folding (`x + 0 → x`)

Implemented on `4b0a06e` (R17.0), following `R17_0_FINAL_HANDWRITTEN_PARITY_ANALYSIS.md`.
**One production file changed** — `compiler/strength_reduce.py`, +32/−1 lines.
No scheduler, bundler, register-allocator, vectorizer, IVSR, LICM, codegen or
loop-transformation change. No register-budget or spill-policy change.

## Answer

> **Can generic IR folding of `x + 0` / `0 + x` remove measurable compiler
> overhead without regressions?**

**Yes**, and by more than R17.0 projected.

| primary target: fixed-DMEM 16×16 `vu8` JT=8 | R16.5 | R17.1 |
|---|---:|---:|
| **ticks** | 795 | **699** (−12.1%) |
| **static bundles (aligned)** | 57 | **54** |
| **executed instructions** | 2827 | **2715** |
| ticks / output | 3.105 | **2.730** |
| **dynamic IPB** | 3.556 | **3.884** |
| `$dot` per bundle | 4 | 4 |
| peak live registers | 25 / 28 | **25 / 28** |
| **spills** | 0 | **0** |
| **correctness** | 256/256 | **256/256** |

R17.0's hand-edited what-if predicted 731 ticks (−8.05%). Production reaches
**699 (−12.1%)** because the what-if removed only the two identity copies that
sat in a bundle of their own, while the pass removes **four** — the other two
freed slots that let the bundler collapse a third bundle.

## The rule

```
x + 0  ->  x
0 + x  ->  x        integer only; either operand order
```

Nothing else. `x - 0`, `x * 1`, `x | 0`, `x ^ 0`, `x << 0` are recognised as
out of scope by the negative tests and are deliberately left alone (Phase 1).

## Phase 2 — why the fold is exact

| concern | resolution |
|---|---|
| **width / truncation** | An integer `IRBinOp` **always** lowers to a full-width `($i64)` or `($u64)` ALU op (`codegen._gen_IRBinOp` selects only those two), and `IRAssign` lowers to `+ d ($i64) $r0 x`. Both are 64-bit register copies — there is no narrow form to truncate through, at any C width. |
| **signedness** | Irrelevant for a copy; `x + 0 == x` in two's complement for signed and unsigned alike. Tested both ways. |
| **float** | Excluded by the pre-existing `if ins.ftype is not None: return None` guard, and it must stay excluded: `x + 0.0` is **not** x for `x = -0.0`, and it quiets a signalling NaN. Tested for `$f32` and `$f64`. |
| **side effects / traps / volatile** | `IRBinOp` is a pure value producer. Memory, calls and volatile accesses are separate IR nodes this pass never inspects. |
| **overflow** | Adding zero cannot overflow. |

## Phase 3 — where it lives, and why

`strength_reduce.py` — the compiler's **existing generic `IRBinOp` algebraic
rewrite layer**. It already owns `x*2^n → x<<n`, `x/2^n → x>>n`, `x%2^n → x&…`,
already reasons carefully about signedness and float, and already runs at the
right point (`compiler.py:635`, after vectorization emits the residue and before
`promote_loop_counters` and the copy-prop/coalesce/DCE clean-up).

Its own `_pow2_exp` docstring already asserted that identities were *"handled
elsewhere"*. **They were not handled anywhere.** `sccp.py:137` folds only when
**both** operands are constant (`_is_const(l) and _is_const(r)`), so `x + 0`
with a variable `x` fell to `_OVER` and survived into codegen as a real
instruction in every program.

Rejected alternatives: `sccp.py` (its lattice is about constants, not algebraic
identities); a new pass (nothing to justify one); anything matmul-aware
(the rule is algebraic and must not know what a kernel is).

The fold rewrites to `IRAssign` and **deletes nothing itself** — the existing
copy-propagation → coalescing → DCE erase the copy. Kill switch
`APARA_NO_IDENTITY_FOLD=1`.

## IR before / after

```
BEFORE                                  AFTER
  _vgl8  = *(_vgb7+0)                     _vgl8  = *(_vgb7+0)
  _vgo9  = _vgl8 + 0        <- folded     ---                (copy-propagated away)
  _vgo10 = _vgo9 << 4                     _vgo10 = _vgl8 << 4
  _vgo11 = _vgo10 + 0       <- folded     ---                (copy-propagated away)
  _vra13 = _vrb12 + _vgo11                _vra13 = _vrb12 + _vgo10
```

Emitted, in the hot block `fb_6`:

```
BEFORE                                  AFTER
  ||  + $r7  ($i64) $r19 0  ; ...         (gone)
  ||  << $r8 ($i64) $r7  4  ; ...         ||  << $r2 ($i64) $r18 4 ; ...
  ||  + $r10 ($i64) $r8  0  ; $null ×7    (gone — was a bundle of its own)
  ||  + $r11 ($i64) $r6  $r10             ||  + $r7 ($i64) $r6 $r2
```

## Phase 8 — where the ticks came from

The gain is **not** proportional to instructions removed. It depends entirely on
whether the removed instruction sat in a serial dependence chain:

| block | bundles | instrs | ticks | effect |
|---|---|---|---|---|
| `fb_6` (hot body) | 15 → **12** | 60 → **57** | 480 → **384** | 3 instructions removed, **3 whole bundles gone** — each was alone in its bundle, one link of a serial address chain |
| `fb_2` (row prologue) | 5 → 5 | 26 → **25** | 80 → 80 | 1 instruction removed, **slot freed only** — the bundle had other work |
| `fc_5`, `fe_8`, `fc_1`, misc | unchanged | unchanged | unchanged | untouched |

**All 96 ticks come from `fb_6`**: 3 bundles × 32 iterations. Removing an
instruction from a **serial chain removes a bundle**; removing one from a full
bundle removes nothing. This is why 4 static instructions are worth 12.1%.

## Phase 6/7 — cross-kernel coverage: 32 fixed-DMEM configurations

Every configuration correct (256 or 1024 gcc-verified PostConditions), 0 errors,
identity copies reduced to **1 everywhere** (see below).

| config | JT=1 | JT=2 | JT=4 | JT=8 |
|---|---|---|---|---|
| `vu8`/`vi8` 16×16 | 2763→**2251** −18.5% | 1483→**1227** −17.3% | 861→**731** −15.1% | 795→**699** −12.1% |
| `vu8`/`vi8` 32×32 | 11643→**9595** −17.6% | 6523→**5499** −15.7% | 4539→**3771** −16.9% | 3195→**2811** −12.0% |
| `vu16`/`vi16` 16×16 | 3291→**2763** −16.0% | 1883→**1611** −14.4% | 1323→**1131** −14.5% | 971→**875** −9.9% |
| `vu16`/`vi16` 32×32 | 15803→**12699** −19.6% | 9147→**8091** −11.5% | 5851→**5083** −13.1% | 4887→**4471** −8.5% |

**Matrix total 149916 → 126616 ticks, −15.54%.** Every one of the 32
configurations improves; none regresses. The gain is *largest at JT=1* (−16% to
−19.6%), where the identity copies are the biggest share of a smaller loop body —
confirming the transform is about the address chain, not about tiling.

### Identity copies removed

Static count per program falls from **5 to 1** across the entire matrix. The one
survivor is `+ $r5 ($i64) $r28 0` in `main`, executed **once**: it is emitted
directly by codegen as the GBASE materialisation for `IRGlobalAddrOf`, not by an
`IRBinOp`, so an IR-level `IRBinOp` rule cannot reach it. Worth 1 tick; noted,
not chased.

## Phase 9 — existing kernels

38-program verification suite, before vs after, all 14 metric columns:

| family | R16.5 | R17.1 | delta | changed |
|---|---:|---:|---:|---|
| **gemm** | 28054 | **26486** | **−1568** | **6 / 6** |
| elementwise | 5780 | 5780 | 0 | 0 / 6 |
| axpy | 5375 | 5375 | 0 | 0 / 6 |
| dot | 8620 | 8620 | 0 | 0 / 6 |
| reduction | 3578 | 3578 | 0 | 0 / 6 |
| conv3 | 3201 | 3201 | 0 | 0 / 6 |
| scalar | 13076 | 13076 | 0 | 0 / 2 |
| **total** | **67684** | **66116** | **−2.32%** | **6 / 38** |

| gemm | ticks | executed instrs | static bundles | IPB |
|---|---|---|---|---|
| `vi8` | 4375 → **4103** (−6.22%) | 8170 → 7898 | 54 → 52 | 1.867 → 1.925 |
| `vu8` | 4631 → **4359** (−5.87%) | 8682 → 8410 | 55 → 53 | 1.875 → 1.929 |
| `vi16` | 4375 → **4119** (−5.85%) | 12029 → 11757 | 54 → 53 | 2.749 → 2.854 |
| `vu16` | 4631 → **4375** (−5.53%) | 12541 → 12269 | 55 → 54 | 2.708 → 2.804 |
| `vi32` | 4893 → **4637** (−5.23%) | 17153 → 16881 | 58 → 57 | 3.506 → 3.640 |
| `vu32` | 5149 → **4893** (−4.97%) | 17665 → 17393 | 59 → 58 | 3.431 → 3.555 |

**Every changed program improved; none regressed; all remain correct and
spill-free.** The 32 unchanged programs are bit-identical — dot, sum-reduction,
convolution, AXPY and elementwise lowerings do not clone a row base, so they
never produced the residue. Exactly `−272` executed instructions on each of the
six GEMMs, which is the transform, not a coincidence.

## Phase 11 — change attribution

Instruction multisets compared **with register numbers erased** — removing
instructions reshuffles register allocation, so a literal text diff compares
noise rather than the transformation:

```
static instructions: before=136 after=132  delta=-4
REMOVED shapes (4):   x4  + $r ($i64) $r 0
ADDED   shapes (0):   (none)
removed instructions that are NOT an integer add with a zero operand: 0
```

**Every removed instruction is an integer add with a zero operand. Nothing was
added. No unrelated opcode changed.**

## Phase 10 — full validation

| gate | result |
|---|---|
| 38-program verification suite | **38/38 PASS** |
| negative controls | **3/3 rejected** |
| `pipeline_crosscheck` | **124/124** — 0 IR, 0 code, 0 selected-tier mismatch; 0 verifier failures, 0 rollbacks |
| datatype × size × tile matrix | **32/32 correct**, 0 errors |
| `_r17_1_test.py --unit` | **24/24** |
| R13.0 / R13.1 | 59/59 · 38/38 |
| R14.1a / R14.2 / R14.3 / R14.6 / R14.8 / R14.9 / R14.10 | 43/43 · 15/15 · 15/15 · 17/17 · 24/24 · 7/7 · 9/9 |

R14.3 — whose epilogue assertion R16.5 re-baselined — passes unchanged, as does
R14.6, whose `dest != accum` distinction R16.5 depends on.

## Latent finding — a redundant instruction was making one kernel *faster*

Writing the identity into the **source** exposed a scheduling deficiency that
has nothing to do with this fold, and it is worth recording because it briefly
looked like a regression.

| scalar kernel, 64 elements | fold OFF | fold ON |
|---|---:|---:|
| `results[i] = (long long)(A[i] + 0);` | **540** | 668 |
| `results[i] = (long long)A[i];` | 668 | 668 |

With the fold ON, `A[i] + 0` compiles **byte-identically** to `A[i]` — which is
exactly what an identity fold must do, and the test now asserts precisely that.

**The 540 was the accident.** The redundant `+ 0` handed the scheduler a spare
independent instruction, which let it interleave the operand-address chain and
the result-address chain two-per-bundle:

```
fold OFF, `A[i] + 0`                     fold ON (and plain `A[i]`)
  << $r4 = $r3 << 3                        << $r4 = $r3 << 3
  $set $r7 = 512      | + $r3 += 1         + $r3 += 1
  + $r6 = $r4 + 0     | + $r7 = $r7 + $r4  + $r6 = $r4 + 0
  + $r6 = $r28 + $r6  | + $r7 = $r28 + $r7 + $r6 = $r28 + $r6
  $ld $r5 [$r6 + 0]                        $ld $r5 [$r6 + 0]
```

Without the spare instruction the two chains **serialize**. So the compiler
emits *worse* code for `A[i]` than for the semantically identical `A[i] + 0` —
a scheduler/region issue, present at R16.5 and unchanged by R17.1.

Scope check: this needs a **source-level** `x + 0`, which no real program
writes. The 38-program suite has **zero regressions** and the 32-configuration
matrix improves everywhere. `results[i] = A[i]*3+1` and a sum reduction are
**byte-identical** with the fold on and off. Filed as a finding for a future
scheduling milestone; not fixed here.

## Tests — `_r17_1_test.py`

```bash
python3 _r17_1_test.py --unit    # 24 checks, no simulator, instant
python3 _r17_1_test.py           # + end-to-end and a datatype/tile subset
python3 _r17_1_test.py --full    # the whole 4 datatypes x 4 tiles matrix
```

The end-to-end tier pins the property that actually matters — **`A[i] + 0`
compiles byte-identically to `A[i]`** — rather than comparing one source across
the kill switch, which is the trap described above.

The unit tier pins the algebraic rule (both operand orders, signed and
unsigned), its **safety boundary** (`x + 5`, `x + y`, float `$f32`/`$f64`, and
the out-of-scope identities `x-0`, `x*1`, `x|0`, `x^0`, `x<<0`), the kill
switch, and that **the pre-existing strength reductions are not weakened**
(`x*8 → x<<3`, unsigned `x/8 → x>>3`, and signed `x/8` still refused).

## Status

**R17.1 COMPLETE.** One rule in the existing generic algebraic layer; no
scheduler, bundler, allocator, vectorizer or codegen change. Tags `r10-final`,
`r11-verified`, `r12.1-verified` untouched. Nothing pushed.

**NOT implemented here** (R17.0 identified them; they are separate milestones):
dead accumulator write-back elimination (measured −48 ticks on the primary
target), `$u128` wide loads, scheduler and allocator work.

**Do not start R17.2 automatically.**

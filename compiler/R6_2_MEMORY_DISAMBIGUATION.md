# R6.2 — Advanced Vector Memory Dependence Analysis

**Scope kept:** this milestone changed dependence *analysis* only. No vectorizer,
no scheduler algorithm, no code generator, no ISA assumption, no loop unrolling,
no software pipelining, no multiple accumulators, no kernel-specific logic and no
AXPY special case. The one production file modified is `bundler.py`, and only to
*consult* the new analysis; its packing and scheduling algorithms are untouched.

**Headline, stated honestly up front.** The analysis works, is validated, and
changes what the compiler emits — 9 of 25 vector kernels now select a much denser
code shape, with vector-body occupancy rising from ~19% to ~78% on those. But the
end-to-end **measured** gain is small: −3.2% dynamic bundles on the kernel suite,
and **−0.02% simulator ticks** on the verified regression suite. R6.1 projected
+13.9% for disambiguation alone; that projection was too optimistic, and §6
explains exactly why. The full prize needs the unrolling that R6.2 is forbidden
from implementing.

---

## 1. Architecture

```
        mcode front-end  ──┐                             ┌──>  bundler.py
                           ├── memory_objects.classify ──┤     (packer + list scheduler)
        IR front-end     ──┘   (THE decision procedure)  └──>  DependenceGraph
                                                               via its existing
                                                               `disambiguator=` hook
```

| file | contents |
|---|---|
| `vector_backend/memory_objects.py` | `SymAddr` affine address algebra, `MemRef`, and the `classify` / `classify_carried` decision procedure |
| `vector_backend/mem_dependence.py` | the two front-ends, the bundler entry point `independent()`, and `StrongDisambiguator` for the IR graph |
| `bundler.py` | consults `_proved_independent` **before** the existing textual rule |

One analysis, two front-ends, two consumers. The decision procedure is shared, so
there is no parallel alias framework and no duplicated reasoning. Integration with
the IR dependence graph is through the `disambiguator=` parameter
`DependenceGraph` **already exposes**, so no frozen R2.1/R2.2 code was modified
and any pass that wants the stronger information passes the stronger
disambiguator.

---

## 2. Dependence algorithm

### 2.1 The model

An address is an affine symbolic expression

```
    sum(coeff_k * sym_k)  +  constant
```

where each `sym` is an opaque **value** — a register's content at its definition,
the frame pointer, an unknown loaded word. Two accesses are compared by
**subtracting** their expressions:

* every symbol cancels → the difference is a known integer `d`, and independence
  is decided by comparing `d` against the two access widths;
* any symbol survives → the difference is unknown → **may alias**.

This subsumes base+constant reasoning instead of replacing it, and it needs no
knowledge of object extents — which is what makes `A[i]` vs `B[i]` provable:

| case | addresses | difference | verdict |
|---|---|---|---|
| different objects `A[i]`, `B[i]` | `FP−64+v`, `FP−128+v` | 64 | **independent** |
| same object `A[i]`, `A[i+8]` | `FP−64+v`, `FP−64+v+8` | 8 | **independent** |
| unrolled copies `A[i]`, `A[i+VL]` | `FP−64+v`, `FP−64+v+8` | 8 | **independent** |
| affine `base+i*s`, `base+(i+k)*s` | `b+4v`, `b+4v+8` | 8 | **independent** |
| different indices `A[i]`, `B[j]` | `FP−64+v`, `FP−128+w` | `w` survives | may alias |
| identical address | `FP−64+v`, `FP−64+v` | 0 | **must alias** |

### 2.2 The mcode front-end

The bundler works on register-allocated text, so the front-end symbolically
evaluates the address arithmetic:

```
+ $r7 ($i64) $r26 -128       ->  r7 = FP - 128
+ $r3 ($i64) $r3 8           ->  r3 = <r3 on entry> + 8
$ld ($i64) $r9 [$r6 + $r3]   ->  address = <r6 on entry> + <r3 on entry>
```

Evaluation is per basic block — precisely the scope in which the answer is used,
since the packer and the list scheduler never cross a block boundary.

**Carrying values across blocks.** Block-local evaluation alone cannot relate two
array bases, because LICM hoists `FP + k` into the loop **preheader**; inside the
body every base is an unrelated live-in. A register **written exactly once in the
whole function** may therefore keep its value across blocks. Soundness: with a
single definition, any dynamic use must be preceded by it, so the value at every
later use is the one computed there. Values are carried forward in program order
only, and multiply-defined registers — induction variables, accumulators — still
get a fresh opaque symbol per block, because a back edge can change them.

### 2.3 The loop-carried rule

For two accesses in different iterations of a loop whose induction value advances
by `step`:

```
    d(delta) = (addr_a - addr_b) + k * step * delta ,   delta != 0
```

requiring the IV coefficient `k` to be identical in both (or the symbol does not
cancel). The finitely many `delta` that could bring the ranges together are then
enumerated and checked. This generalises R2.2's same-base SIV rule to arbitrary
affine addresses and to real access widths.

### 2.4 Where it refuses to answer

Every one of these returns *may alias*, by construction:

* an unrecognised instruction defining a register yields a **fresh opaque
  symbol** — never an assumption about its value;
* **only 64-bit address arithmetic is interpreted.** A narrower add wraps at its
  own width, and modelling it with unbounded integers would be unsound;
* **`$set` is opaque** — it writes a 16-bit *field*, and a register can be
  assembled from several of them;
* any surviving symbol, any unknown base, any width the model cannot bound.

---

## 3. Examples of newly-proven independence

Each is proved on real mcode text in `_r6_2_test.py`, not on a hand-built
structure:

```
different objects          + $r6 ($i64) $r26 -64     + $r7 ($i64) $r26 -128
                           $ld ($i64) $r9 [$r6 + $r3]
                           $st ($i64) [$r7 + $r3] $r9        -> INDEPENDENT

same object, next chunk    $ld ($i64) $r9 [$r6 + 0]
                           $st ($i64) [$r6 + 8] $r9          -> INDEPENDENT

unrolled iterations        + $r4 ($i64) $r3 8
                           $st ($i64) [$r6 + $r3] $r1
                           $ld ($i64) $r9 [$r6 + $r4]        -> INDEPENDENT

affine with a stride       * $r5 ($i64) $r3 4    + $r4 ($i64) $r3 2
                           * $r7 ($i64) $r4 4
                           $st [$r6 + $r5]  vs  $ld [$r6 + $r7]  -> INDEPENDENT

computed base              + $r7 ($i64) $r6 64
                           $st [$r6 + $r3]  vs  $ld [$r7 + $r3]  -> INDEPENDENT

loop-carried, step 8       a[i] store vs a[i] load across iterations
                                                             -> INDEPENDENT
```

On one compiled axpy kernel the model removes **166 textual conflicts**, and
every one of those proofs survives randomised concretisation (§8).

---

## 4. Examples that remain conservative

| shape | why it must stay conservative |
|---|---|
| `$st [$r6+$r3]` vs `$ld [$r6+$r5]` | two unrelated index registers — nothing relates `v3` and `v5` |
| base loaded from memory | the base is an opaque value, not an expression |
| `+ $r6 ($i32) …` | 32-bit arithmetic wraps at 32 bits; interpreting it as unbounded would be unsound |
| `$set $r6 0 1024` | `$set` writes a 16-bit field; the register may be assembled from several |
| 8-byte accesses 4 bytes apart | the ranges genuinely overlap |
| two `$st ($i32)` in one 64-bit word | a sub-word store is a read-modify-write of the whole word, so disjoint *byte* ranges still race |
| a register written twice | it is not carried across blocks; a back edge could change it |
| `A[i]` vs `B[j]`, unrelated indices | requires object extents, which this model deliberately does not use |

The sub-word-store rule deserves emphasis: byte-range disjointness is enough for
the *textual* rule, whose pairs share a base register and are naturally aligned by
construction. This model can relate accesses through **different** base
registers, where that guarantee does not exist, so a pair involving a store
narrower than a word additionally requires a full 8-byte separation.

---

## 5. Bundle occupancy before and after

Measured on the R6.1 kernel suite through the real production path, with
`APARA_NO_MEMDISAMB=1` as the "before".

| kernel | realisation before → after | static bundles | dynamic bundles | vector-body occupancy |
|---|---|---|---|---|
| elementwise add | compact+peeled → **unrolled** | 25 → 23 | 77 → 51 | 18.8% → **76.2%** |
| elementwise mul | compact+peeled → **unrolled** | 25 → 23 | 77 → 51 | 18.8% → **76.2%** |
| elementwise copy | unrolled → **unrolled+peeled** | 25 → 16 | 50 → 16 | 37.5% → **61.4%** |
| expr a+b+c | compact+peeled → **unrolled** | 27 → 27 | 85 → 58 | 20.0% → **79.8%** |
| expr a\*b+c | compact+peeled → **unrolled** | 27 → 27 | 85 → 58 | 20.0% → **79.8%** |
| expr (a+b)\*c | compact+peeled → **unrolled** | 27 → 27 | 85 → 58 | 20.0% → **79.8%** |
| axpy vi8 | compact → **unrolled** | 20 → 18 | 87 → **18** | 17.5% → **67.3%** |
| conv 3-tap | compact+peeled → **unrolled** | 30 → 28 | 100 → 72 | 30.4% → **79.8%** |
| conv 3-tap vi16 | compact+peeled → **unrolled+peeled** | 25 → 21 | 101 → **21** | 32.8% → **70.3%** |
| expr a+b+c+d | unchanged | 30 → 28 | 94 → 92 | 20.8% |
| dot vi8 | unchanged | 21 → 20 | 74 → 73 | 37.5% |
| axpy remainder | unchanged | 22 → 21 | 53 → 52 | 48.4% → 55.4% |
| conv 5/7-tap | unchanged | 30 → 29 / 32 → 31 | −1 each | 34.7% / 37.5% |
| gemm ×3, conv2d ×4, dot vi16, reduction ×2, axpy vi16 | unchanged | unchanged | unchanged | unchanged |

**Totals:** static bundles **822 → 795 (−3.3%)**, dynamic bundles
**10868 → 10518 (−3.2%)**, dynamic IPB **2.074 → 2.114 (+1.9%)**.

The mechanism behind the big movers is worth stating precisely, because it was
not the intended one. R4.2.5 chooses per kernel between a *compact* vector loop
and a *fully unrolled* chunk sequence by **measuring** which packs smaller. The
unrolled candidate is a straight-line block full of independent chunk accesses —
exactly the shape the old textual rule could not disambiguate. With R6.2 those
chunks pack, the unrolled candidate wins the measurement, and the compiler
selects it. **R6.2 did not implement unrolling; it made the unrolled form that
already existed viable.**

---

## 6. Measured IPB improvement, and why it is small

| measurement | before | after | change |
|---|---|---|---|
| kernel suite, dynamic bundles (25 kernels) | 10868 | 10518 | **−3.2%** |
| kernel suite, dynamic IPB | 2.074 | 2.114 | **+1.9%** |
| verified suite, simulator ticks (27 programs) | 67406 | 67391 | **−0.02%** |
| verified suite, static bundles (38 programs) | 2205 | 2165 | −1.81% |

**R6.1 projected +13.9% for "memory disambiguation alone". The measured figure is
about +2%.** The discrepancy is a defect in the R6.1 experiment, and it is worth
recording:

R6.1's `unroll1_disamb` what-if rewrote `[$rb + $riv]` into `[$rb + 0]` as part
of its "constant addressing" model — even at unroll factor 1. That rewrite
*changed the addressing form*, which is a different intervention from supplying
distinct-object information. The projection therefore measured two changes and
attributed both to disambiguation.

The deeper reason the gain is small is the one R6.1 got right: **a compact vector
loop body is a serial RAW chain, not an alias-limited block.** In the axpy body

```
$ld -> $v * -> $v + -> $st
```

every bundle boundary is a true data dependence. Proving the store disjoint from
the loads changes nothing, because the store cannot issue before the value it
stores exists. Disambiguation pays only where several independent memory streams
are in flight — which is what unrolling creates, and R6.2 is explicitly forbidden
from unrolling.

**On the simulator the effect is smaller still (−0.02% ticks)** because in the
verified regression programs the vector loop is a small fraction of the work: each
program spends most of its ticks in scalar initialisation loops. This is an
honest limitation of that benchmark shape, not evidence that the compiled kernels
did not improve — the same programs' static bundle counts fell 1.81%, and the
kernel-level dynamic bundles fell 3.2%.

---

## 7. Dynamic bundle reduction

Per-kernel dynamic bundle reduction, kernel suite:

```
axpy vi8            87 -> 18    -79.3%   ####################
conv 3-tap vi16    101 -> 21    -79.2%   ####################
elementwise copy    50 -> 16    -68.0%   #################
expr a+b+c          85 -> 58    -31.8%   ########
expr a*b+c          85 -> 58    -31.8%   ########
expr (a+b)*c        85 -> 58    -31.8%   ########
elementwise add     77 -> 51    -33.8%   ########
elementwise mul     77 -> 51    -33.8%   ########
conv 3-tap         100 -> 72    -28.0%   #######
axpy remainder      53 -> 52     -1.9%   
dot vi8             74 -> 73     -1.4%   
expr a+b+c+d        94 -> 92     -2.1%   
(13 kernels unchanged)
```

Nine kernels improve substantially; sixteen do not move at all. The suite total
is dominated by the unchanged gemm and conv2d kernels, which together account for
~9200 of the 10868 dynamic bundles — hence a −3.2% total behind −79% peaks.

---

## 8. Correctness discussion

Correctness was treated as absolute; the evidence is in four independent layers.

**1. Structural — the rule can only ever remove a conflict.** The model is
consulted *before* the textual rule and only to skip it. Everything unproven —
a missing reference, an unknown address, a surviving symbol, an exception inside
the analysis — returns `False`, and the pre-R6.2 behaviour applies unchanged.
Verified on a compiled kernel: 166 conflicts removed, **zero** pairs conflicting
where the textual rule did not.

**2. Randomised concretisation — an independent check of the algebra.** For every
pair the model proves independent, random concrete values are assigned to the
opaque symbols and the byte ranges are checked directly. **172 proofs × 200 draws
= 34 400 concrete checks on real compiled code, zero overlaps**, plus 1 200 more
on targeted fragments. This re-derives the answer a different way, so an algebra
error surfaces as an overlap rather than as silence.

**3. Soundness under reordering.** The references are computed on the
pre-scheduling order and the scheduler then moves instructions. This remains valid
because `bundler._must_precede` unconditionally preserves every register RAW, WAR
and WAW dependence, so no definition can cross a use or another definition of the
same register: a register's symbolic value at a given instruction is invariant
under any schedule the bundler is permitted to produce.

**4. Execution on the simulator — the R6.2A harness.** Every program is compiled,
assembled, run on `mcode_run` and compared against an independent gcc reference,
with six checks that reject vacuous verification.

```
                        before R6.2      after R6.2
verified programs         27 / 38          27 / 38
status changes                   NONE
negative controls        all 3 rejected   all 3 rejected
```

**No program changed status.** The same 27 pass; the same 11 fail for the
pre-existing reasons documented in R6.2A (unaligned convolution loads, 16/32-bit
packed GEMM, `vu8_t` sign extension) — none introduced by R6.2, all reproducible
with `APARA_NO_MEMDISAMB=1`.

**Kill switch.** `APARA_NO_MEMDISAMB=1` reproduces the pre-R6.2 mcode **byte for
byte** (asserted in `_r6_2_test.py`), so any suspicion can be settled in one run.

**Regression suite.** `_r6_1_test.py`, `_r6_2_test.py`, `_r3_1`, `_r3_2`,
`_r4_1`, `_r4_4`, `_r4_5`, `_r4_6` all pass; `pipeline_crosscheck` PASS 124/124.

**Two R6.1 assertions were updated, not weakened**, and both because R6.2
deliberately changed the thing they asserted: axpy vi8 is no longer a compact
loop (the realisation moved), and `bundler.py` now imports the memory model by
design. The R6.1 measurement modules are still absent from every production path,
which is what that test now checks.

---

## 9. Remaining limitations

* **The prize needs unrolling.** The measured +1.9% IPB against R6.1's projected
  +36.7% for unroll+disambiguation is the gap: R6.2 supplies half a mechanism.
  The compact loops that remain are RAW-chain-bound and no amount of alias
  information will help them.
* **The R6.1 what-if experiments still pack an isolated block**, so they cannot
  see the preheader where the array bases are defined and continue to
  under-report what the real bundler now achieves for compact loops. The
  production measurements in §5 do not have this limitation.
* **The IR-side integration is built but not enabled in production.**
  `StrongDisambiguator` plugs into `DependenceGraph`'s existing hook and is
  available to any pass; wiring it into the production IR scheduler would change
  scalar scheduling and was left for a milestone that can validate that
  separately.
* **Object extents are not used.** `A[i]` vs `B[j]` with unrelated indices stays
  conservative. Frame layout would settle many such pairs and is the obvious next
  extension.
* **No range analysis.** Nothing bounds an induction variable, so independence
  that depends on a trip count is not provable at mcode level.
* **The unbounded-integer assumption.** Address arithmetic is modelled without
  wraparound. Every address here lives in a 64 KB DMEM, and the existing textual
  rule makes the same assumption when it calls `[r+0]` and `[r+8]` distinct.
* **`mcode_align` alignment behaviour is unchanged**, so the convolution
  unaligned-load defect (R6.2A D1) is untouched — and R6.2 moved conv 3-tap to
  the unrolled realisation, which is still wrong on hardware, just differently
  packed. **Fixing D1/D2/D3 should precede any further backend aggression.**

---

## 10. Reproduction

```sh
cd compiler
python3 _r6_2_test.py                         # capabilities, conservatism, soundness
python3 -m verification --csv after.csv       # simulator, 38 programs
APARA_NO_MEMDISAMB=1 python3 -m verification --csv before.csv
python3 -m vector_backend.ilp_analysis        # R6.1 analysis under R6.2
```

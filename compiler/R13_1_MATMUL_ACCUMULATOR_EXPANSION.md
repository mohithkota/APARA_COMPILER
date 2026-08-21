# R13.1 — Generic dot-shaped accumulator expansion

Branch `feature/r13-matmul-dot`, on top of R13.0 Phase 5 (`377ea84`).
`r10-final`, `r11-verified`, `r12.1-verified` untouched. Nothing pushed.
No tiling, no new scheduler, no new `$dot` backend, no second accumulator pass.

## Answer to the final question

**Yes.** Generic R6.6-style accumulator expansion removes the matmul
dot-accumulator dependence bottleneck and materially improves ticks/output, with
**zero spills and zero change to any existing kernel**.

| case | R13.0 t/out | **R13.1 t/out** | gain |
|---|---|---|---|
| vu16 8×8 | 26.42 | **24.42** | 7.6% |
| vi8 16×16 | 24.00 | **22.00** | 8.3% |
| vu8 16×16 | 25.00 | **23.00** | 8.0% |
| vi16 16×16 | 28.06 | **24.06** | 14.3% |
| vi8 24×24 | 26.64 | **24.64** | 7.5% |
| vi8 32×32 | 28.50 | **24.50** | 14.0% |
| vu8 32×32 | 29.50 | **25.50** | 13.6% |
| vi16 32×32 | 36.53 | **25.53** | **30.1%** |
| vu16 32×32 | 37.53 | **26.53** | **29.3%** |

## 1. The existing R6.6 mechanism

`reduction_accumulator_expansion.py`. `plan_expansion(plan, u)` returns a
prologue (accumulator 0 takes the live value, the rest start at 0), a per-copy
`acc_k = acc_k + partial`, and an epilogue folding the accumulators with a
**balanced tree** (deliberately not a chain, which would re-create the
dependence). Correctness rests on integer addition being associative including
under two's-complement wrap; floats are rejected by name. `best_accumulator_count`
chooses k by minimising `ceil(chunks/k) + log2(k)`, preferring the smallest k
because R7.0 established register pressure as the binding constraint.

**R13.1 reuses this implementation unchanged.** The balanced-tree fold, the
prologue/epilogue construction and the associativity argument are all the
existing ones.

## 2. Two gates, both were kind-based

1. `vector_lowering.py:443` — *which* k:
   `k = best_accumulator_count(chunks) if plan.kind == 'dot-product' else 1`
2. `reduction_accumulator_expansion.eligible()` — *whether at all*:
   `if plan.kind not in ('sum-reduction', 'dot-product')`

matmul failed both, so it received **k = 1**.

## 3. The structural predicate

`vector_lowering.is_dot_shaped(plan)` — promoted from R13.0's private helper —
is true when **two multiplicand arrays feed the accumulator**. It is about the
reduction structure, never a kind string, kernel name, datatype or matrix size.

- gate 1 → `if is_dot_shaped(plan) else 1`
- gate 2 → `if not (is_dot_shaped(plan) or plan.kind == 'sum-reduction')`

**Why matmul qualifies:** its reduction value is `load * load` (the detector's own
`reduction_value == 'dot'`), so `plan_lowering` extracts two array slots.
**Why sum-reduction does not:** one operand array, so `is_dot_shaped` is false
and it still gets k = 1 in the fully-unrolled realisation — R8.1a's restriction
preserved exactly. It remains eligible in the *compact* path, unchanged.

## 4. The k model was wrong, and measurement proved it

Pinning k with the new `APARA_ACC_COUNT` (mirroring `APARA_VECTOR_UNROLL`):

| case | chunks | k=1 | k=2 | k=4 | k=8 |
|---|---|---|---|---|---|
| vu8 16×16 | 2 | 25.00 | **23.00** | – | – |
| vi8 16×16 | 2 | 24.00 | **22.00** | – | – |
| vi16 16×16 | 4 | 28.06 | 24.06 | **23.06** | – |
| vu8 32×32 | 4 | 29.50 | 25.50 | **24.50** | – |
| vi16 32×32 | 8 | 36.53 | 28.53 | **25.53** | 33.66 |

(k above `chunks` clamps to `chunks`, hence the dashes.)

**k>1 helps in every case.** But `best_accumulator_count(2)` returned **1** — it
charged k=1 only `chunks`, tying with k=2 and losing on the smaller-k tie-break.
The model was missing a real cost: with one accumulator the lowering emits
`IRVecDot(fresh, .., accum=acc); acc = fresh`, which codegen realises as a
**register copy after every chunk**, so the k=1 chain is `dot→copy→dot→copy` —
2 per chunk, not 1. Charging `2*chunks` for k=1 is a structural correction, not
a tuning constant. It changes k for **chunks=2 and chunks=3 only**; every other
chunk count is untouched.

**Honest residual:** at chunks=4 the model ties k=2 with k=4 and keeps k=2,
leaving ~4% (vi16 16×16: 24.06 vs a measured 23.06). The tie-break was NOT
changed, because preferring the larger k would select k=8 at chunks=8, which
**regressed 32%** (33.66 vs 25.53). Register pressure is real at k=8.

## 5. The dependence chain actually disappeared

vu8 16×16 hot block, `fb_10` — not an IPB story, a bundle-count story:

**R13.0 — 9 bundles, 18 instrs, IPB 2.00**
```
b5 [#.......] $dot $accumulate $r8  ($vu8) $r23 $r7
b6 [#.......] + $r10 ($i64) $r0 $r8          <- register copy
b7 [#.......] $dot $accumulate $r10 ($vu8) $r24 $r9
b8 [#.......] + $r29 ($i64) $r0 $r10         <- register copy
```
**R13.1 — 7 bundles, 20 instrs, IPB 2.86**
```
b5 [##......] $dot $accumulate $r9  ($vu8) $r23 $r7 | $dot $accumulate $r10 ($vu8) $r24 $r8
b6 [#.......] + $r29 ($i64) $r9 $r10          <- the balanced-tree fold
```
**Both `$dot`s now issue in the SAME bundle.** The four 1/8 bundles are gone;
the chain lost 2 links. Instruction count *rose* 18→20 (accumulator init plus
the fold) while bundles *fell* 9→7 — the correct trade, and the reason ticks
fell rather than IPB merely rising.

## 6. Register pressure

**Zero spills in every configuration measured** (`spills 0->0`), at every k and
every datatype/size. The allocator was not weakened, the budget not raised, no
matmul-specific spill policy added. The k=8/chunks=8 regression is the pressure
cost showing up as *time*, not spills, and the model's `cap`/tie-break already
avoid it.

## 7. Existing-kernel regression

| check | Phase-0 baseline | R13.1 |
|---|---|---|
| 38-program suite | 38/38 | **38/38, metrics CSV bit-for-bit identical** |
| negative controls | 3/3 | **3/3** |
| `pipeline_crosscheck` | 124/124 | **124/124**, 0 IR / 0 code / 0 tier mismatches |
| `compiler/_r*_test.py` | 20/20 | **22/22** (adds `_r13_0`, `_r13_1`) |
| `loopopt/_*_test.py` | 25/25 | **25/25** |
| `_r13_1_test.py` | — | **38/38** |

GEMM, reduction, convolution, dot, elementwise and AXPY are all inside the
38-program suite whose metrics are byte-identical, so none changed.

**One test file was edited, and it is worth stating plainly.** `_r6_6_test.py`'s
`FakePlan` stub carried only the four fields `plan_expansion` used to read; the
structural predicate reads a fifth (`array_slots`), so the stub raised
`AttributeError`. The stub now derives `array_slots` from its `kind`
(dot-product → 2, otherwise → 1). **No assertion was changed or weakened** —
`dot-product` is still asserted eligible and `saxpy` still asserted rejected.
Separately, `is_dot_shaped` was made non-raising via `getattr`, so a plan
lacking the field can never crash eligibility.

## 8. Adaptive selection — what is and is not adaptive

Stated precisely, because R13.0 Phase 6 found the U search to be nominal:

- **k is chosen by a closed-form model, not a measured search.** There is no
  build-and-compare over k anywhere in the framework. The model is now accurate
  at chunks ∈ {1,2,3,8,…} and 4% pessimistic at chunks=4.
- **U remains entirely nominal for matmul**: U∈{8,4,2,1} produce byte-identical
  mcode, because both consumers of `unroll_factor` live in the compact path,
  which matmul declines. Verified by md5 across all four values.

Building k candidates through the real pipeline and ranking them by
ticks/output would need the R6.4.1-style search extended to k; that is not in
R13.1 and is not invented here as a second selector.

## 9. Limitations

1. ~4% left at chunks=4 from the smaller-k tie-break (kept deliberately — see §4).
2. k is modelled, not searched (§8).
3. Compact realisation still declines based accesses (R13.0), so matmul has one
   realisation candidate.
4. The remaining gap to the hand-written reference is **not** the accumulator
   chain: that kernel keeps 8 output columns in flight, while the compiler still
   computes one output element per inner-loop trip. Closing that needs
   j-dimension tiling — deferred, and untouched here.

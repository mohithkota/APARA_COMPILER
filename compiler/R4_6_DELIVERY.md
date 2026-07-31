# R4.6 Delivery Report — Automatic Convolution Vectorization

**Milestone:** R4.6 (convolution as a client of the existing infrastructure).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-31

---

## 1. Headline result
**Convolution required no new client logic.** `conv_vectorizer.py` contains **under
20 executable lines** — an entry point and documentation — and is asserted by test
to contain no `IRVecArith`, no packed load/store, no `lower_vector`/`lower_scalar`
call, no `PeelTemplate` use and no `VectorTransform`. A fused convolution

```
    out[i] = w0*in[i] + w1*in[i+1] + w2*in[i+2];
```

is, to the existing framework, an **elementwise expression over shifted contiguous
accesses**: `vector_affine` already classifies `in[i+k]` as CONTIGUOUS,
`expression_tree` already represents the fused sum-of-products, and
`expression_lowering` already emits both its vector body and its scalar remainder.

## 2. The milestone's premise was *almost* right — one gap was real
The brief stated "no further infrastructure work should be necessary" and asked me
to validate that. **It was not quite true, and the gap was measured, not guessed.**
Before any change, every convolution form was *recognised and then rolled back*:

```
    out[i] = in[i+1]                  recognised -> differential mismatch -> rollback
    out[i] = in[i] + in[i+1]          recognised -> differential mismatch -> rollback
    out[i] = in[i]+2*in[i+1]+in[i+2]  recognised -> differential mismatch -> rollback
```

The cause was **addressing, not recognition or lowering**: the tree-driven vector
body addressed every array as `base + chunk*lanes*elem_bytes`, which silently
assumes the offset's invariant part is zero. True for `a[i]`, false for `in[i+k]`.

**No new mechanism was invented to fix it.** R4.4 had already solved the identical
problem for GEMM row bases with `clone_offset` (re-emit the loop's own address
computation with the induction variable substituted). R4.6 wires that same
function into the tree path:

- `ArrayRef` gained a field (`offset_expr`) recording the offset it came from —
  a field on an existing node, **not a new node type**;
- the elementwise/expression client uses `clone_offset` for both realisations
  (constant index when unrolled, re-loading the IV slot when compact) **only when
  the offset is not a bare `IV`/`IV*const`**, so R4.2/R4.5 output is unchanged;
- `MAX_DEPTH` was raised 4 → 8 so a 7-point stencil (depth 7) fits. A bound, not
  a mechanism: deeper trees are still declined, never mis-lowered.

Nothing else changed. No new IR, no new backend instruction, no new legality or
profitability analysis, no new remainder framework, no new expression node.

## 3. Results
```
  kernel                      result        bundles       dyn instrs    reduction
  1-D 3-tap                   VECTORIZED   21 -> 30     1708 -> 294      -83%
  1-D 5-tap                   VECTORIZED   23 -> 30     2124 -> 376      -82%
  1-D 7-tap                   VECTORIZED   25 -> 32     2508 -> 426      -83%
  1-D 3-tap weighted          VECTORIZED   22 -> 34     1891 -> 366      -81%
  1-D 5-tap weighted          VECTORIZED   24 -> 36     2419 -> 476      -80%
  1-D 3-tap vi16              VECTORIZED   21 -> 25      928 -> 281      -70%
  1-D 3-tap vi32              VECTORIZED   21 -> 25      928 -> 540      -42%
  1-D 3-tap remainder         VECTORIZED   21 -> 25      700 ->  67      -90%
  1-D 5-tap remainder         VECTORIZED   23 -> 29      900 ->  99      -89%
  REJECT dynamic window / gather / column-stride / window > MAX_DEPTH   scalar

  vectorized 9/9 expected · mismatches 0 · rollbacks 0
  bundles 201 -> 266 · dynamic instructions 14106 -> 2925 (-79.3%) · 188 ms/kernel
  vs R4.5 (depth bound 4, base-relative addressing): 5/13 -> 9/13
  Also newly accepted elsewhere: `c[i] = a[i+1] + b[i]`, rejected through R4.5.
  Full corpus: 124 programs, scalar byte-identical 124/124
```

## 4. Recognition versus lowering — the quantified answer
| added | lines | what |
|---|---|---|
| `conv_vectorizer.py` | **< 20 executable** | entry point only; zero lowering (test-asserted) |
| `expression_tree.py` | ~6 | one field + one constant |
| `vector_elementwise_lowering.py` | ~55 | wiring `clone_offset` into both realisations |
| `conv_corpus.py`, `_r4_6_test.py` | — | measurement and tests |

**Zero lines of new vector lowering, zero lines of new scalar lowering, zero new
reduction implementation.** The only non-test code is address plumbing that reuses
an existing function.

## 5. Success criteria
1. Convolution implemented almost entirely as a client ✅ < 20 executable lines.
2. No duplicated lowering logic ✅ test-asserted.
3. No duplicated scalar lowering ✅ the shared `lower_scalar` handles the tail.
4. No duplicated vector lowering ✅ the shared `lower_vector` handles the body.
5. Existing infrastructure reused throughout ✅ including `clone_offset` from R4.4.
6. Differential validation clean ✅ 0 mismatches.
7. No regressions ✅ 124/124 corpus identical; 13 suites and 8 corpora pass.

## 6. Honest notes / limitations
- **The premise did not fully hold** (§2). One real gap existed — offset-aware
  addressing in the tree path — and it is reported rather than glossed over. It
  was plumbing of an existing mechanism, not new infrastructure, but it was work.
- **Static bundles grow on convolution** (201 → 266, +32%). A k-tap stencil loads
  k overlapping windows, so the vector body is inherently larger than the scalar
  loop; the win is dynamic (−79.3%). No realisation avoids this — the compact and
  peeled variants are offered and the R4.2.6 gate picks the smaller.
- **vi32 gains least** (−42%): 2 lanes, so a 3-tap stencil barely amortises.
- **2-D stencils ARE accepted (fixed in R4.6.1, below).**
- **The tap-innermost form** (`for(i) for(r) out[i] += in[i+r]*w[r]`) is not
  recognised — its accumulator is an array element at an invariant address, which
  the reduction machinery does not model (it expects a scalar slot). The fused form
  above is the supported spelling.
- **Two test expectations were updated, not weakened**: `_r4_2_test.py` and
  `vector_elementwise_corpus.py` asserted `c[i]=a[i+1]+b[i]` is rejected; it is now
  correctly vectorized, so it moved to the newly-accepted list.

## 7. Test summary
```
_r4_6_test.py ........................ ALL R4.6 UNIT TESTS PASS  (50/50 checks)
_r4_5 / _r4_4_5 / _r4_4 / _r4_3 / _r4_2_8 / _r4_2_6 / _r4_2_5 / _r4_2 / _r4_1 / _r4_0 ... PASS
_r3_1 / _r3_2 ........................ PASS
conv / expression / gemm / axpy / compact / elementwise / dot / affine corpora ... PASS (8/8)
pipeline_crosscheck.py ............... 124/124 identical
```


---

# R4.6.1 Addendum — 2-D Stencil Nest Recognition

**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-31

The R4.6 report listed 2-D stencils as declined. That is now fixed, with two
further optimizations found along the way. No new IR, instructions, legality or
profitability analysis; no new expression nodes.

## 1. Root cause — the last stale `iv_terms` user
The elementwise **store detection** still used the pre-R4.2.8 mechanism:

```python
    stores = [... if instrs[i].offset.name in iv_terms]     # a SUM is never in it
```

`out[i*N+j]` has a **summed** offset, so a 2-D stencil was counted as **zero**
stores and declined with `expect-exactly-one-array-store(got 0)`. Exactly the bug
class fixed in `kernel_detector` for R4.4 — this was the last place in the
elementwise path still using it. It now asks `vector_affine` (`classify_access ==
CONTIGUOUS`), consistent with everything else.

## 2. Optimization — do not discard candidates the pipeline would accept
Multi-operand stencils (3×3 = 6 packed loads) then failed with
`lower:no-realisation-compiles`. Diagnosis: both realisations compile **spill-free**
under the plain backend, but spill under the **post-optimizer** probe (tier-1 +
superblock raise register pressure). `choose_smaller` discarded them — yet the
pipeline's own commit gate uses the *plain* probe and would have accepted them.

`choose_smaller` now falls back to the plain measurement when every candidate
spills under the post-optimizer probe, letting the pipeline's real spill gate
decide. That alone recovered the 3×3, weighted and vi16 stencils.

## 3. Correctness guard — the induction variable must start at zero
The 5-point cross (`for (j = 1; ...)`) then rolled back on a differential
mismatch: chunk addressing indexes elements as `0, lanes, 2*lanes, ...`, so a
non-zero IV start is lowered one element off. The differential caught it — no
wrong code was ever produced — but it is now **declined at match time**
(`iv-does-not-start-at-zero`) rather than burning a rollback.

## 4. Results
```
  2-D 3-point row            VECTORIZED   39 ->  52    868 -> 208   -76%
  2-D 3x3 stencil            VECTORIZED   42 -> 101   1260 -> 420   -67%
  2-D 3-point weighted       VECTORIZED   40 -> 103   1120 -> 271   -76%
  2-D 3-point vi16           VECTORIZED   41 ->  44    952 -> 367   -61%
  2-D 3-point remainder      VECTORIZED   39 ->  46    620 -> 180   -71%
  REJECT IV start != 0 / dynamic window / gather / column stride / too deep

  vectorized 14/14 expected · mismatches 0 · rollbacks 0
  bundles 402 -> 612 · dynamic 18926 -> 4371 (-76.9%) · 308 ms/kernel
  vs R4.5: 9/19 -> 14/19 · full corpus 124/124 byte-identical
```

## 5. Honest notes
- **Static bundles grow substantially on 2-D stencils** (3×3: 42 → 101, +140%).
  Six overlapping windows plus their cloned address computations are inherently
  bigger than the scalar loop; the win is dynamic (−67%). The R4.2.6 gate still
  picks the smaller of the four realisations.
- **`for (j = 1; ...)` is declined, not supported.** Supporting a non-zero IV start
  means offsetting every chunk index and the IV fix-up; it is a contained change
  but touches all four clients' plans, and was not attempted without budget to
  verify it broadly.
- **Column-strided stencils remain correctly rejected** by `vector_affine`.
- Three test expectations were updated to the new reason strings
  (`expect-exactly-one-contiguous-store`, `contiguous-store`); no assertion was
  removed.

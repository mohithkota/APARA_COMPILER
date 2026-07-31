# R4.4 Delivery Report — Automatic Packed GEMM Vectorization

**Milestone:** R4.4 (packed GEMM, i-k-j, over 1-D packed arrays).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-31

> **GEMM is AXPY over a row.** No second vector lowering was written: the inner
> j loop is exactly `Y[j] += a*X[j]` with Y = C's i-th row and X = B's k-th row,
> so R4.4 reuses R4.3's planner, scalar loader and chunk body verbatim and adds
> only row-aware addressing. `vector_pipeline.py` is untouched.

---

## 1. Files
| File | |
|---|---|
| `gemm_lowering.py` | **NEW.** `plan_gemm` (reuses `plan_axpy` for the whole structural match), `clone_offset`, `lower_gemm`. |
| `gemm_vectorizer.py` | **NEW.** `GemmTransform` — owns the `'saxpy'` kind, chains GEMM → AXPY → elementwise. |
| `gemm_corpus.py`, `_r4_4_test.py`, `R4_4_DELIVERY.md` | **NEW.** 14-kernel corpus, 48-check suite, this report. |
| `kernel_detector.py` | Its two ad-hoc affine tests fall back to `vector_affine` (see §2). |
| `vector_legality.py` | Additive lane-disjointness disproof (see §3). |
| `axpy_lowering.py` | `AxpyPlan` gains three row fields; behaviour unchanged. |
| `dot_vectorizer.py` | Production registers `GemmTransform`. |

## 2. Two upstream blockers had to be removed first
GEMM did **not** reach any client. Two modules still used the pre-R4.2.8 `iv_terms`
test, which only sees a **bare** `IV*const` offset:

- `kernel_detector._affine_access` and `_affine_store_src` classified the GEMM
  inner loop as **no kernel at all** (`C[i*N+j]`'s offset is a SUM), so legality
  rejected it with `no-recognised-kernel` before any client ran.

Both now fall back to `vector_affine`. This **removes** a duplicate address
recognizer rather than adding one — exactly the spec's "use ONLY vector_affine".
The fallback is strictly additive: everything the old test accepted still passes it
first, and R4.0–R4.3 suites are unchanged.

## 3. The one deviation from "reuse without modification"
`vector_legality._aliasing_ok` rejected GEMM with `unproven-aliasing`. Diagnosed
precisely: R2.2's SIV rule proves distinctness for a bare `IV*const` offset — which
is why AXPY passes — but a 2-D-indexed offset is a SUM and falls back to a generic
`('computed',)` may-alias edge between the C store and the C/B loads.

The facts needed to disprove it already exist in `vector_affine`; R2.2 simply
predates it. A **purely additive** disproof was added — it never creates an edge,
only excuses one R2.2 could not analyse:

- different stack slots → distinct objects, cannot alias;
- same slot **and the same offset temp**, both CONTIGUOUS → the two accesses share
  one address, so they collide only *within* an iteration; distinct iterations
  touch distinct elements.

Anything else still rejects. This is the only change to a module on the mandatory
reuse list, and it is reported here rather than buried.

## 4. Lowering: reuse, not duplication
`axpy_lowering._chunk` was already parameterised by three access emitters so one
body could serve the unrolled and compact realisations. GEMM passes **row-aware
emitters** and reuses everything else:

```
    plan_axpy      the entire structural match
    _load_scalar   materialising the invariant coefficient once
    _chunk         X * $replicate(a) + Y -> Y      <- the body, verbatim
```
`gemm_lowering.py` emits **no `IRVecArith` of its own** (asserted by test).

**The one thing GEMM adds is a row base.** R4.3 addresses a chunk as
`slot + chunk*lanes*elem_bytes`, which assumes the invariant part of the offset is
zero; for `C[i*N+j]` it is `i*N`, and ignoring it would read and write row 0 every
time. Rather than reconstruct the base arithmetically, `clone_offset` **re-emits
the loop's own address computation** with the induction variable substituted (a
`Const` for the unrolled realisation, or re-loading the IV slot for the compact
one). The compiler already computes the right address, so the clone is correct by
construction for any affine index R4.2.8 accepts — not just `i*N + j`.

## 5. Why other orderings are rejected without a special check
In i-j-k the innermost loop is k, where `B[k*N+j]` steps by a whole row.
`vector_affine` reports STRIDED (coeff = N·elem_bytes ≠ elem_bytes), so the kernel
is never recognised as contiguous. **The ordering requirement falls out of the
affine analysis** rather than being pattern-matched separately — verified: i-j-k is
declined as `not-recognised`, and its IR is byte-identical.

## 6. Results
```
  kernel              via   realisation     bundles     dyn instrs   val
  vi8  square 16^3    gemm  unrolled       54 -> 44     528 -> 34    OK
  vi8  square 8x8x16  gemm  unrolled       41 -> 35     528 -> 34    OK
  vi8  rect 8x8x32    gemm  unrolled       54 -> 54    1056 -> 66    OK
  vi8  rect 4x8x24    gemm  unrolled       41 -> 38     792 -> 50    OK
  vi8  odd N=20       gemm  unrolled       41 -> 46     660 -> 166   OK
  vi8  odd N=17       gemm  unrolled       41 -> 46     561 -> 67    OK
  vi16 8x8x16         gemm  unrolled       55 -> 56     560 -> 74    OK
  vi16 rect 4x8x32    gemm  compact        56 -> 58    1120 -> 275   OK
  vi16 odd N=30       gemm  compact        54 -> 72    1050 -> 311   OK
  vi32 8x8x8          gemm  unrolled       55 -> 56     280 -> 74    OK
  vi32 N=12           gemm  compact        43 -> 43     420 -> 207   OK
  REJECT i-j-k / unpacked / small-inner-trip        scalar (correct)

  vectorized 11/11 expected · mismatches 0 · rollbacks 0
  bundles 535 -> 548        code 49282 -> 61852 chars
  dynamic instructions 7555 -> 1358   (-82.0%)
  564 ms/kernel
  vs R4.3 (GEMM planner disabled): 0/14 -> 11/14
  Full corpus: 124 programs, scalar byte-identical 124/124
```

## 7. Success criteria — honest scoring
1. Automatic recognition of packed GEMM ✅ 11/11.
2. Reuse of existing vector infrastructure ✅ one deviation, declared in §3.
3. No duplicated lowering logic ✅ asserted by test — no `IRVecArith` in
   `gemm_lowering.py`.
4. 100% differential correctness ✅ 0 mismatches.
5. Automatic rollback ✅ 0 needed; all rejections happen at match/legality with
   specific reasons and byte-identical IR.
6. **Reduced bundle count ❌ NOT MET in aggregate: 535 → 548 (+2.4%).**
   Square/rectangular exact-multiple GEMMs — the main case — **do** shrink
   (16³: 54→44, −19%; 8×8×16: 41→35, −15%; 4×8×24: 41→38). The growth is
   concentrated in remainder shapes, dominated by vi16 N=30 alone (54→72, +18).
   Reporting this as a partial pass rather than claiming the criterion.
7. Reduced dynamic instructions ✅ −82.0%.
8. No regressions ✅ 124/124 corpus identical; all suites and all five corpora pass.

## 8. Honest notes / limitations
- **Bundle count is the weak axis** (§7.6). The cause is the one carried over from
  R4.2.6: a remainder keeps the scalar tail loop, and `PeelTemplate` still cannot
  express this kernel family. Remainder peeling for the AXPY/GEMM family remains
  the single highest-value follow-on.
- **Only the innermost j loop is vectorized**; i and k stay scalar, as specified.
- **The scalar must be hoisted by the source** (`s = A[i*N+k]` before the j loop).
  A GEMM writing `A[i*N+k]` inline in the inner loop is still recognised — the
  access is INVARIANT — but was not made a corpus case.
- **vi8 needs inner trip ≥ 16** (8 lanes × the trip ≥ 2·lanes profitability rule),
  so an 8×8×8 vi8 GEMM is correctly declined as unprofitable.
- **Attribution change:** the `'saxpy'` kind is now owned by `GemmTransform`, which
  chains GEMM → AXPY → elementwise, so those kernels report `via=gemm`. Four R4.3
  assertions were relaxed to accept either name; the transformations applied are
  unchanged.
- Nothing required new IR, new vector instructions, gather, frontend layout changes
  or backend redesign.

## 9. Test summary
```
_r4_4_test.py ........................ ALL R4.4 UNIT TESTS PASS  (48/48 checks)
_r4_3 / _r4_2_8 / _r4_2_6 / _r4_2_5 / _r4_2 / _r4_1 / _r4_0 ... PASS
_r3_1 / _r3_2 ........................ PASS
gemm / axpy / compact / elementwise / dot / affine corpora .... PASS (6/6)
pipeline_crosscheck.py ............... 124/124 identical
```

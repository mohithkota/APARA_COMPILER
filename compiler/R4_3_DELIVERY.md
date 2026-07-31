# R4.3 Delivery Report — Automatic AXPY Vectorization

**Milestone:** R4.3 (the first production client of `vector_affine`).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-31

> Not matrix multiplication (shown impossible on this architecture — see the R4.3
> investigation note in STATUS.md). AXPY instead: the first transformation whose
> every access decision is delegated to the R4.2.8 affine analysis.
> `vector_pipeline.py`, `vector_affine.py`, `vector_legality`,
> `vector_profitability`, `vector_validation`, the scheduler, bundler and backend
> are all consumed UNMODIFIED.

---

## 1. Files
| File | Purpose |
|---|---|
| `axpy_lowering.py` | **NEW.** `plan_axpy` (pattern match) + `lower_axpy` (lowering). All access classification via `vector_affine`. |
| `axpy_vectorizer.py` | **NEW.** `AxpyTransform` — the pipeline client. |
| `axpy_corpus.py`, `_r4_3_test.py`, `R4_3_DELIVERY.md` | **NEW.** Evaluation, 59-check suite, this report. |
| `elementwise_vectorizer.py` | `kinds` drops `'saxpy'` (now AXPY's); its standalone entry point registers both clients so its contract is unchanged. |
| `dot_vectorizer.py` | `vectorize_all_module` registers `AxpyTransform`. |

## 2. Lowering
```
    per chunk:   packed load X
                 $v *  with $replicate(a)      <- a is src2; $replicate broadcasts src2
                 packed load Y
                 $v +
                 packed store Y
```
No new vector instruction. The invariant coefficient is materialised ONCE ahead of
the vector body (a slot load, or an `IRAssign` for a literal). Both the unrolled
and the **compact vector-loop** realisations are built and the R4.2.6
post-optimizer probe keeps the smaller — compact wins on 8 of 10 kernels.

## 3. `vector_affine` is the only affine analysis
`plan_axpy` never reads `desc.iv_terms`. It asks `classify_access` for every
access and dispatches on the answer: the store and the `Y`/`X` reloads must be
CONTIGUOUS, the coefficient must be INVARIANT. A test asserts `.iv_terms` does not
appear in the module.

**One correction the implementation forced.** `classify_access` describes the
ADDRESS pattern, not the loaded VALUE. A load of the IV's own slot sits at a
constant offset and therefore looks address-invariant, while its value changes
every iteration — so `Y[i] += i*X[i]` initially matched and was only caught by the
differential. The coefficient test now additionally requires `ctx.varies(...)` to
be false, and that kernel is rejected at match time. **This is a usage lesson, not
a limitation of `vector_affine`** — the analysis exposes both facts; the client
was asking the wrong one.

## 4. A design consequence worth recording
`kernel_detector` labels ANY loop whose stored value contains a multiply as
`'saxpy'` — both a real AXPY and R4.2's elementwise multiply `C[i]=A[i]*B[i]`. The
pipeline dispatches one client per kind, so only one can own `'saxpy'`. Rather
than modify the pipeline or coarsen the detector, `AxpyTransform` owns `'saxpy'`
and falls back to the R4.2 elementwise planner/lowering when its own match fails.
Every shape R4.2 vectorized still vectorizes, by the same code — verified by the
R4.2 suite and corpus passing unchanged.

## 5. Results
```
  AXPY suite (13 cases)          via     realisation    bundles    dyn ops
    AXPY vi8  N=64 / N=128       axpy    compact        20->20   1600->155 / 3200->307
    AXPY vu8  N=64               axpy    compact        20->20   1600->155
    AXPY vi16 N=32 / N=64        axpy    compact        20->20    832->163 / 1664->323
    AXPY vi32 N=16               axpy    compact        20->20    416->163
    AXPY const coeff / X*a       axpy    compact        20->20   1472->155 / 1600->155
    AXPY rem N=20 / N=30(vi16)   axpy    unrolled       20->22    500->118 / 20->30 780->110
    REJECT unpacked / varying a / small trip      scalar (correct)

    vectorized 10/10 expected · mismatches 0 · rollbacks 0
    bundles  200 -> 212      code 14131 -> 17110 chars
    dynamic operations 13664 -> 1804   (-86.8%)
    104 ms/kernel

  Versus R4.2.6 (same kernels, AXPY client removed):  0/13 -> 10/13   (+10)
  Full corpus: 124 programs, scalar byte-identical 124/124
```
**vi32 is supported, not rejected.** The spec anticipated a possible rejection;
`$v` covers vi32 (only `$dot` lacks a 32-bit form), so AXPY vi32 vectorizes at 2
lanes.

## 6. Success criteria
1. Automatic AXPY recognition ✅ 10/10, both operand orders, literal coefficients.
2. `vector_affine` the only affine analysis ✅ asserted by test.
3. Correct `$replicate` lowering ✅ `$replicate` present in emitted mcode.
4. 100% differential validation ✅ 0 mismatches.
5. Automatic rollback ✅ unpacked / varying-coefficient / small-trip / unknown-trip
   all rejected with specific reasons, IR byte-identical.
6. Reduced dynamic operations ✅ −86.8%.
7. No regressions outside accepted kernels ✅ 124/124 corpus identical;
   R4.0–R4.2.8 and R3.1/R3.2 suites and all corpora pass.

## 7. Test summary
```
_r4_3_test.py ........................ ALL R4.3 UNIT TESTS PASS  (59/59 checks)
_r4_2_8 / _r4_2_6 / _r4_2_5 / _r4_2 / _r4_1 / _r4_0 ... PASS (unchanged)
_r3_1 / _r3_2 ........................ PASS (unchanged)
axpy_corpus / vector_compact_corpus / vector_elementwise_corpus ... PASS
pipeline_crosscheck.py ............... 124/124 identical
```

## 8. Honest notes / limitations
- **Static size grows on the two remainder kernels** (20→22 and 20→30 bundles);
  the eight exact-multiple kernels stay flat at 20→20 thanks to the compact loop.
  Code size overall 14131→17110 chars.
- **No peeled remainder for AXPY.** `PeelTemplate` cannot express
  `Y[i] += a*X[i]` (two loads plus an invariant scalar and two operations), and
  extending it speculatively was out of scope; with a remainder the scalar tail
  loop is kept, exactly as R4.1/R4.2 do. This is the obvious follow-on.
- **`a` is reloaded per compact-loop iteration** — it is materialised once per
  vector body, and the body is the loop. LICM may hoist it; not verified here.
- **No `vector_affine` limitation blocked AXPY.** The one issue encountered (§3)
  was the client asking about the address when it needed the value; both facts
  were already available.
- Validation remains the packed IR oracle (no hardware simulation, per policy).

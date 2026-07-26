# R4.1 Delivery Report — Automatic Dot-Product & Sum-Reduction Vectorization

**Milestone:** R4.1 (the FIRST production vector transformation — dot product and
sum reduction only, built on the R4.0 infrastructure).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-27

> The first pass that EMITS vector instructions. Only two kernel classes; no
> general vectorization, no matmul/convolution/elementwise. Every committed kernel
> is proven behaviour-identical by the R4.0 packed differential oracle and compiles
> spill-free through the existing backend; otherwise the loop stays scalar. Scalar
> compilation is byte-identical whenever vectorization is rejected. No scheduler,
> bundler, register allocator, or backend is modified. `APARA_NO_VECTORIZE` disables it.

---

## 1. Files added
| File | Purpose |
|---|---|
| `vector_lowering.py` | Packed-array lowering: packed 64-bit loads + `$dot`/`$dot $accumulate` (dot) or `$vreduce` + scalar accumulate (reduction) + a scalar remainder loop. `PackedVectorInterp` + `differential_packed` (the packed-aware oracle, extends VectorInterp by subclass). |
| `dot_vectorizer.py` | The shared driver (detect → legality → profitability → lower → differential → backend/dynamic-profit → commit/rollback) + the dot entry point. `VectorizeStats`, `VectorizeReport`. |
| `reduction_vectorizer.py` | The sum-reduction specialization of the same driver. |
| `vectorize_corpus.py` | Corpus evaluation (dedicated packed-kernel suite + full-corpus no-regression proof). |
| `_r4_1_test.py` | Unit suite (28 checks). |
| `R4_1_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `compiler.py` | `compile_c_to_mcode()`: a guarded vectorization step that runs FIRST (on `_ir0`), then the existing scalar optimizer / scheduler / bundler / backend process the result. `APARA_NO_VECTORIZE` kill-switch; any error keeps the scalar IR. |

R4.0 (`vector_capability`/`_db`/`legality`/`profitability`/`kernel_detector`/
`vector_validation`) is reused unmodified; `vector_validation.VectorInterp` is
EXTENDED by subclass (`PackedVectorInterp`), not modified.

## 3. The APARA packing reality (why this shape)
Determined from the backend: ordinary C arrays are stored one element per 8-byte
DMEM word (stride 8), so consecutive elements are **not** contiguous and cannot be
gathered by a single load. Only arrays declared with the packed markers
(`vu8_t/vi8_t/vu16_t/vi16_t/vu32_t/vi32_t`) are stored tightly packed, so
`lanes = 8/elem_bytes` consecutive elements fill one 64-bit word — exactly what
`$dot`/`$vreduce` consume. **R4.1 therefore vectorizes only packed arrays**, which
`vector_legality` enforces (unpacked stride is rejected).

## 4. Lowering
```
dot:        acc += A[i]*B[i]   ->   for each chunk c: packed-load A[c], B[c];
                                    $dot $accumulate into a register chain;
                                    store acc;  scalar remainder loop
reduction:  acc += A[i]        ->   for each chunk c: packed-load A[c];
                                    $vreduce (+); scalar accumulate;
                                    store acc;  scalar remainder loop
```
The known trip count `N` gives `chunks = N // lanes` (unrolled straight-line) and a
scalar tail of `N % lanes` iterations. When `N % lanes == 0` the scalar loop is
dropped entirely; the IV slot is set to `N` so memory is identical to the scalar
form. A packed 64-bit load is a normal `IRLoad` (`elem_bytes=8`) that on hardware
reads the contiguous packed bytes; the oracle models the gather.

## 5. Validation & rollback (100%)
Every candidate must pass, in order: **legality** (packed, supported+reliable type,
ISA-supported op, no aliasing), **profitability** (lanes ≥ 2, trip ≥ 2·lanes),
**lowering**, the **packed differential oracle** (`differential_packed`, 6 seeds
over the full byte/half-word range so sign/zero-extension and overflow divergence
surface), **spill-free compile**, and a **dynamic-operation reduction**. Any
failure rolls the loop back to scalar. The oracle is load-bearing: e.g. a dot with
a 32-bit accumulator (which the vector form over-accumulates before wrapping) is
**automatically rolled back** — caught, not mis-compiled.

## 6. Production integration
Vectorization runs first, so the vectorized IR flows through the existing scalar
optimizer, scheduler, bundler, register allocator, and backend unchanged. A
function with no committed kernel is byte-identical to today. Verified: real
`$dot`/`$vreduce` instructions appear in the production mcode.

## 7. Corpus results
```
  Dedicated packed-kernel suite (10 kernels)
    vectorized                    : 7/10   (dot vi8/vu8/vi16, red vi8/vi16/vi32, +remainder)
    behaviour mismatches          : 0      (100% differential validation)
    dynamic operations            : 5892 -> 326   (-94%)
    rejected (correctly)          : vi32-dot (no ISA), narrow-acc (rollback), unpacked
    real $dot/$vreduce in mcode    : yes (2-8 per kernel)

  Full corpus (124 programs)
    vectorized (packed kernels)    : 0   (the general corpus has no packed arrays)
    scalar & byte-identical (on/off): 124/124   (NO regression)
```

## 8. Success criteria — met
1. **Automatic recognition** — dot-product & sum-reduction over packed arrays.
2. **Correct lowering** — `$dot`/`$dot $accumulate`, `$vreduce`; real vector ops in mcode.
3. **Scalar remainder** — for `N % lanes ≠ 0`, validated.
4. **Automatic rollback** — narrow accumulator, unsupported ISA, unpacked arrays, aliasing.
5. **100% differential validation** — 0 mismatches on all committed kernels.
6. **Reduced (dynamic) operation count on accepted kernels** — 5892 → 326 (−94%);
   static code can grow (the code-size-for-speed trade of unrolling the chunks).
7. **No regressions** — 124/124 scalar programs byte-identical; `pipeline_crosscheck`
   124/124; R2.5–R4.0 suites all pass.

## 9. Test summary
```
_r4_1_test.py ........................ ALL R4.1 UNIT TESTS PASS   (28/28 checks)
vectorize_corpus.py .................. PASS (100% differential, scalar unchanged)
pipeline_crosscheck.py ............... 124/124 identical
R2.5-R4.0 unit suites ................ PASS
```

## 10. Honest notes / limitations
- **Packed arrays only.** The APARA memory model makes ordinary (unpacked) arrays
  un-vectorizable without an expensive gather; only the packed typedef markers are
  handled. General-corpus coverage is therefore ~0 (few packed kernels); the value
  is demonstrated on the dedicated packed-kernel suite.
- **Wide accumulator required.** A narrow (32-bit) accumulator diverges from the
  vector form on overflow and is rolled back by the oracle; only 64-bit-accumulator
  kernels vectorize. (Correct, if conservative.)
- **Static size may grow** (chunks are unrolled); the win is dynamic (−94% ops).
  Large `N` would benefit from a compact vector loop (future refinement).
- **Validation is the packed IR oracle** (no hardware simulation, per policy),
  modelling the hardware semantics from `golden_stubs.h`; a simulator-backed gate
  remains available.
- Not done (by mandate): matrix multiplication (R4.3), convolution, elementwise
  (R4.2), general vectorization (R4.4).

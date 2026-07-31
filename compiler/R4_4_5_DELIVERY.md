# R4.4.5 Delivery Report — Generalized Vector Remainder Peeling

**Milestone:** R4.4.5 (one reusable remainder framework for every vector client).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-31

> Infrastructure only. No new IR, no new vector instructions, no new backend
> functionality, no new legality or profitability analysis. `vector_pipeline.py`
> untouched.

---

## 1. Problem
When `trip % lanes != 0` a vectorized kernel keeps the original scalar loop for the
tail — a full skeleton (compare, branch, IV load/add/store, label boundaries the
bundler cannot pack across). R4.2.7 introduced peeling, but its `PeelTemplate`
could only express `dest = f(loads)` with **every load at the same element index**.
That covered dot, reduction and elementwise but not

```
    Y[i] += a * X[i]
```

because `a` is an **invariant scalar** (not indexed) and `Y[i]` is **read and
written**. AXPY and GEMM therefore kept scalar tails, which was the sole cause of
the bundle growth reported in R4.3 (+6%) and R4.4 (+2.4%).

## 2. Design — declarative, one emitter
`PeelTemplate` now describes an update instead of hard-coding one shape:

```
    operands  [PeelArray | PeelScalar | PeelConst], in the ORIGINAL order
    op        (opcode, unsigned) combining operands[0] and operands[1], or None
    dest      PeelArray (indexed store) or PeelScalar (accumulator slot)
    dest_op   None -> dest = value ;  (op, uns) -> dest = dest <op> value
```

That expresses every required form with no branching on kernel kind:

| update | operands | op | dest | dest_op |
|---|---|---|---|---|
| `Y[i] = X[i]` | `[array X]` | — | array Y | — |
| `Y[i] = X[i] + Z[i]` | `[array X, array Z]` | `+` | array Y | — |
| `Y[i] += X[i]` | `[array X]` | — | array Y | `+` |
| `Y[i] += a*X[i]` | `[scalar a, array X]` | `*` | array Y | `+` |
| `Y[i] += 3*X[i]` | `[const 3, array X]` | `*` | array Y | `+` |
| `s += A[i]*B[i]` | `[array A, array B]` | `*` | slot s | `+` |

**Why declarative rather than a per-client emitter callback.** The tail must
reproduce the original loop's integer promotion and sub-word truncation exactly.
Clients record the ORIGINAL instructions' `elem_bytes`, `unsigned` flags and
opcodes; the framework replays them. Letting each client emit its own tail would
re-open the class of bug that made R4.1's narrow-accumulator dot diverge — and
would be the duplicated scalar lowering this milestone exists to prevent.

**The one genuinely per-client concern is a callback.** How an array element's
ADDRESS is formed differs: GEMM addresses a row (`C[i*N+j]`) while every other
client addresses from the array base. `PeelArray.offset_at(idx)` overrides address
formation; GEMM supplies `clone_offset(..., Const(idx))`, reusing R4.4's machinery.
The default covers every other client.

## 3. Result: exactly one framework
- `build_peeled_tail` is defined once, in `vector_remainder_peel`.
- **No `AxpyPeeler`, `GemmPeeler` or any `*Peeler` class exists** — asserted by a
  test that greps every module.
- All four clients (`vector_lowering`, `vector_elementwise_lowering`,
  `axpy_lowering`, `gemm_lowering`) build a template and **none of them calls
  `build_peeled_tail` or emits tail code** — also asserted.
- Peeled variants are offered as ordinary candidates to the R4.2.6 selector, so
  they inherit the post-optimizer size gate, the acceptance margin, validation and
  rollback unchanged.

## 4. Measurements (post-optimizer bundles, vs R4.4)
```
  GEMM corpus (14 kernels)          R4.4            R4.4.5
    vectorized                      11/11           11/11
    mismatches / rollbacks          0 / 0           0 / 0
    bundles                         535 -> 548      535 -> 541     (+13 -> +6)
    code size                     49282 -> 61852  49282 -> 61423   (-429 chars)
    dynamic instructions           7555 -> 1358    7555 -> 1345    (-82.2%)

  AXPY corpus (13 kernels)          R4.3            R4.4.5
    vectorized                      10/10           10/10
    mismatches / rollbacks          0 / 0           0 / 0
    bundles                         200 -> 212      200 -> 206     (+12 -> +6)
    code size                     14131 -> 17110  14131 -> 15830   (-1280 chars)
    dynamic ops                   13664 -> 1804   13664 -> 1859

  Highlighted regression cases (pre-optimizer bundles, realisation chosen)
    AXPY vi16 N=30      compact+peeled     dyn 780 -> 165
    GEMM vi8  N=17      unrolled+peeled    dyn 561 ->  54   (R4.4: 561 -> 67)
    GEMM vi16 N=30      compact            (peel offered, not chosen)
```

## 5. Success criteria — honest scoring
1. Exactly one reusable remainder framework ✅ asserted by test.
2. No client-specific peeler ✅ no `*Peeler` class exists.
3. AXPY uses the new framework ✅ `compact+peeled` chosen on vi16 N=30.
4. GEMM uses the new framework ✅ `unrolled+peeled` chosen on vi8 N=17.
5. Differential validation clean ✅ 0 mismatches across both corpora.
6. **Bundle-count regression REDUCED, not eliminated** ⚠️ GEMM +2.4% → +1.1%,
   AXPY +6.0% → +3.0%. Roughly halved on both. Scoring this as partial.
7. No regressions ✅ 124/124 corpus identical; 11 suites and all 6 corpora pass.

## 6. Honest notes / limitations
- **Peeling is offered, not forced.** It competes as a candidate under the R4.2.6
  post-optimizer probe and the 10% acceptance margin, so a kernel whose peel does
  not clear the margin keeps its scalar tail — e.g. GEMM vi16 N=30 still chooses
  plain `compact`. That is the selector working as designed (peeling trades static
  size for dynamic speed, see R4.2.6), and it is why the regression is halved
  rather than eliminated.
- **AXPY dynamic ops rose slightly** (1804 → 1859) because several kernels changed
  realisation; static size fell by 1280 characters in exchange.
- A defect was found and fixed during bring-up: the first GEMM wiring silently
  failed to install the row-aware template, so GEMM peeled with base-relative
  addresses. **The differential caught it and rolled back** (no wrong code was ever
  committed); the fix installs the override with an assertion.
- The framework handles at most two operands and one combining operation. An
  expression tree (`d[i] = a[i]+b[i]+c[i]`) would need the `op` field to become a
  small tree; the descriptors are shaped to allow that without touching clients.

## 7. Test summary
```
_r4_4_5_test.py ...................... ALL R4.4.5 UNIT TESTS PASS  (58/58 checks)
_r4_4 / _r4_3 / _r4_2_8 / _r4_2_6 / _r4_2_5 / _r4_2 / _r4_1 / _r4_0 ... PASS
_r3_1 / _r3_2 ........................ PASS
gemm / axpy / compact / elementwise / dot / affine corpora .... PASS (6/6)
pipeline_crosscheck.py ............... 124/124 identical
```

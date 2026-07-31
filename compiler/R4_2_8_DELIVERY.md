# R4.2.8 Delivery Report — Affine Access Recognition

**Milestone:** R4.2.8 (the affine infrastructure the vector roadmap requires —
determined by survey, then built to exactly that envelope).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-31

> ANALYSIS ONLY, in the R3.0/R4.0 mould: mutates nothing, emits nothing, and
> generated code is byte-identical 124/124. Nothing is wired into the vector
> clients yet — this milestone establishes and validates the capability so the
> AXPY/GEMM work can consume it without discovering the analysis is too weak
> halfway through.

---

## 1. The question, and the answer

**Asked:** is `invariant_base + IV * constant` sufficient for the whole roadmap?

**Answer: NO.** Four independent reasons, each measured on the IR the front end
actually emits, not reasoned about abstractly.

### 1.1 For `elem_bytes >= 2` the scale is applied AFTER the index sum
`C[i*8+j]` on `vi16_t` emits `(i*8 + j) * 2` — the shape is
`(invariant + IV) * const`, **not** `invariant + IV*const`. A matcher for the
latter matches **none** of the vi16/vi32 kernels. It would appear to work on vi8
(where the ×1 scale is elided) and then silently fail to generalize — the worst
possible failure mode.

### 1.2 Operand order varies with loop order
A 1-D convolution emits `(invariant + IV*1)` when the tap loop is innermost and
`(IV*1 + invariant)` when the output loop is. Both orders occur in the roadmap.

### 1.3 Expressions nest
A 2-D convolution's `in[(i+r)*8 + (j+s)]` hides the IV **two levels down** inside
`(j+s)`. One level of pattern matching finds nothing.

### 1.4 Invariance is a VALUE property, not a syntactic one
This is the one that actually broke the first prototype. The "invariant"
subexpressions (`i*8`) are **recomputed inside the innermost body** — LICM has not
run at vectorization time — so they *look* local. Deciding invariance by position
rejected **every** 2-D kernel. It must be decided by asking whether the slot is
written in the loop, which is the M2 memory-effects question. (Exactly the class of
bug behind R1.4's "the bound must be VALUE-invariant, not merely
temp-independent".)

### 1.5 And `coeff == 0` must be a first-class answer
Identifying the *loop-invariant* operand is precisely what recognises an AXPY (the
`$replicate` scalar) or a row-dot GEMM (the accumulator). A yes/no "is it affine"
predicate cannot express this.

## 2. The minimum sufficient extension
A **bounded affine normalizer** resolving an offset to

```
        offset  ==  coeff * IV  +  invariant          (coeff a compile-time constant)
```

over `{+, -, *}` with constant folding, recursively, against **one** induction
variable. Classification follows directly:

```
    coeff == elem_bytes  -> CONTIGUOUS   packed lane-parallel access
    coeff == 0           -> INVARIANT    scalar operand
    coeff == other const -> STRIDED      APARA cannot gather it (stride reported)
    unresolvable         -> UNKNOWN      rejected with a reason
```

This **subsumes** `invariant + IV*const` — that is just one of the shapes it folds.

## 3. Why this is not over-generalizing
Deliberately unsupported, and rejected with a reason:

- **Multiple varying induction variables.** Not needed *by construction*:
  vectorization always targets the **innermost** loop, so every enclosing loop's
  IV is invariant with respect to it. `IV1 + IV2` with both varying cannot arise.
  This single observation is what keeps the analysis small.
- **Symbolic coefficients.** `B[k*N+j]` with the k-loop innermost and runtime `N`
  resolves to a non-constant coefficient and is rejected — correctly, since that
  *is* the column-strided access APARA cannot perform.
- **Division, modulo, shifts, min/max, data-dependent (gather) indices.**
- No SCEV, no chains of recurrences, no polyhedral machinery, no dependence
  testing (R2.1/R2.2 already own that).

## 4. A soundness bug found and fixed during validation
The first implementation classified `a[idx[k]]` — a **gather** — as INVARIANT,
because the invariance test only checked the load's *base slot* (`idx`, never
written in the loop) and ignored whether its *offset* moved with the IV. That
would have handed a gather to a vectorizer as a loop-invariant scalar. Fixed: a
load also varies when its offset expression varies. The gather now resolves to
UNKNOWN, and there is a regression test asserting it is neither contiguous **nor**
invariant.

## 5. Results
```
  Roadmap kernel shapes            contig  invar  strided  unknown  verdict
    R4.1  dot vi8                       2      0        0        0  RESOLVED
    R4.1  reduction vi16                1      0        0        0  RESOLVED
    R4.2  elementwise vi8               3      0        0        0  RESOLVED
    R4.2  elementwise vi16              3      0        0        0  RESOLVED
    PLAN  AXPY (i-k-j) vi8              3      1        0        0  RESOLVED
    PLAN  AXPY (i-k-j) vi16             3      1        0        0  RESOLVED
    PLAN  GEMM row-dot (Bt)             2      2        0        0  RESOLVED
    PLAN  conv1d inner-taps             2      2        0        0  RESOLVED
    PLAN  conv1d inner-out              3      1        0        0  RESOLVED
    PLAN  conv2d inner-j                3      1        0        0  RESOLVED
    REJECT column-strided               0      0        1        0  rejected
    REJECT symbolic stride              0      0        0        1  rejected
    REJECT gather                       0      0        0        1  rejected
    REJECT unpacked int                 0      4        2        0  rejected

  Agreement with today's recognizer : 4 currently-vectorized kernels, 0 disagreements
  Full corpus                       : 124 programs, 65 innermost loops
                                      47 loops fully resolved
                                      accesses 15 contiguous / 493 invariant
                                               26 strided / 3 unknown
                                      generated code identical 124/124
```

Every planned transformation resolves; every access APARA cannot perform is
rejected **with the stride named** rather than dismissed as "unrecognised".

## 6. Success criteria
1. **Survey of every existing and planned transformation** ✅ §1, measured on
   emitted IR.
2. **Minimum extension identified and justified** ✅ §2–3; the narrower proposal
   is shown insufficient with four concrete counter-examples.
3. **Roadmap fully covered** ✅ 10/10 kernel shapes resolved.
4. **Not over-generalized** ✅ 4/4 out-of-envelope forms rejected with reasons.
5. **No regression to existing recognition** ✅ 0 disagreements.
6. **Analysis only** ✅ 124/124 generated code identical.

## 7. Test summary
```
_r4_2_8_test.py ...................... ALL R4.2.8 UNIT TESTS PASS  (39/39 checks)
affine_corpus.py ..................... PASS
_r4_2_6 / _r4_2_5 / _r4_2 / _r4_1 / _r4_0 ... PASS (unchanged)
_r3_1 / _r3_2 ........................ PASS (unchanged)
pipeline_crosscheck.py ............... 124/124 identical
```

## 8. Honest notes / limitations
- **Nothing is wired in yet.** The vector clients still use their own
  `_packed_array_access`. Adopting this normalizer would *widen* what they accept
  (row-wise kernels), which is a behaviour change and belongs to the milestone
  that needs it, not to this one.
- **The corpus barely exercises it**: 15 contiguous accesses across 124 programs,
  because the corpus has almost no packed arrays. The roadmap table, not the
  corpus, is the evidence that matters here.
- **Conservative on multi-def temps.** A temp with no single definition is treated
  as invariant only when `varies()` can prove it; otherwise the access is
  UNKNOWN. Sound, occasionally pessimistic.
- **The `varies()` walk is not cached** across accesses within a loop. Fine at
  current scale (65 loops, milliseconds); would want memoizing if a client calls
  it per-iteration.
- Recursion is bounded at depth 16; deeper index arithmetic is rejected rather
  than explored.

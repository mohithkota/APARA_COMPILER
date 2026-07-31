# R4.5 Delivery Report — Expression Tree Vectorization

**Milestone:** R4.5 (reusable small-expression lowering infrastructure).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-31

> Infrastructure. No new IR, no new vector instructions, no backend changes, no
> new legality or profitability analysis. `vector_pipeline.py` untouched.

---

## 1. Problem
Every client described its computation as at most `operand OP operand`, and
`PeelTemplate` mirrored that. Enough for assignment, AXPY, GEMM and simple
elementwise kernels; not enough for `a+b+c`, `a*b+c`, `(a+b)*c`, fused
expressions, convolution or a general vectorizer.

## 2. Architecture — one representation, two evaluators
`expression_tree.py` — immutable, kernel-independent nodes:

```
    Const(value)                      ArrayRef(slot, elem_bytes, unsigned, offset_at)
    ScalarRef(slot, elem_bytes, ...)  BinOp(op, left, right, unsigned)
```
plus `walk`, `arrays`, `depth`, `is_invariant`, and `map_arrays` (rebuilds rather
than mutates — GEMM uses it to swap in row addressing). `build_expression`
recognises a tree from IR using **`vector_affine` and nothing else**: a CONTIGUOUS
access becomes an `ArrayRef`, an invariant slot load a `ScalarRef`, a literal a
`Const`; strided accesses, gathers, unsupported opcodes and over-deep trees are
rejected with a reason. There are no kernel-specific subclasses.

`expression_lowering.py` — **two evaluators over the same tree**:

```
    lower_vector(tree, vtype, load_array)  -> packed loads + `$v` ops for one chunk
    lower_scalar(tree, idx)                -> ordinary scalar IR for one element
```

`lower_scalar` is what the remainder framework uses, so a client that describes
its computation **once** gets both the vector body and the peeled tail from the
same description. There is no second, hand-written scalar lowering to drift.

**One ISA constraint is explicit rather than implicit.** `$replicate` broadcasts
**src2** only (codegen `_gen_IRVecArith`). A scalar on the left of a *commutative*
operator is commuted into src2; on the left of `-` it cannot be, so
`vector_feasible` **refuses** such a tree at match time instead of mis-emitting it.
Tested directly.

## 3. PeelTemplate now consumes trees
`PeelTemplate(expr, dest, dest_op)` where `expr` is a tree and `dest` is an
`ArrayRef` or `ScalarRef`. The R4.4.5 form
`PeelTemplate(operands=[...], op=..., dest=..., dest_op=...)` is still accepted and
converted internally, so **all four existing clients kept working unchanged** —
their suites pass untouched. `build_peeled_tail` now calls `lower_scalar`; it
contains no expression walking of its own.

## 4. Elementwise became the first tree-driven client
Its 1-or-2-operand matcher was replaced by `build_expression`, and both its vector
bodies (unrolled and compact) now call `lower_vector`. That is a deletion of
special-case code, not an addition, and it is what makes the new shapes work.

## 5. Results
```
  kernel                      result        bundles      dyn instrs   val
  a+b            (R4.4.5)     VECTORIZED   19 -> 20     704 -> 28     OK
  a*b            (R4.4.5)     VECTORIZED   19 -> 20     704 -> 28     OK
  a+b+c              NEW      VECTORIZED   20 -> 20     800 -> 87     OK
  a*b+c              NEW      VECTORIZED   20 -> 20     800 -> 87     OK
  a+b*c              NEW      VECTORIZED   20 -> 20     800 -> 87     OK
  (a+b)*c            NEW      VECTORIZED   20 -> 20     800 -> 87     OK
  a+b+c+d            NEW      VECTORIZED   21 -> 21     896 -> 99     OK
  a-b-c              NEW      VECTORIZED   20 -> 20     800 -> 87     OK
  3*a+b   const      NEW      VECTORIZED   20 -> 20     736 -> 83     OK
  a+b+c rem N=20     NEW      VECTORIZED   20 -> 22     500 -> 120    OK
  vi16 a*b+c         NEW      VECTORIZED   20 -> 20     832 -> 179    OK
  vi32 a+b+c         NEW      VECTORIZED   20 -> 20     416 -> 179    OK
  REJECT divide / shift       scalar (unsupported-operator)

  Coverage:  R4.4.5 (binary shapes only)  2/14   ->   R4.5  12/14
  Newly accepted (10): a+b+c, a*b+c, a+b*c, (a+b)*c, a+b+c+d, a-b-c,
                       3*a+b, a+b+c(rem), vi16 a*b+c, vi32 a+b+c
  Also newly accepted elsewhere: `c[i] = a[i]*3` (dyn 640 -> 24), which R4.2
  through R4.4.5 rejected as 'operand-not-a-temp'.

  mismatches 0 · rollbacks 0 · full corpus 124/124 scalar byte-identical
```

**On comparing totals.** The A/B totals (bundles, dynamic instructions, code size)
are *not* like-for-like, because R4.4.5 vectorizes 2 of these kernels and R4.5
vectorizes 12 — the sums cover different kernel sets. The meaningful figures are
**coverage 2 → 12** and the **per-kernel** dynamic reduction (typically ~−89%,
e.g. `a+b+c` 800 → 87) at **flat bundle count** (20 → 20). No new kernel grows
static size except the remainder case, which behaves as R4.4.5 established.

## 6. Success criteria
1. Expression lowering is reusable ✅ two evaluators, no kernel knowledge.
2. No client-specific tree walkers ✅ clients call `lower_vector`/`lower_scalar`;
   `build_peeled_tail` walks nothing itself.
3. Existing clients continue to work unchanged ✅ 12 suites pass; the legacy
   `PeelTemplate` form is still accepted.
4. PeelTemplate consumes expression trees ✅ tested with a nested tree.
5. Differential validation clean ✅ 0 mismatches everywhere.
6. No regressions ✅ 124/124 corpus identical; all 7 corpora pass; crosscheck
   124/124.

## 7. Honest notes / limitations
- **Only elementwise is tree-*driven*.** Dot, reduction, AXPY and GEMM consume
  trees through `PeelTemplate` but still build their vector bodies with their own
  (binary, already-minimal) emitters. Converting them would be churn without
  behaviour change, so it was not done. They are unaffected either way.
- **Depth is bounded at 4** (`expression_tree.MAX_DEPTH`) on purpose: this is small
  expression support, not a general vectorizer. Deeper trees are declined, never
  mis-lowered.
- **`scalar - vector` is refused**, not emitted, because `$replicate` broadcasts
  src2 only. Supporting it would need a new instruction, which the rules forbid.
- **Two test expectations were updated, not weakened**: `_r4_2_test.py` and
  `vector_elementwise_corpus.py` asserted that `c[i]=a[i]*3` and
  `d[i]=a[i]+b[i]+c[i]` are *rejected*. Both are now correctly vectorized with
  clean differentials, so those cases moved from the rejection list to an
  explicit "newly accepted since R4.5" check.
- A bug was caught during bring-up: `MAX_DEPTH` was bound as a default argument at
  import, so the A/B harness could not actually restrict depth and reported a
  meaningless "+0 coverage". Fixed to read at call time; the real A/B is 2 → 12.
- **Convolution is now mostly a client, not new lowering** — it needs kernel
  recognition and a reduction destination, both of which already exist.

## 8. Test summary
```
_r4_5_test.py ........................ ALL R4.5 UNIT TESTS PASS  (58/58 checks)
_r4_4_5 / _r4_4 / _r4_3 / _r4_2_8 / _r4_2_6 / _r4_2_5 / _r4_2 / _r4_1 / _r4_0 ... PASS
_r3_1 / _r3_2 ........................ PASS
expression / gemm / axpy / compact / elementwise / dot / affine corpora ... PASS (7/7)
pipeline_crosscheck.py ............... 124/124 identical
```

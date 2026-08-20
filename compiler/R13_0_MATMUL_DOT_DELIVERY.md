# R13.0 — Generic matmul → `$dot` vector lowering

Branch `feature/r13-matmul-dot`, off `main` (`b46aa63`). `r10-final`,
`r11-verified` and `r12.1-verified` untouched. Nothing pushed.

Phase 7 (TM×TK×TN loop tiling) is **deferred to R13.1/R14**: the compiler has no
tiling mechanism for candidates to plug into, and adding one is a separate loop
transformation. R13.0's configuration space is
**datatype × derived lanes × U∈{8,4,2,1} × compact/unrolled realisation**.

---

## 1. Original rejection cause

A hand-written `$dot` 16×16 matmul (**309 ticks, 1160 instrs, IPB 3.754, 46.9%
occupancy**) ran far faster than compiler output. The same algorithm in C — B
pre-transposed, `vu8_t`, inner loop as a dot product — compiled correctly but
emitted **zero** vector instructions.

The compiler already detected `kind='matmul'` (`kernel_detector.py:283`), mapped
it to the `$dot` capability (`vector_legality.py:44`), rated it profitable
(`~8.0x, instr -88%, util 100%`) and owned a working `$dot` emitter (R4.1). It
declined at `vector_lowering.py:217` with **`pattern:array-bases-not-extracted`**:
`plan_lowering` required every array offset to be a BARE IV term scaled by
`elem_bytes`, but a matmul offset is `invariant_row_base + IV*elem_bytes`.

**The missing capability was the access REPRESENTATION, not the instruction.**

## 2. Generic access representation (Phase 1)

```
access = base_slot[ invariant_base + IV * coeff ]
    invariant_base = const_off (compile-time bytes)
                   + symbolic part characterised by sym_div
```

`matmul_access.py` composes `vector_affine`'s `classify_access`, `word_aligned`
and `LoopAffineContext.varies` (R4.2.8) into **ten explicit predicates**, ordered
p1, p10, p2, p6, p3, p4, p5, p9, p7, p8. Multiplicands are anchored on the two
operands of the multiply that updates the reduction slot — mirroring
`kernel_detector`'s own reduction walk — not by sweeping affine loads.

No matrix-size, benchmark-name, variable-name, `vu8`-specific or tile logic.

## 3. The exact `plan_lowering` change (Phase 5)

**(a) Extraction generalised.** The affine test became
`vector_affine.classify_access(...) == CONTIGUOUS`, a strict **superset**: a bare
IV term classifies CONTIGUOUS with an empty invariant part
(`const_off == 0 and sym_div == 0`), records `None` as its base and takes the
byte-for-byte identical emission path. Only a real invariant part takes the new
path.

**(b) `need` derived from the reduction, not the kind.**

```diff
- need = 2 if kernel.kind in ('dot-product',) else 1
+ need = 2 if kernel.reduction_value == 'dot' else 1
```

Behaviour-preserving for pre-R13 kinds (dot-product → 2, sum-reduction → 1) and
**correct for matmul**, which is also a `load*load` reduction. The old
expression gave matmul 1, and `array_slots[:1]` would have silently dropped the
second multiplicand — a wrong-answer bug, not a missed optimisation.

**(c) Row bases materialised, reusing R9.3.** For `offset = base + IV*eb`,
substituting `IV = c*lanes` gives `off(c) = off(0) + c*lanes*eb`, so the
invariant part is computed **once** via `gemm_lowering.clone_offset(..., Const(0))`
into an address temp and each chunk becomes `[addr + c*lanes*eb]` — a
compile-time displacement. New plan fields `array_offs`, `array_addr`,
`array_base_pre`.

**(d) Emission branches on operand count, not kind.** `build_vector_body` used
`plan.kind == 'dot-product'`, which sent matmul down the ONE-operand `$vreduce`
path. `_is_dot_shaped(plan)` (`len(plan.array_slots) == 2`) replaces it —
identical for the pre-R13 kinds.

**Two guards against wrong answers, not silent fallbacks:**
- `build_compact_body` **declines** based accesses (returns `None`): the compact
  realisation addresses chunks with a register offset carrying no row base, so
  emitting it would address row 0 on every row — the exact shape of the R12.1
  bug. `lower_kernel` simply has one fewer candidate.
- A based access with `remainder > 0` is **rejected** (`based-access-with-
  remainder-unsupported`): the peel template replays the original loads, whose
  addressing this plan does not describe.

**(e)** `dot_vectorizer._SUPPORTED` gains `'matmul'`. Same client, same planner,
same emitter. No second `$dot` backend.

## 4. Found by the differential oracle, not shipped

First integration attempt produced `differential:mismatch`. Cause was (d) above —
matmul fell into the `$vreduce` path. **The oracle caught a real wrong-answer
bug before any test did.** It was fixed at the root, not patched around.

## 5. Phase 5 results

| dtype | N | lanes | trip | chunks | vec | `$dot` | realisation | ticks | ticks/out | checks | err |
|---|---|---|---|---|---|---|---|---|---|---|---|
| vi16 | 4 | 4 | 4 | 1 | no | 0 | — | 967 | 60.44 | 16 | 0 |
| vu16 | 8 | 4 | 8 | 2 | **yes** | **2** | unrolled | 1691 | 26.42 | 64 | 0 |
| vu8 | 16 | 8 | 16 | 2 | **yes** | **2** | unrolled | 6399 | 25.00 | 256 | 0 |
| vi16 | 16 | 4 | 16 | 4 | **yes** | **4** | unrolled | 7183 | 28.06 | 256 | 0 |
| vi32 | 16 | 2 | 16 | 8 | no | 0 | — | 35825 | 139.94 | 256 | 0 |
| vi8 | 24 | 8 | 24 | 3 | **yes** | **3** | unrolled | 15343 | 26.64 | 576 | 0 |
| vu8 | 32 | 8 | 32 | 4 | **yes** | **4** | unrolled | 30207 | 29.50 | 1024 | 0 |

`$dot` count equals `chunks` in every vectorized case. All correct, 0 errors,
0 spills. Every result is verified against gcc compiling the same source.

**Two non-vectorized cases, both expected and neither an R13 defect:**
- **4×4 vi16** — structurally ACCEPTED by all ten predicates, then declined by
  the **pre-existing** profitability rule `trip >= 2*lanes`
  (`vector_profitability.py:91`), untouched by R13. Per Phase 11 a scalar
  implementation legitimately wins here. NOTE: the spec named 4×4 vi16 as the
  smallest positive target; it does not vectorize, for a reason that predates
  R13. The smallest case that does is **8×8 vu16**.
- **vi32/vu32** — `illegal:isa-unsupported:no-32bit-dot`. An **ISA** limit from
  the capability DB. The datatype is never narrowed to win lanes.

## 6. Regression gate

| check | Phase-0 baseline | after Phase 5 |
|---|---|---|
| 38-program suite | 38/38 | **38/38**, metrics CSV **bit-for-bit identical** |
| negative controls | 3/3 | **3/3** |
| `pipeline_crosscheck` | 124/124 | **124/124**, 0 IR / 0 code / 0 tier mismatches |
| `loopopt/_*_test.py` | 25/25 | **25/25** |
| `compiler/_r*_test.py` | 20/20 | **20/20** + `_r13_0_test.py` |
| `_r13_0_test.py` | 42/42 | **59/59** |

**Existing dot-product and sum-reduction are provably unchanged**: the metrics
CSV is byte-identical, and dedicated tests assert they still get 2 and 1 array
slots respectively, with `array_offs` all `None` and no row-base prologue — i.e.
they take the pre-R13 path exactly.

## 7. Tests added

- `test_phase5_both_multiplicands` — the guard for the `need` change: two
  DISTINCT slots, both with an invariant base, both address temps materialised.
- `test_phase5_existing_kinds_unchanged` — dot-product/sum-reduction plan shape.
- `test_phase5_dot_is_emitted` — **mandatory**: compiles vu8 16×16, vi16 16×16,
  vi8 24×24 and asserts `$dot` is present and equals `chunks`. A simulator PASS
  with scalar fallback is not accepted.
- All Phase 1–4 negative controls retained, each asserting the OWNING predicate.

## 8. Remaining limitations

1. **Compact realisation unsupported for based accesses.** Declines cleanly; only
   the unrolled realisation is available to matmul. This bounds the R13.0
   configuration space in practice — every vectorized case above chose
   `unrolled` because it was the only candidate.
2. **Remainder unsupported for based accesses.** Rejected rather than mis-peeled.
   All tested shapes have `trip % lanes == 0`.
3. **Alignment provability is spelling-dependent** (pre-existing, not R13).
   Pre-hoisting row bases through a stack slot loses divisibility
   (`sym_div` 16 → 1); legality itself then rejects with
   `unaligned-packed-access`. Asserted in `test_known_limitations`.
4. **p4 is subsumed by p6** and is defence in depth, not load-bearing.
5. **No performance comparison against the hand-written reference yet** — Phase 5
   establishes correct `$dot` lowering only. The hand-written proof is
   **309 ticks / 1160 instrs / IPB 3.754 / 46.9% occupancy**; it is not
   comparable to the table above, which uses a different source shape (full
   256-element result dump) and no `data.map` preload.

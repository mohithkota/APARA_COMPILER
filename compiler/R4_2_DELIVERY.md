# R4.2 Delivery Report — Generic Vectorization Framework & Elementwise Vectorization

**Milestone:** R4.2 (generalize the R4.1 driver into reusable production
infrastructure, then implement automatic elementwise vectorization as its first
new client).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-31

> Two deliverables in one milestone. The framework is a REFACTOR: R4.1's
> dot/reduction vectorization is converted into a client and its output is proven
> byte-identical. The elementwise vectorizer is the new capability, and it adds
> **zero** pipeline logic — it supplies pattern matching, lowering and a dynamic
> model, nothing else. There is exactly ONE production vectorization pipeline.
> `APARA_NO_VECTORIZE=1` disables all of it.

---

## 1. Files added
| File | Purpose |
|---|---|
| `vector_pipeline.py` | **The generic pipeline.** Owns Kernel Detection → Legality → Profitability → Transformation → Validation → Compile → Commit/Rollback, module/function slicing, the dependence graph, the backend probe, statistics, reporting and determinism resets. `VectorTransform`, `MatchResult`, `DynamicModel`, `run_module`, `format_reports`. |
| `elementwise_vectorizer.py` | The elementwise client: claims the kinds, delegates matching/lowering, supplies the dynamic model. 50 lines of code. |
| `vector_elementwise_lowering.py` | Elementwise pattern matching (`plan_elementwise`) + lowering (`lower_elementwise`): packed load → `$v` → packed store + scalar remainder. |
| `vector_elementwise_corpus.py` | Corpus evaluation: the six kernel classes, the R4.1-vs-R4.2 conversion check, and the full-corpus no-regression proof. |
| `_r4_2_test.py` | Unit suite (79 checks). |
| `R4_2_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `dot_vectorizer.py` | Driver REMOVED (it is now the framework's). What remains is `DotReductionTransform` — the R4.1 client — plus the unchanged `vectorize_module` / `vectorize_dot_module` entry points and the new `vectorize_all_module`. 92 lines, down from 205. |
| `vector_lowering.py` | `PackedVectorInterp` gains the packed **store** (scatter). Additive: dot/reduction never mark a store, so R4.1 is inert to it. |
| `compiler.py` | The vectorization hook now calls `vectorize_all_module` (all clients, one pass) and prints the framework's report. Same guard, same kill-switch, same position in the pipeline. |

No scalar pass, scheduler, allocator, bundler, backend, or R4.0 analysis file is
modified. `kernel_detector.py`, `vector_legality.py`, `vector_profitability.py`
and `vector_capability*.py` are consumed **exactly** as R4.0 built them.

## 3. Phase 1 — the generic framework
The pipeline executes the full sequence without knowing which transformation is
being applied. A client supplies four things and cannot see anything else:

```
    kinds            which detected kernel kinds it claims
    match()          its own pattern matching        (default: accept)
    lower()          its own lowering to vector IR
    dynamic_model()  its own executed-op accounting
    validate()       optional; default = the packed differential oracle
```

Everything else is shared and unskippable: function slicing with globals
preserved, loop discovery + M1/M2/M3 annotation, the R2.1/R2.2 dependence graph,
the R4.0 legality and profitability calls, the **gate order**, the real-backend
spill/bundle probe, per-loop reporting, statistics, determinism resets, and
rollback. **A client cannot skip a gate, because it never sees the pipeline.**

That property is tested directly rather than asserted: `_ToyTransform` is a client
the framework has never seen, and the suite drives it through every hook and then
forces it to fail at `match`, `lower`, `validate` and `dynamic_model` in turn,
checking each time that nothing is committed and the scalar IR is returned
untouched.

**Reuse achieved:** 217 lines of shared pipeline code serve both clients, which
are 58 and 50 lines respectively. Adding a vectorizer adds no pipeline logic.

## 4. Phase 2 — elementwise recognition
Exactly four shapes are supported; everything else is rejected with a specific
reason:

```
    A[i] = B[i];        A[i] = B[i] + C[i];
                        A[i] = B[i] - C[i];
                        A[i] = B[i] * C[i];
```

Enforced: **packed arrays** (stride == element size), **affine accesses** (the
offset is the IV scaled by exactly `elem_bytes`), **one known trip count** shared
by every access, **contiguous** access, exactly one array store, and no other
array traffic in the body. The `$v` operation must be supported and reliable for
the element type — asked of the R4.0 capability layer, never hardcoded.

**Why the detector's kinds are only a pre-filter.** `kernel_detector` labels a
loop `vector-add` when the stored value has no multiply and `saxpy` when it does,
so `A[i] = B[i] * C[i]` arrives as `saxpy`. The client claims both kinds and then
does its own exact analysis: a *real* saxpy (`a*x[i]`, scalar × array) fails
because its operand is not an array load, and is rejected. R4.0's detector is
consumed unchanged.

**A displaced index cannot masquerade as a contiguous one.** `analysis_iv`'s
`iv_terms` only recognises `IV` or `IV * const` — a constant displacement is not
representable — so `a[i+1]` is simply not an affine term and is rejected at match
time. This was checked in the implementation rather than assumed, because a wrong
answer here would read the wrong elements.

## 5. Phase 3 — lowering
```
    per chunk:  packed load(s)  ->  $v <op>  ->  packed store
    then:       scalar remainder loop for trip % lanes  (dropped when rem == 0)
```
A copy needs no VALU at all — the loaded packed register is stored straight back,
and the emitted code contains zero `$v` instructions (verified).

**What elementwise adds over R4.1: the packed STORE.** R4.1 only ever *read*
packed data and reduced it to a scalar; elementwise must write `lanes` results
back contiguously. On hardware this is an ordinary 64-bit store into the packed
array; the `_vec_pack` marker exists only so the differential oracle models the
scatter. Each lane is truncated **exactly** as the scalar store would be
(`_trunc(v, elem_bytes, unsigned=False)`), so scalar and vector forms must leave
byte-identical memory — which is precisely what the differential then checks.

The same APARA memory reality binds as in R4.1: ordinary C arrays are stored one
element per 8-byte word, so only packed-marker arrays can be gathered or
scattered. Elementwise coverage is therefore restricted to packed arrays.

## 6. Phase 4 — validation & rollback
`differential_vector`'s packed specialisation is reused unchanged as the default
validator: 6 seeds over the full byte/half-word range, comparing return value AND
complete final memory. Every transformed loop must validate before commit.

Rollback is total and covers every failure mode — validation failure, backend
failure, spill introduction, compile failure, and profitability rejection. A loop
that fails any gate is left in its scalar form, so a function with no committed
kernel compiles byte-identically to the scalar compiler.

The oracle remains load-bearing: the narrow-32-bit-accumulator dot from R4.1 is
still caught and **rolled back** by the differential (1 rollback in the suite),
not mis-compiled.

## 7. Phase 5 — production integration
`compile_c_to_mcode` calls `vectorize_all_module`, which runs the one pipeline
with both clients in a single pass over the module. Verified end-to-end on a real
program: the elementwise kernel is vectorized, flows through the scalar optimizer,
R3.1 SWP and R3.2 superblock scheduling unchanged, and reaches the mcode as four
real `$v + $rN ($vi8) $rX $rY` instructions (one per chunk). With
`APARA_NO_VECTORIZE=1` the same program emits zero.

## 8. Corpus results
```
  R4.2 kernel suite -- ONE pipeline, BOTH clients (20 cases)
    vectorized                     : 14/14 expected
    behaviour mismatches           : 0        (100% differential validation)
    rollbacks                      : 1        (narrow accumulator, correctly)
    dynamic operations             : 9464 -> 558      (-94.1%)
    static bundles (vectorized)    : 336  -> 354      (+18, the unroll trade)
    pipeline time                  : 71.8 ms / kernel

    committed via dot-reduction    : 4   (dot vi8/vi16, reduction vi8/vi32)
    committed via elementwise      : 10  (add vi8/vu8/vi16/vi32, sub, mul vi8/vi16,
                                          copy, remainder, in-place)
    correctly rejected             : unpacked, saxpy a*x, divide, a[i+1],
                                     trip < 2*lanes, narrow accumulator

  Coverage vs R4.1 (same 20-case suite)
    R4.1 client set only           : 4/20
    R4.2 full client set           : 14/20      (+250%)

  R4.1 -> R4.2 conversion check
    dot/reduction kernels IDENTICAL through both paths : 4/4

  Full corpus (124 programs)
    vectorized (packed kernels)     : 0    (the general corpus has no packed arrays)
    scalar & byte-identical (on/off): 124/124   NO REGRESSION
    corpus pass time                : 14.1 ms / program
```

## 9. Success criteria — met
1. **Generic reusable vectorization framework** — `vector_pipeline.py`; 217 shared
   lines drive clients of 58 and 50 lines; proven generic by a synthetic client.
2. **R4.1 converted with no regressions** — 4/4 kernels byte-identical through
   both paths; `_r4_1_test.py` passes unchanged.
3. **Automatic elementwise vectorization** — copy/add/sub/mul over vi8/vu8/vi16/
   vi32, real `$v` in production mcode.
4. **100% differential validation** — 0 mismatches on every committed kernel.
5. **Automatic rollback** — every gate tested by forced failure; unsupported
   shapes rejected with specific reasons; narrow accumulator still rolled back.
6. **Reduced dynamic instruction count** — 9464 → 558 (−94.1%).
7. **Zero regressions** — full corpus 124/124 byte-identical; `pipeline_crosscheck`
   124/124; R3.1/R3.2/R4.0/R4.1 suites all pass.

## 10. Test summary
```
_r4_2_test.py ........................ ALL R4.2 UNIT TESTS PASS   (79/79 checks)
_r4_1_test.py / _r4_0_test.py ........ PASS (unchanged)
_r3_1_test.py / _r3_2_test.py ........ PASS (unchanged)
vector_elementwise_corpus.py ......... PASS (100% differential, R4.1 unchanged,
                                             scalar unchanged)
pipeline_crosscheck.py ............... 124/124 identical, 0 rollbacks
```

## 11. Honest notes / limitations
- **Packed arrays only** — the same binding constraint as R4.1, and the reason
  general-corpus coverage is 0. The value is demonstrated on the dedicated suite.
- **Static size grows** (+18 bundles on the vectorized kernels): the chunks are
  unrolled, so narrow element types with many chunks (vi16/vi32 at 8 chunks) pay
  the most. The win is dynamic (−94%). A compact vector loop would fix this and is
  the natural follow-on, as it was for R2.8 after R2.5.
- **The remainder can dominate a small trip.** `N=20` at 8 lanes gives 2 chunks
  and a 4-iteration scalar tail: 440 → 102 dynamic ops, much weaker than the
  ×25 of a clean multiple. The profitability gate (trip ≥ 2·lanes) bounds this
  but does not eliminate it.
- **Two operands maximum.** `d[i] = a[i]+b[i]+c[i]` is rejected rather than
  decomposed into two `$v` operations; expression trees are R4.4 territory.
- **A copy emits no `$v`** — it is a packed move. That is correct and optimal, but
  it means "elementwise vectorization" covers a shape where the vector ALU is
  unused.
- **Validation is the packed IR oracle** (no hardware simulation, per policy),
  modelling `golden_stubs.h` semantics including the known hardware bugs. A
  simulator-backed gate remains available.
- Not done (by mandate): **matrix multiplication (R4.3), convolution, and general
  loop vectorization (R4.4)** are all out of scope for R4.2.

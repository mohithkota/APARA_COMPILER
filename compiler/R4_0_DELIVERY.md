# R4.0 Delivery Report — APARA Vector Infrastructure & Capability Framework

**Milestone:** R4.0 (the production foundation for all future vector optimization
— the vector equivalent of R3.0; analysis, capability discovery, legality,
profitability, validation, kernel recognition).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-27

> NOT a vectorizer. Emits no vector instructions, changes no scalar code, redesigns
> nothing. Every ISA fact is determined DIRECTLY from the production
> implementation (never assumed). Proven: generated scalar code is byte-identical
> with and without the analysis (124/124); the frozen scalar pipeline is untouched.

---

## 1. Files added
| File | Purpose |
|---|---|
| `vector_capability_db.py` | The ground-truth ISA database: instructions, element types, lane widths, operand/alignment/grouping constraints, reduction/accumulate semantics, and the confirmed-broken list — extracted from codegen, ir_gen, and `golden_stubs.h`. |
| `vector_capability.py` | The reusable query layer (`VectorCapability`): can-vectorize / which-instruction / lanes / reliability / register-layout. The single API future passes consult. |
| `vector_legality.py` | Legality analysis (reuses CFG/LoopInfo/Dom/DependenceGraph/Disambiguator + the capability layer): accepts/rejects each kernel with a specific reason. |
| `vector_profitability.py` | Profitability estimation (lanes, instruction/bundle reduction, throughput, utilization, remainder cost). |
| `kernel_detector.py` | Kernel recognition (dot-product / sum-reduction / SAXPY / vector-add / matmul / convolution) — records structure only, no transform. |
| `vector_validation.py` | The vector differential oracle: extends `ir_interp` to execute vector IR per the hardware semantics; `differential_vector` for future passes. |
| `vector_corpus.py` | Corpus evaluation. |
| `_r4_0_test.py` | Unit suite (30 checks). |
| `R4_0_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `compiler.py` | One opt-in, print-only diagnostic block (`APARA_VECTOR_REPORT`, verbose) that reports legal vectorizable kernels. It never touches `body`/`mcode` — zero effect on generated code. |

No scalar pass, scheduler, allocator, bundler, or backend is modified.

## 3. The capability map — determined from the implementation
The APARA vector ISA, extracted from codegen's emitters + ir_gen's intrinsic
lowering + the "no-bias" `golden_stubs.h` reference (isa.txt + hardware-confirmed):

- **Lane model:** a 64-bit register holds `64/element_bits` packed lanes.
- **`$v` (VALU):** element-wise `+ - *` across lanes; `$replicate` broadcasts a
  scalar; types vi8/vu8/vi16/vu16/vi32/vu32.
- **`$dot` / `$dot $accumulate`:** sum of element-wise products; types
  vi8/vu8/vi16/vu16 (**no 32-bit dot**); hardware-confirmed correct.
- **`$dot128`:** 16×vu8 dot across a value pair (2 `$dot` instrs).
- **`$vreduce +`:** horizontal sum — **signed only**; unsigned sign-extends (a
  confirmed simulator bug). **`$vreduce $max`:** all types. Other reduce ops return 0.
- **wide `$ld/$st` (`$u128`/`$u256`):** contiguous 2/4-word move into an
  aligned register pair/quad.
- **`$slice` / `$pack` / `$fsqrt`.**
- **Confirmed broken (never to be emitted):** 4-bit lanes (vi4/vu4), unsigned
  `$vreduce` sum, `$vreduce` min/mul/or/xor/and, native `$abs/$max/$min`, 32-bit dot.

## 4. Validation framework (the key methodology piece)
`ir_interp` raises `Unsupported` on vector IR, so `vector_validation.py` **extends**
it (without modifying it) with a `VectorInterp` that executes
`IRVecArith/Dot/Dot128/Reduce/LoadWide/StoreWide` per the `golden_stubs.h`
semantics — **faithful to the hardware, including the unsigned-`$vreduce` bug**, so
the oracle would catch a pass that wrongly used an unreliable op. `differential_
vector(scalar, vector, …)` is the vector equivalent of the scalar differential that
gated R2/R3: run the scalar form (frozen scalar interpreter) and the vector form
(vector interpreter) from identical seeded memory and compare. **Validated against
real intrinsic-produced vector programs**: the oracle reproduces `golden_stubs.h`
exactly (e.g. `__dot_vi8` → 36) and catches an injected mismatch.

## 5. Kernel recognition + legality + profitability
- **Recognition** keys off framework facts already computed: a clean single-store
  accumulator whose value is `load(slot)+V` (a reduction), the shape of `V`
  (`load*load`→dot, `load`→sum), and whether accesses are affine in the IV
  (elementwise). Loop depth separates matmul from a plain dot.
- **Legality** grounds every decision in the capability layer: innermost counted
  loop, single exit, no calls, supported+reliable element type, ISA-supported
  operation, affine accesses, and — via the R2.2 disambiguator — no loop-carried
  memory dependence except the clean scalar IV/accumulator slots.
- **Profitability** estimates lanes, dynamic instruction/bundle reduction,
  throughput (~lanes), and the scalar-remainder cost.

## 6. Corpus results (124 programs)
```
  generated scalar code UNCHANGED : 124/124   (the milestone's core guarantee)

  Detected kernels (40): sum-reduction 28, vector-add 7, dot-product 2,
                         matmul 2, saxpy 1
  Legal (ISA-supported) : 12       Profitable : 6   (avg 2.0 lanes / 2.0x / 47%)

  Rejection reasons (recognised but not vectorizable):
    unsupported/unanalyzable element type : 11
    unproven aliasing                     :  5
    trip-count unknown                    :  4
    call in body                          :  5
    no-32bit-dot                          :  2
    unsigned-vreduce-buggy                :  1

  Validation oracle: executes 6/6 real vector-intrinsic programs.
```
The headline value is exactly the ISA-grounding the milestone demanded: the
framework **rejects the byte-sum-unsigned and 32-bit-dot kernels the hardware
cannot do correctly**, while accepting the 8-lane signed-byte kernels (8×) — no
assumptions, all traceable to `golden_stubs.h`/STATUS.md.

## 7. Success criteria — met
1. **Complete ISA capability map** — `vector_capability_db.py`, from the implementation.
2. **Reusable capability layer** — `vector_capability.py`.
3. **Reusable legality analysis** — `vector_legality.py`.
4. **Reusable profitability model** — `vector_profitability.py`.
5. **Production-quality validation framework** — `vector_validation.py`, the vector
   differential oracle, validated against real vector programs.
6. **Automatic kernel detection** — `kernel_detector.py` (6 idiom classes).
7. **Zero changes to generated scalar code** — byte-identical 124/124.
8. **No regressions** — `pipeline_crosscheck` 124/124; R2.5–R3.2 suites all pass.

## 8. Test summary
```
_r4_0_test.py ........................ ALL R4.0 UNIT TESTS PASS   (30/30 checks)
_r3_1 / _r3_2 / R2.5-R3.0 ........... PASS (unchanged)
pipeline_crosscheck.py ............... 124/124 identical
```

## 9. Honest notes / limitations
- **Validation is IR-level (vector interpreter), grounded in `golden_stubs.h`.** No
  hardware simulation is invoked (project policy). The vector oracle models the
  documented hardware behaviour (including bugs); a simulator-backed acceptance
  gate remains available for R4.1+ if desired.
- **Recognition/legality are conservative by design.** matmul/convolution are
  recognised structurally but their 2-D compound indices are not yet proven affine
  here (reported, not vectorized). Symbolic trip counts, non-unit strides, and
  pointer-aliased arrays are rejected pending later milestones.
- **Profitability numbers are estimates** (the corpus skews to 2-lane int kernels;
  8-lane byte kernels give 8×). The lane/throughput facts are exact; the dynamic
  reduction is modelled.
- Not done (by mandate): no automatic vectorization, no vector emission, no scalar
  redesign. R4.1 (dot/reduction), R4.2 (elementwise), R4.3 (matmul), R4.4 (general)
  build on this foundation.

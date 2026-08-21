# R14.8 — Scalar result-store parallelization

Branch `feature/r13-matmul-dot`, on top of R14.7 (`bff7743`). Frozen tags
untouched, nothing pushed.

## Answer to the final question

> "Can the compiler generically expose the already-existing independence among
> the scalar result stores, using shared affine bases and suitable
> address-register allocation, and turn that independence into fewer executed
> bundles?"

**Yes.** For **16×16 vu8 J_TILE=4**:

| | R14.6 | **R14.8** |
|---|---|---|
| scalar-epilogue bundles | 16 | **5** |
| total block bundles | 24 | **12** |
| block instructions | 55 | **44** |
| **ticks** | 5343 | **4575 (−14.4%)** |
| ticks/output | 20.87 | **17.87** |
| spills | 0 | **0** |
| epilogue IPB | 1.31 | **2.60** |
| vector-head bundles / instrs | 8 / 42 | 7 / 31 |

The four result stores now issue in **one bundle** — exactly the 4-per-bundle
memory-lane limit.

## 1. The current epilogue and its IR

IR before (`vu8` 16×16, J_TILE=4):

```
_t136 = _t132 + _t134          i*16 + j          <- already common
_t138 = _t136 + 0 ; _t139 = _t138 * 8 ; DMEM[0x400+_t139] = _t128
_t145 = _t136 + 1 ; _t146 = _t145 * 8 ; DMEM[0x400+_t146] = _t141
_t152 = _t136 + 2 ; _t153 = _t152 * 8 ; DMEM[0x400+_t153] = _t148
_t159 = _t136 + 3 ; _t160 = _t159 * 8 ; DMEM[0x400+_t160] = _t155
```

The common part `_t136` was **already shared**; what was duplicated is the `*8`
and, crucially, the address materialisation.

`codegen._gen_IRGlobalStore` lowers each store with a computed offset as

```
+  addr, off_reg, goff        addr = self._safe_borrow(...)   <- SCRATCH
+  addr, GBASE, addr
$st [addr + 0], value
```

into a **borrowed** scratch register released immediately afterwards. Four
stores therefore became four sequential borrow/build/store chains — and the
bundler is a greedy forward pass that does not reorder, so it could not
interleave them.

## 2. Common-base proof

Offsets are `(common + t)·8`, i.e. `common·8 + t·8`. Resolved with R14.2's
existing machinery — `vector_affine.resolve_offset` + **`constant_delta`** — the
four differ by the compile-time constants **0, 8, 16, 24**, derived from the
element width, not hard-coded. No second affine analysis was written.

## 3. Why the allocator serialized them — classification

**C — allocation register reuse**, from the `_safe_borrow` per store, measured in
R14.7: `$r3` live in bundles 10–14, `$r12` 14–18, `$r16` 18–22, `$r17` 22–24,
each chain's register immediately reused by the next. Not A (all four have
identical ASAP = 9), not E (zero MemAlias), not F (no lane limit reached).

## 4. What-if, measured with the project's own bundler

`bundler.bundle_mcode` on the two forms:

| | instructions | **bundles** |
|---|---|---|
| current epilogue | 20 | **16** |
| idealised shared-base epilogue | 10 | **3** |

The four stores packed into one bundle. That justified implementing.

## 5. The implementation — no new IR node, no codegen change

`global_store_base_sharing.py`. A group of `IRGlobalStore` whose offsets are
provably a constant apart is re-expressed with the **existing** nodes:

```
IRGlobalAddrOf(base, dmem_addr_0, offset_0)      # materialise once
IRStore(base, Const(d_t), src_t, elem_bytes)     # $st [reg + imm]
```

`_gen_IRStore` already lowers a `Const` offset in `[-512, 511]` to a single
`$st [base + imm]` with no borrow. Nothing is reordered — each store stays where
it was, so memory ordering and aliasing are untouched. Kill switch
`APARA_NO_STORE_BASE_SHARE`.

**Placement matters and was found by measurement.** The pass must run *before*
`strength_reduce`: SR rewrites `x * 8` into `x << 3`, and
`vector_affine._resolve` deliberately rejects shifts, so the constant relation
becomes unprovable afterwards. Wired at that point; the rewritten stores carry
`Const` offsets and survive the rest of the pipeline unchanged (verified
end-to-end through IVSR, SR, LICM, loop-reg, GVN, mem2reg and the final clean).

## 6. Before/after bundles

```
BEFORE (16 epilogue bundles)        AFTER (5 epilogue bundles)
 b9  << $r19, $r17, 3               b8  + $r16,$r2,0 | 4 value copies   [5/8]
 b10 + $r3, $r19, 0                 b9  << $r17, $r16, 3                [1/8]
 b11 + $r3, $r28, $r3               b10 + $r10, $r10, $r17              [1/8]
 b12 $st [$r3 + 0]                  b11 + $r3, $r28, $r10               [1/8]
 b13 << $r3, $r21, 3                b12 $st [$r3+0] | [$r3+8]
 ... 11 more bundles ...                | [$r3+16] | [$r3+24] | branch  [5/8]
 b24 $st [$r17 + 0]
```

## 7. Results — every supported datatype and size

| dtype | N | ticks R14.6 → R14.8 | ticks/output | checks | errors |
|---|---|---|---|---|---|
| vu8 | 16 | 5343 → **4575** (−14.4%) | 17.87 | 256 | 0 |
| vu8 | 32 | 22175 → **19103** (−13.9%) | 18.66 | 1024 | 0 |
| vi8 | 16 | 5087 → **4319** (−15.1%) | 16.87 | 256 | 0 |
| vi8 | 32 | 21151 → **18079** (−14.5%) | 17.66 | 1024 | 0 |
| vu16 | 16 | 5279 → **4511** (−14.5%) | 17.62 | 256 | 0 |
| vu16 | 32 | 29216 → **25376** (−13.1%) | 24.78 | 1024 | 0 |
| vi16 | 16 | 5023 → **4255** (−15.3%) | 16.62 | 256 | 0 |
| vi16 | 32 | 31264 → **27424** (−12.3%) | 26.78 | 1024 | 0 |

**Not tied to four outputs** — the gain tracks the group size:

| J_TILE | stores | displaced stores | ticks | change |
|---|---|---|---|---|
| 1 | 1 | **0** | 6143 → 6143 | **unchanged** (nothing to share) |
| 2 | 2 | 1 | 5535 → **5023** | **−9.3%** |
| 4 | 4 | 3 | 5343 → **4575** | **−14.4%** |

## 8. Register pressure

**Zero spills everywhere.** Pressure *falls*: one base register replaces four
independent address chains, and the borrowed scratch registers disappear.

## 9. Anti-bias

Renamed variables produce identical ticks and identical displacement counts.
**Reversed store order** is recognised as the same group and emits negative
displacements — `[$r3 + -8]`, `[-16]`, `[-24]` — for **identical ticks (4575)**.
(My first test regex only matched positive displacements and reported a false
failure; the compiler was right and the test was wrong.)

## 10. Regression

| check | result |
|---|---|
| 38-program suite | **38/38 PASS**; 4 programs changed, **0 slower** |
| negative controls | **3/3** |
| `pipeline_crosscheck` | **124/124**, 0 IR / 0 code / 0 tier mismatches |
| `compiler/_r*_test.py` | **27/27** |
| `loopopt/_*_test.py` | **25/25** |
| `_r14_8_test.py` | **24/24** |

The only changed suite programs are the four `dot` kernels, all from R14.6's
self-copy fix, all improvements. **The R14.8 pass is inert on the existing
corpus** — no suite program has a group of independent global stores a constant
apart — so GEMM, reduction, dot, convolution, AXPY and elementwise are untouched
by it.

**`_r14_3_test.py` was updated, deliberately.** It pinned the epilogue
bottleneck (IPB < 2, mostly 1-instruction bundles). R14.8 removed that
bottleneck, so those assertions are now false *by design*. They were inverted to
guard the new state (epilogue IPB > 2, ≤ 5 bundles, stores sharing one bundle)
rather than deleted.

## 11. Comparison with the hand-written kernel

| | strategy | epilogue bundles |
|---|---|---|
| hand-written | `[$r29 + imm]`, one base live across the whole row | ~2 |
| compiler R14.6 | four borrowed scratch chains | 16 |
| **compiler R14.8** | **one base + immediates, rebuilt per j-tile** | **5** |

The remaining structural difference: the hand-written kernel keeps its result
base live across the entire row, while the compiler rebuilds it per j-tile
(bundles b9–b11, 3 bundles). Hoisting that base out of the j-loop is the
residual — and, unlike R14.3's premise, it is a genuine loop-invariance
opportunity. **Not pursued: this milestone ends here.**

# R14.6 — Eliminate the redundant self-copy before `$dot $accumulate`

Branch `feature/r13-matmul-dot`, on top of R14.5 (`9ecb321`). Frozen tags
untouched, nothing pushed. **One file changed: `compiler/codegen.py`** (plus the
dedicated test and this report).

## Answer to the final question

> "Can the compiler safely eliminate the identity self-copy when `dest == acc`,
> while preserving the required copy when `dest != acc`, and does this produce a
> reproducible runtime improvement?"

**Yes to safety and preservation, both proven directly. The runtime improvement
is real but uneven — reproducible where it lands, and exactly zero elsewhere.**

| | answer |
|---|---|
| **existing dot-product kernels** | improve, all four, small but consistent |
| **matmul J_TILE=2** | **−2.3%** (5663 → 5535 ticks) |
| **matmul J_TILE=4** | **−2.5% / −2.3% on two of three**; unchanged on the other |

## 1. The `dest` / `acc` contract

`codegen._gen_IRVecDot`. `dest` is the physical register the allocator bound to
`ir.dest`; `acc` is the physical register holding `ir.accum`. Both come from
`_alloc_reg`, so they are the code generator's own resolved register identities —
not source names, not benchmark names.

`$dot $accumulate` is **read-modify-write on its destination**, so `dest` must
already hold the accumulator when it issues.

* **`dest != acc`** — the accumulator lives elsewhere; the copy is **required**.
* **`dest == acc`** — occurs whenever the lowering accumulates in place
  (`IRVecDot(acc, .., accum=acc)`, the R2.6 loop-register form that R13.1 and
  R14.2 both produce).

## 2. Why skipping it is safe — the argument does not involve values

When `dest` and `acc` name the same register the emitted text is:

```
+ rX ($i64) $r0 rX          i.e.  rX = 0 + rX
```

That is the identity **for any contents of `rX`**. The proof is about the
emitted instruction, not about what the register happens to hold, so no
liveness or value reasoning is required.

## 3. The change

```diff
             acc, ba = self._operand_reg(ir.accum, protect=[d, rs1, rs2] + sn)
-            self._emit(f"+ {dest} ($i64) {ZERO} {acc}")
+            if dest != acc:
+                self._emit(f"+ {dest} ($i64) {ZERO} {acc}")
             self._emit(f"$dot $accumulate {dest} ({ir.type_str}) {rs1} {rs2}")
```

One guard. Nothing else in `codegen.py` touched; no change to the scheduler,
bundler, vectorizer, register allocator or dot planner.

## 4. Diff attribution (Phase 7) — clean

Compiled before/after and compared the **instruction multiset** (bundle
boundaries and `$null` padding shift when an instruction is removed, so a raw
textual diff is the wrong level):

| program | instrs before → after | self-copies removed | **other removed** | **added** |
|---|---|---|---|---|
| dot vi16 | 158 → 142 | 16 | **0** | **0** |
| matmul vu8 16×16 JT=4 | 169 → 161 | 8 | **0** | **0** |
| matmul vi16 16×16 JT=4 | 200 → 184 | 16 | **0** | **0** |

**Every changed instruction is a removed identity self-copy. No other opcode
changed and nothing was added.**

## 5. Existing dot kernels (Phase 4)

Bit-for-bit identity is deliberately *not* required for this milestone. 4 of 38
programs change, all improvements, all PASS, **no new spills**:

| program | ticks | instructions | static bundles |
|---|---|---|---|
| dot vi8 | 889 → **888** | 1204 → 1196 | 50 → 49 |
| dot vu8 | 1017 → **1016** | 1332 → 1324 | 52 → 51 |
| dot vi16 | 833 → **831** | 1388 → 1372 | 57 → 56 |
| dot vu16 | 960 → **959** | 1516 → 1500 | 59 → 58 |

The other 34 programs are unchanged. `dot vi32`/`vu32` are unaffected because
the ISA has no 32-bit `$dot` (they never vectorize).

## 6. Matmul (Phase 5) — honest, including the null results

| case | ticks | ticks/output | self-copies | spills | correct |
|---|---|---|---|---|---|
| vu8 16×16 JT=2 | 5663 → **5535** (−2.3%) | 21.621 | 0 | 0 | 256/256 |
| vu8 16×16 JT=4 | 5343 → 5343 (**0%**) | 20.871 | 0 | 0 | 256/256 |
| vi16 16×16 JT=4 | 5151 → **5023** (−2.5%) | 19.621 | 0 | 0 | 256/256 |
| vu8 32×32 JT=4 | 22687 → **22175** (−2.3%) | 21.655 | 0 | 0 | 1024/1024 |
| vi16 32×32 JT=4 | 31264 → 31264 (**0%**) | 30.531 | 0 | 0 | 1024/1024 |

**All 78 identity self-copies are gone in every configuration**, but two cases
show no tick change: there the copies sat in bundles that had spare slots, so
removing them freed issue slots without removing a bundle. That is the expected
outcome for a redundancy that is bundle-visible only sometimes — it is reported
rather than averaged away.

## 7. Validation

| check | result |
|---|---|
| 38-program suite | **38/38 PASS** (4 programs improved, 34 identical) |
| negative controls | **3/3 rejected** |
| `pipeline_crosscheck` | **124/124** — 0 IR / 0 code / 0 tier mismatches |
| `compiler/_r*_test.py` | **26/26** |
| `loopopt/_*_test.py` | **25/25** |
| `_r14_6_test.py` | **17/17** |

`pipeline_crosscheck` passing at 124/124 is worth noting: it compares generated
code per tier, and it is unaffected because the guard only fires on `$dot`
accumulate forms, which the crosscheck corpus does not exercise.

## 8. Test design note

The end-to-end sources all produce `dest == acc`, so compiling them **never
reaches the `dest != acc` branch**. A suite built only from them would leave the
preserved-copy path untested and still pass. `_r14_6_test.py` therefore drives
`_gen_IRVecDot` **directly** with two distinct accumulator temps and asserts:

* `dest == acc` → exactly one instruction emitted, the `$dot`;
* `dest != acc` → two instructions, the copy first, and that copy is **not** an
  identity.

Both branches are proven, not inferred.

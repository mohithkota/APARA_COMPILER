# R14.5 — `$dot $accumulate` audit

Branch `feature/r13-matmul-dot`, production at R14.2 (`dee8954`; R14.3 changed no
production `.py`). **ANALYSIS ONLY — 0 production `.py` files changed.** Frozen
tags untouched, nothing pushed.

## Answer to the final question

> "Does the current R14.2 matmul path already exploit the hardware's combined
> `$dot $accumulate` instruction wherever semantically possible?"

**YES.** Of **78** `$dot` instructions inspected across six configurations:

| category | count |
|---|---|
| **A — direct `$dot $accumulate` into the live accumulator** | **78** |
| B — `$dot` into a temporary, then a copy into the accumulator | **0** |
| C — `$dot` into a temporary, then arithmetic/tree reduction | **0** |
| D — any other form | **0** |

**No redundant dot→temp→accumulator pattern remains.** Stop condition 1 applies,
so no fix was implemented for the pattern this milestone targeted.

## But the audit found a different redundancy — quantified, not fixed

Every `$dot $accumulate` is preceded by a **self-copy** `+ rX, ($i64) r0, rX`,
i.e. `rX = 0 + rX` — provably the identity. **78 `$dot`, 78 self-copies: exactly
1:1.**

| case | `$dot` | A | B | C | D | self-copies | bundles | ticks |
|---|---|---|---|---|---|---|---|---|
| vu8 16×16 JT=1 | 2 | 2 | 0 | 0 | 0 | 2 | 8 | 6143 |
| vu8 16×16 JT=2 | 4 | 4 | 0 | 0 | 0 | 4 | 14 | 5663 |
| vu8 16×16 JT=4 | 8 | 8 | 0 | 0 | 0 | 8 | 24 | 5343 |
| vi16 16×16 JT=4 | 16 | 16 | 0 | 0 | 0 | 16 | 29 | 5151 |
| vu8 32×32 JT=4 | 16 | 16 | 0 | 0 | 0 | 16 | 28 | 22687 |
| vi16 32×32 JT=4 | 32 | 32 | 0 | 0 | 0 | 32 | 39 | 31264 |

### Exact cause — `codegen.py:1456-1459`

```python
if ir.accumulate and ir.accum is not None:
    acc, ba = self._operand_reg(ir.accum, protect=[d, rs1, rs2] + sn)
    self._emit(f"+ {dest} ($i64) {ZERO} {acc}")          # <-- unconditional
    self._emit(f"$dot $accumulate {dest} ({ir.type_str}) {rs1} {rs2}")
```

`$dot $accumulate` is read-modify-write on `dest`, so `dest` must start out
holding the accumulator — the copy is **correct and necessary when
`dest != acc`**. It is emitted unconditionally, so when the allocator has
already placed the accumulator *in* `dest` — which is exactly what R14.2's
in-place accumulation (`dest` **is** `accum`) produces — it degenerates to a
no-op.

**The temporary is not semantically required in that case, and the accumulator
is already live and available as the `$dot` destination.** The obstacle is
**codegen**, not the IR representation or the lowering: `IRVecDot` already
carries `dest is accum`.

### Measured cost — bundle-visible in half the cases, free in the others

Controlled experiment (guard added, measured, then **reverted**):

| case | bundles before → after | ticks before → after |
|---|---|---|
| vu8 16×16 JT=1 | 8 → 8 | 6143 → 6143 (free) |
| vu8 16×16 JT=2 | 14 → **13** | 5663 → **5535** (−2.3%) |
| vu8 16×16 JT=4 | 24 → 24 | 5343 → 5343 (free) |
| vi16 16×16 JT=4 | 29 → **27** | 5151 → **5023** (−2.5%) |
| vu8 32×32 JT=4 | 28 → **26** | 22687 → **22175** (−2.3%) |
| vi16 32×32 JT=4 | 39 → 39 | 31264 → 31264 (free) |

So the redundancy is **neither uniformly free nor performance-critical**: 3 of 6
cases gain ~2.3–2.5% and lose 1–2 bundles; the other 3 absorb the copies into
bundles that exist anyway.

### Why it was NOT implemented

The same guard also fires on the **existing dot-product** path, because R13.1's
accumulator expansion likewise produces `dest == acc`. Measured on the 38-program
suite, 4 of 38 programs change — **all improvements, all still PASS**:

| program | ticks | static |
|---|---|---|
| dot vi8 | 1204 → 1196 | 128 → 120 |
| dot vu8 | 1332 → 1324 | 130 → 122 |
| dot vi16 | 1388 → 1372 | 186 → 170 |
| dot vu16 | 1516 → 1500 | 188 → 172 |

That trips **stop condition 5** (existing dot/reduction behaviour disturbed) on
top of stop condition 1, and it would be the first time since R13.0 that the
38-program metrics stop being bit-for-bit identical to the frozen baseline.
Shipping a change to *shared codegen* that alters four shipped kernels is a
decision for the project owner, not something to slip into an audit — so the
experiment was reverted and byte-identity re-verified.

## Phase 6 — which paths already use direct accumulate

All of them, in every configuration measured: matmul at J_TILE=1/2/4, and the
existing dot-product path. J_TILE does **not** change the form — only the count
(`$dot` = chunks × J_TILE). Sum-reduction uses `$vreduce` + integer add and has
no `$dot` at all, so it is out of scope for this audit.

## Verification

`git status` clean; **0 production `.py` changed**; 38/38 with metrics
**bit-for-bit identical** to the Phase-0 baseline after the revert.

## Conclusion

The remaining scalar-epilogue and addressing bottleneck identified in R14.3
(16 of 24 bundles, IPB 1.31) is **not** caused by separated dot and accumulate.
Dot and accumulate are already fused everywhere they can be.

A one-line codegen guard (`if dest != acc:`) would remove 78 provably-identity
copies and is worth ~2.3–2.5% on half the matmul configurations plus a small
improvement to four existing `dot` kernels — **available if you want it as its own
milestone**. Not started, per this milestone's closing instruction.

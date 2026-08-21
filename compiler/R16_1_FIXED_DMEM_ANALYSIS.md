# R16.1 — Fixed-DMEM matmul analysis: **hypothesis rejected**

Compiler at `e5bf5da`. **ANALYSIS ONLY — 0 production `.py` changed.** Frozen
tags untouched, nothing pushed. Source/layout changes only, as specified.

## Answer

> "Does moving A/B/C from stack locals to fixed DMEM allow the compiler to keep
> the 8 output accumulators in registers and approach the 241-tick handwritten
> schedule?"

**No — and the register question never even arises.** Moving the matrices to
globals **loses vectorization entirely**: `$dot` count drops to **0** and the
kernel falls back to scalar. The result is **8×–4× slower**, not faster.

**Classification: C — fixed-DMEM does not materially help. The data-placement
hypothesis from R16.0 is rejected.**

## Results — 16×16 vu8, identical computation, identical inputs

| configuration | ticks | `$dot` | correctness | notes |
|---|---|---|---|---|
| **stack-local JT=4** (current) | **4575** | 8 | 256/256 | vectorized |
| stack-local JT=8 | 7950 | 16 | 256/256 | vectorized, regresses (R16.0) |
| fixed-DMEM JT=4, no `--dmem-init` | — | 0 | **FAILS** | **IMEM overflow — never halts** |
| fixed-DMEM JT=4, `--dmem-init` | **37000** | **0** | 256/256 | **scalar** |
| fixed-DMEM JT=8, `--dmem-init` | **19164** | **0** | 256/256 | **scalar** |
| hand-written 8-dot | **241** | 32 | 256/256 | reference |

Only the storage placement changed — same dimensions, datatype, input values,
loop bounds, J_TILE, and mathematical operation.

## Why: the vector lowering requires **stack-local** arrays

```
[vectorize] NOT COMMITTED: 0 kernel(s) vectorized (1 declined, 0 rolled back)
    report: Vec[main:'fc_9'] rolled back: pattern:array-bases-not-extracted
```

`vector_lowering.plan_lowering` accepts an array access only when its base is a
**local `IRLoadAddr` stack slot** (`base.name not in addr_off` → skip). Global
arrays are addressed GBASE-relative via `IRGlobalLoad`/`IRGlobalStore`, so **no
array is extracted**, `need` is never met, and the kernel goes scalar.

This is the same predicate R13.0 had to generalise for the *offset* form
(`invariant_base + IV*eb`). The *base* form — stack slot vs global — was never
generalised.

**So the register-pressure benefit of fixed DMEM cannot be measured at all with
the current compiler: vectorization is lost before registers become relevant.**

## Secondary finding: global initializers overflow IMEM

Without `--dmem-init`, 512 bytes of global initializers generate **531 init
stores → 1107 source bundles → 2651 aligned bundles**, against the stack-local
version's **74**. The program **never halts** and all 256 PostConditions fail.

`--dmem-init` fixes exactly this — **2651 → 97 aligned bundles**, correct output
— by preloading via `data.map` instead of emitting stores. The mechanism works
as documented; it simply cannot recover the lost vectorization.

## Memory map (verified from the compiler's own report)

```
gbase = 0x400,  global area 0x400 – 0xe00
  A[256]        0x400 .. 0x4ff      (word 0x80)   vu8, preloaded
  Bt[256]       0x500 .. 0x5ff      (word 0xa0)   vu8, preloaded
  results[256]  0x600 .. 0xdff      (word 0xc0)   i64, zero-init
```

`data.map` word `0x80` = `0x030a00070e040b01` = A[0..7] = 3,10,0,7,14,4,11,1 —
matching the source initializer exactly (APARA is MSB-first). The preload itself
is correct; the arrays are genuinely in fixed DMEM.

## Kernel vs initialization (Phase 8)

| | end-to-end ticks | init ticks | kernel ticks |
|---|---|---|---|
| stack-local JT=4 | 4575 | 3331 (73%) | **1225** |
| fixed-DMEM JT=4 | 37000 | **~0** (preloaded) | **~37000** |
| hand-written 8-dot | 241 | 0 (preloaded) | **241** |

Fixed DMEM **does** deliver the initialization saving R15.0 predicted — init goes
to essentially zero. But the kernel becomes **30× worse** (1225 → ~37000) because
it is no longer vectorized, so the net is a large loss.

This cleanly separates the two effects R15.0 conflated: the init saving is real,
and it is **swamped** by losing `$dot`.

## What fixed-DMEM actually changed (Phase 9)

| category | effect |
|---|---|
| A. removes array-base registers | **not observable** — kernel went scalar first |
| B. absolute-immediate addressing | not reached |
| C. reduces accumulator pressure | not reached |
| D. eliminates accumulator traffic | not reached |
| **E. reduces initialization** | **yes — 3331 → ~0 ticks** |
| F. changes bundling | yes, adversely (scalar) |

Only **E** materialised. A–D are gated behind vectorization, which was lost.

## J_TILE comparison under fixed DMEM (Phase 10)

| J_TILE | ticks | `$dot` |
|---|---|---|
| 4 | 37000 | 0 |
| 8 | **19164** | 0 |

JT=8 is ~2× better than JT=4 here — the scalar code reuses the A-row load across
8 columns instead of 4. That is a *scalar* reuse effect, and it does not approach
the vectorized stack baseline (4575), let alone 241.

Datatype/size sweep (Phase 11) was **not run**: with `$dot = 0` on the primary
target, the mechanism under test is absent, so a wider sweep would measure only
scalar codegen. Stated rather than silently skipped.

## Conclusion

R16.0 recommended fixed DMEM on the reasoning that it would free the
register-held array bases. **That reasoning is sound but untestable on this
compiler**, because a prior constraint fires first: **the vectorizer only accepts
stack-local packed arrays.**

The true remaining blocker is therefore **not** data placement. It is that
`plan_lowering` recognises exactly one storage class. Making fixed DMEM
evaluable would require generalising the array-base predicate from
"local `IRLoadAddr` slot" to "local slot **or** global address" — a **production
compiler change**, which Phase 13 forbids in this milestone.

**Not implemented. R16.2 not started.** If this line is resumed, the milestone to
scope is *"generalise the vector lowering's array-base predicate to global
arrays"* — after which the R16.0 register hypothesis becomes measurable for the
first time.

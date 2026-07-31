# R6.3 — Sliding Window Completion (investigation)

**The stated hypothesis was wrong, and the investigation says so.** The defect is
not in the chunk-advance logic and not in the funnel-shift reconstruction. The
sliding-window lowering computes **correct results**; the failure comes from an
interaction with R6.2's memory disambiguation.

One genuine unsoundness in that disambiguation was found and corrected. It is not
sufficient to unblock R6.3, so **no lowering is committed** and the convolution
kernels remain declined. Verification stays at **38/38**.

---

## 1. What was instrumented

The lowering was re-applied and exercised, recording per chunk the byte shift,
the two aligned word addresses, the reconstructed value and the expected scalar
values — via kernels that expose **every** output element rather than three.

## 2. The reconstruction is correct

| kernel | shape | result |
|---|---|---|
| conv 3-tap vi8, `in[40]`, 30 iterations, **all 30 outputs checked** | 4 chunks (lanes 8) | **PASS** |
| conv 3-tap vi8, `in[72]`, 61 iterations, **first 24 outputs checked** | 8 chunks + remainder | **PASS** |

The second kernel is the *exact* shape of the failing suite case, and `out[11]` —
the element the suite reports as wrong — comes back **`0xc`, the expected value**.
Chunk 0 and chunk 1 are both correct, so the premise "chunk 0 correct, chunk 1
incorrect" does not hold. Emitted code contains **0 unaligned loads**.

## 3. Root cause: memory disambiguation, not the window

The suite kernel differs from the passing one in one respect only — it reads
`out[0]`, `out[11]`, `out[60]` into `results[0..2]` instead of reading outputs
sequentially. That is not a vector-lowering difference. Bisecting the backend:

```
memory disambiguation OFF (APARA_NO_MEMDISAMB=1)   PASS   1888 ticks
memory disambiguation ON                           FAIL   Mem[0x81] = 0x0, expected 0xc
```

The same failure appears identically for **vi8, vu8, vi16, vu16, vi32 and vu32** —
different lane counts, different byte shifts, different chunk counts. A defect in
the window arithmetic could not be width-invariant like that; a defect in memory
disambiguation is exactly that.

A later scalar read of `out[11]` is being allowed to move above, or co-issue
with, the vector stores that write it, so it reads 0.

## 4. The unsoundness found and corrected

R6.2 let a register keep its symbolic value across basic blocks when it was
**written exactly once**, arguing that a single definition must precede every
dynamic use.

That argument is wrong. "Written once" is a **static** count. A definition inside
a loop body executes on every iteration, and its symbolic value is expressed in
terms of *that block's live-in symbols*, which denote a different concrete value
each time round. Carrying such a value into another block lets two addresses
cancel symbols that were never equal at the same instant, so the model can report
`independent` for accesses that genuinely alias.

**Correction (the minimum one):** carry a value only when it is written once
**and** depends solely on **function-entry live-ins**. Such a value is
loop-invariant by construction, so carrying it is safe — and it is precisely the
case the carry exists for, since LICM hoists array bases into the preheader as
`FP + constant`.

```python
def _entry_only(addr):
    return addr.ok and all(s[0] == 'in' and s[1] == 0 for s in addr.terms)
```

This is a real correctness fix to shipped R6.2 code and is committed on its own
merits. **It does not resolve the R6.3 failure**, so the disambiguation defect
that does is still open.

## 5. What is still unknown

Disabling the model in only one of its two consumers makes the kernel pass in
both cases:

```
scheduler reordering only   PASS
bundle packing only         PASS
both (production default)   FAIL
```

So the failure needs both paths active, which is not yet explained. Pair-scanning
the emitted stream found 40 pairs proven independent inside a block, none of them
obviously wrong on inspection. Isolating the offending pair is the next step and
was not completed.

## 6. Recovered kernels and performance

**None, and none claimed.** The lowering is not committed, so there is nothing to
measure. R6.2C's cost stands unchanged: 8 convolution/2-D kernels declined,
`conv3` simulator ticks 5854 → 9895.

Recording the one number the investigation did produce, clearly labelled as
**not shippable**: with disambiguation disabled, the reconstructed conv 3-tap vi8
runs **1888 ticks** against **1517** for the scalar form the compiler currently
ships — i.e. the sliding-window vector form is presently *slower* than scalar for
this kernel, because each shifted tap costs two aligned loads plus three ALU
operations. That is a further reason not to rush it in: even once correct, it
needs the profitability gate to be re-examined.

## 7. Regression (for the committed soundness fix)

| check | result |
|---|---|
| verification harness (simulator, 38 programs) | **38/38 PASS**, negative controls reject |
| 124-program corpus | **2 changed** — `testing/universal/b3.c`, `u5_sieve.c` |
| both changed programs re-verified on the simulator | **PASS** (3 and 5 golden checks) |
| `pipeline_crosscheck` | **PASS 124/124** |
| unit suites (all 15) | **all pass** |

The two corpus changes are expected: the model is now more conservative, so a few
pairs are no longer proven independent and pack differently. Both programs were
re-verified end to end against their gcc golden references rather than assumed
benign.

## 8. Remaining work

1. **Find the disambiguation pair that is wrongly proven independent.** The
   symptom is reproducible in one command
   (`APARA_NO_MEMDISAMB=1` flips it), and it requires both consumers active.
2. Only then re-apply the sliding-window lowering, which is otherwise ready — its
   diff is one field on `ArrayRef`, one shared emitter, three call sites and one
   legality relaxation.
3. Re-examine profitability before enabling it: on the evidence above the
   reconstructed form can be slower than scalar.

**Do not enable the R6.3 lowering until step 1 is closed.** The disambiguation
defect is latent at HEAD only because convolution is declined; it is not
convolution-specific.

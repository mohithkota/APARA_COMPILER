# R6.2E — Dependence Soundness Investigation

**Conclusion: there is no missing dependence edge. The premise of this milestone
is not supported by the evidence, and my previous milestone's conclusion was
wrong.**

R6.3's sliding-window lowering does **not** compute correct results. It fails
with memory disambiguation **disabled** as well, so R6.2's dependence graph is
not implicated. No dependence was "restored", because none was shown to be
missing — fixing by guess would have been worse than reporting this.

The tree is unchanged at `c0330eb`; verification remains **38/38**.

---

## 1. Correcting the previous conclusion

R6.3's investigation concluded "the reconstruction is correct; the blocker is
memory disambiguation", on the strength of two kernels that check every output
element and pass. That conclusion was **too strong**, and this milestone
disproves it.

Those kernels read their outputs **sequentially** (`results[k] = out[k]`). The
failing suite kernel reads them **sparsely** (`out[0]`, `out[11]`, `out[60]`).
Sequential reads pass; sparse reads fail. I generalised from the passing shape
without testing the failing one, and reported a correct-looking result that was
an artifact of the diagnostic.

## 2. Minimal reproducer

```c
long long results[3];
int main(void){ vi8_t in[72],out[72]; int i;
  for(i=0;i<72;i++) in[i]=(vi8_t)(i&7);
  for(i=0;i<56;i++) out[i]=in[i]+in[i+1]+in[i+2];   /* 7 chunks, remainder 0 */
  results[0]=out[0]; results[1]=out[11]; results[2]=out[55];
  return 0; }
```

56 iterations = exactly 7 chunks, so there is no remainder and no peeling.

## 3. Simulator evidence

```
Info:  PostCondition Mem[0x80] = 0x3                        <- out[0]   CORRECT
Error: PostCondition Mem[0x81] = 0x0, expected 0xc          <- out[11]  ZERO
Error: PostCondition Mem[0x82] = 0x0, expected 0x8          <- out[55]  ZERO
```

`out[0]` is right; `out[11]` and `out[55]` read as **0**, i.e. as if never
written.

## 4. Why it is not a dependence problem

| experiment | result |
|---|---|
| same kernel, **56 results** (`results[k]=out[k]`, sequential) | **PASS** |
| same kernel, **3 results** (sparse) | **FAIL** |
| same kernel, 3 results, **disambiguation OFF** | **FAIL** |
| elementwise `c[i]=a[i]+b[i]`, identical sparse reads, disambiguation ON | **PASS** |
| elementwise vi16, identical sparse reads | **PASS** |

Two of these are decisive:

* **it fails with disambiguation OFF.** The 56-iteration case fails in *both*
  configurations, so R6.2's symbolic model cannot be the cause. The milestone's
  premise — "scheduler alone PASS, bundler alone PASS, together FAIL, therefore a
  missing dependence" — does not hold once the remainder is removed from the
  kernel.
* **an elementwise kernel with the identical sparse-read structure passes.** If a
  store→load dependence between a vector loop and later sparse reads were being
  dropped, elementwise would fail the same way. It does not. The defect is
  specific to the sliding-window lowering.

Hypotheses tested and eliminated:

| hypothesis | test | verdict |
|---|---|---|
| wrong disjointness proof | enumerated every same-block (store, access) pair the model clears, with symbolic addresses | none wrong on inspection — distinct arrays, distinct result words |
| scheduler vs bundler interaction | bundled one **fixed** program with `disambiguate` on and off, ran both | **both PASS** — the bundler is not the difference |
| different realisation | `compact` vs `compact+peeled` | a real difference, but `compact` fails too at 56 iterations |
| different optimization tier | printed the selected tier for the passing and failing shapes | **identical** (`IVSR+LICM+loop-reg`) |
| R3.1 software pipelining | checked whether SWP fired | **not applied** in either |

The earlier "scheduler alone PASS / bundler alone PASS / both FAIL" observation
has a mundane explanation: disabling the model in either consumer changes the
measured bundle counts, so R4.2.5's size probe selects a *different realisation*
— `compact` instead of `compact+peeled`. It was never evidence of an interaction
between the two consumers.

## 5. What remains unexplained

Why the number of *result reads* changes the outcome, when the tier, the
realisation, the vectorization decision and the bundling of a fixed program are
all identical. `out[0]` is correct while `out[11]` and `out[55]` are zero, which
looks like the later reads observing memory before the vector stores land — but
the scheduler works within a basic block only, and the reads are in a block after
the loop, so it cannot hoist them there.

That contradiction is the next thing to resolve, and it is where this
investigation ran out of evidence rather than out of hypotheses.

## 6. Compiler fix

**None.** No dependence was restored, because none was shown to be missing.
Restoring a conservative edge on suspicion would have made the symptom disappear
without establishing why, and would have cost real scheduling freedom on every
kernel for a defect that is not in the dependence graph.

## 7. Verification

The tree is unchanged at `c0330eb`.

| check | result |
|---|---|
| verification harness (simulator, 38 programs) | **38/38 PASS**, negative controls reject |
| R6.3 lowering | **not committed**, convolution remains declined |

## 8. Performance impact

None — nothing changed. R6.2C's cost stands: 8 convolution/2-D kernels declined,
`conv3` ticks 5854 → 9895.

The performance warning from R6.3 also stands and is worth repeating, because it
affects whether this work should continue at all: with disambiguation off, the
reconstructed conv 3-tap vi8 ran **1888 ticks against 1517 for the scalar form
the compiler currently ships**. Even once correct, the sliding-window form is
slower than scalar for this kernel — two aligned loads plus three ALU operations
per shifted tap. **The profitability question should be settled before more
correctness effort goes into it**; if the answer is that it is not profitable,
the right outcome is to leave convolution declined and close R6.3.

## 9. Recommended next step

1. Determine why the number of result reads changes the outcome, given that the
   tier, realisation and fixed-program bundling are identical. Diff the emitted
   mcode of the 3-result and 56-result builds around the vector loop and the
   reads — that comparison was not completed here.
2. Settle profitability first (§8). If the window form cannot beat scalar, close
   R6.3 and keep the R6.2C decline.

# R11 — Realisation-probe candidate rescue

Branched off **`r10-final`** (`930834e`). Fixes the one defect R10 documented as
future work: *"GEMM vi32 does not scale past M=16."*

Kill switch **`APARA_NO_PROBE_RESCUE=1`** restores the R10 behaviour.

**Result: gemm vi32 M=24 65 471 → 31 236 ticks (−52.3%). The shipped 38-program
suite is bit-identical — 0 programs changed.**

---

## 1. The defect — not what the R10 write-up guessed

R10 recorded the symptom and speculated the realisation probe was "picking by
static size when it should pick by ticks". Instrumenting the probe showed
something more specific:

```
case          chunks   probe result                      chosen
vi32 M=16          8   unrolled=55,      compact=59   -> unrolled
vi32 M=24         12   unrolled=68*SPILL compact=69   -> compact     <-- WRONG
vi32 M=32         16   unrolled=87*SPILL compact=97*  -> compact     <-- correct
```

At M=24 compact was **larger** than unrolled (69 > 68) yet still won — because
the unrolled candidate was **discarded outright** for spilling, so compact won by
default, not on size and not by the 10% margin.

**Root cause: `vector_size_probe` models TIER 1 ONLY, while production runs a
SEVEN-TIER ladder and simply steps down when tier 1 spills.** A candidate that
spills under the probe is therefore not unbuildable — it just gets a weaker tier.
Confirming this with both probes on the same slice:

| case | post-optimizer probe | plain backend probe |
|---|---|---|
| vi32 M=24 unrolled | (68, **SPILL**) | **(125, no spill)** |
| vi32 M=24 compact | (69, ok) | (158, ok) |
| vi32 M=32 unrolled | (87, **SPILL**) | (148, **SPILL**) |
| vi32 M=32 compact | (97, SPILL) | (204, ok) |

At M=24 the unrolled form builds cleanly under the plain backend and was thrown
away anyway. At M=32 it spills **both** ways, so it is genuinely unbuildable and
compact is the correct choice there.

**Why the wrong choice costs so much:** the compact realisation's body executes
`chunks` times per row while the unrolled body executes once, so vector-region
bundles explode (2 560 → 41 472 at M=24). vi32 hits this and vi16 does not
purely because of ISA granularity — a packed 64-bit word holds `8/elem_bytes`
lanes, so vi32 gets 2 lanes and needs `M/2` chunks where vi16 needs `M/4`.

## 2. The fix

Two changes in `vector_compact_loop.choose_smaller`, both small:

1. **Per-candidate rescue.** If the post-optimizer probe reports a spill or
   failure, re-measure that candidate with the plain backend before discarding
   it. This is exactly the rescue the existing R4.6.1 path already performs — but
   that one only fires when *every* candidate spills, which is why the mixed case
   slipped through.
2. **Scale consistency.** The two probes measure different things (68 vs 125
   bundles for the same slice), so once any candidate is rescued, **every**
   candidate is re-measured with the plain probe. Ranking on mixed scales would
   have favoured whichever candidate happened to be measured post-optimizer.

The incumbent rule, the 10% margin and the spill/differential gates are all
unchanged. No scheduler, bundler, vectorizer, codegen or legality change.

## 3. Measured effect

| case | chunks | R10 | R11 | |
|---|---|---|---|---|
| gemm vi16 M=16 / 24 / 32 | 4 / 6 / 8 | 4 375 / 10 750 / 19 982 | identical | unaffected |
| gemm vi32 M=16 | 8 | 4 893 | 4 893 | unaffected |
| **gemm vi32 M=24** | **12** | **65 471** | **31 236** | **−52.3%** |
| gemm vi32 M=32 | 16 | 148 975 | 148 975 | unchanged — correctly still compact |

Ticks per output element: vi32 M=24 **113.66 → 54.23**.

The fix repairs exactly the case that was broken and deliberately leaves the
other five alone, including the one where compact is genuinely right.

## 4. Correctness

| check | result |
|---|---|
| 38-program simulator suite | **38/38 PASS** |
| negative controls | **3/3 rejected** |
| unit suites | **21/21 PASS** |
| `pipeline_crosscheck` | **PASS — 124/124** identical |
| **shipped suite ticks** | **67 689 → 67 689, 0 programs changed** |
| gemm vi32 M=24/32 vs gcc golden | 3/3 PostConditions |
| new spills | none |

The shipped suite being **bit-identical** is the important one: R8.1a established
that probe changes can silently cost an unrelated kernel a transform. Here the
rescue only fires when a candidate spills under the post-optimizer probe, which
no shipped-suite kernel does.

## 5. Honest bounds

* This does **not** make vi32 scale as well as vi16. At M=24 it is 54.23
  ticks/output against vi16's 18.66 — still ~3× worse. The remaining gap is the
  2-lane granularity of 32-bit elements, which is architectural.
* **M=32 is not improved** and cannot be by this fix: the unrolled form spills
  under both probes, so compact is genuinely the better of two poor options. A
  realisation that unrolls partially (say 4 of 16 chunks) does not exist in the
  compiler and was not added.
* The fix is validated at M=16/24/32 for two element types. Other sizes and
  types are covered only by the shipped suite, which is unchanged.

## 6. Status

R10's `FINAL_EVALUATION.md` and `ARTIFACT.md` list "GEMM vi32 does not scale past
M=16" as the clearest future-work item. That item is now **partially closed**:
the M=24 cliff was a probe defect and is fixed; the M=32 behaviour is a genuine
architectural limit and remains.

The `r10-final` tag and its evaluation are untouched — this work lives on
`feature/r11-realisation-probe`.

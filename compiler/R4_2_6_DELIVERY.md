# R4.2.6 Delivery Report — Post-Optimizer Size Gate & Remainder Peeling

**Milestone:** R4.2.6 (close the two weaknesses R4.2.5 documented, before R4.3).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-31

> Two quality fixes to realisation SELECTION plus one new realisation. Only
> lowering and selection changed; `vector_pipeline.py` remains byte-for-byte
> untouched and a test asserts it. Both fixes were driven by measurement, and one
> of them **refuted the hypothesis it started from** — see §4.

---

## 1. Files added / modified
| File | Change |
|---|---|
| `vector_size_probe.py` | **NEW.** Measures a candidate as production would build it: tier-1 scalar optimizer (IVSR → SR → LICM → loop-reg → clean/SCCP/GVN/mem2reg/LICM) + R3.2 superblock, then bundling. Every pass imported from the module `compiler.py` imports it from, so it cannot drift. `APARA_VECTOR_FAST_PROBE=1` reverts to the cheap probe. |
| `vector_remainder_peel.py` | **NEW.** `PeelTemplate` + `build_peeled_tail` + `splice_peeled`: replaces the residual scalar tail loop with straight-line iterations at constant offsets, and sets the IV to `trip` (the deleted loop is what used to leave it there). |
| `_r4_2_6_test.py` | **NEW.** Unit suite (51 checks). |
| `vector_compact_loop.py` | Selector uses the post-optimizer probe; adds the acceptance MARGIN; `realisation_of` reports peeled variants. |
| `vector_lowering.py`, `vector_elementwise_lowering.py` | Capture a `PeelTemplate` during planning; offer `unrolled+peeled` and `compact+peeled` as extra candidates. |
| `vector_dynamic.py` | Peel-aware: a peeled tail executes once at a known length instead of `body_ops × remainder`. |
| `vector_compact_corpus.py` | Headline bundle count is now the post-optimizer one. |

## 2. Fix A — the probe measures what actually ships
R4.2.5 chose between realisations using `CodeGen` + `bundle_mcode` on the
vectorized IR alone, **before** the scalar optimizer, SWP and superblock ran.
Those passes favour straight-line code, so the ranking could invert after
lowering had committed. The documented case: a 4-loop program with one 8-chunk
vectorized loop finished at **67 bundles unrolled vs 69 compact**, and R4.2.5
picked compact.

With the post-optimizer probe that program now picks **67**. The probe also
changes the picture globally: measured post-optimizer, the corpus scalar baseline
is 276 bundles (not 336), R4.2 is 319 (not 354) and R4.2.6 is **273** — i.e. the
vectorized code is now genuinely *below* the scalar baseline, which the
pre-optimizer numbers could not show.

## 3. Fix B — an acceptance margin
The better probe immediately exposed a flaw in "smallest wins": on `vector add
vi8` (4 chunks) compact became smaller by **1 bundle of 31 (−3%)** while costing
**+47 executed operations (+168%)**. That is a bad trade taken for a rounding
error.

A challenger must now beat the incumbent (always the unrolled form, which is
dynamically fastest) by at least **10%** of its bundle count —
`APARA_VECTOR_COMPACT_MARGIN`. This keeps the large wins (8-chunk kernels save
13 of 43, −30%) and rejects the marginal ones, restoring the 89.6% dynamic
reduction that the unguarded selector had eroded to 87.1%.

## 4. Fix C — remainder peeling, and the hypothesis it refuted
**The hypothesis:** peeling would be a two-axis win — deleting the tail loop
removes its compare, branch and IV update, so both size and speed should improve.

**The measurement says otherwise.** At remainder 4 the peeled tail is 4 copies of
the body (~29 instructions) where the tail *loop* was ~10 skeleton instructions
plus one body. Peeling is dynamically faster (the tail executes ~29 operations
instead of ~88) but statically **larger** — it is the mirror image of compaction,
not an exception to it:

```
    add vi8 N=20 (post-optimizer bundles)   scalar 19
        unrolled          21      <- chosen
        unrolled+peeled   29
        compact           30
        compact+peeled    25
```

So peeling is subject to the same margin as every other challenger, and it is
chosen only where it genuinely shrinks the code — **2 of 6** remainder kernels:

```
    reduction vi16 N=30    unrolled 33  ->  compact+peeled 27   (-18%)
    vector mul vi16 N=30   unrolled 29  ->  compact+peeled 22   (-24%)
```

**A correction to the R4.2.5 report.** That report called `add vi8` N=20 a weak
case at "30 bundles vs a 23-bundle scalar baseline". Both figures were
pre-optimizer artifacts. Measured post-optimizer it is **21 vs 19** — vectorizing
costs 2 bundles, not 7. The weak case is real but far milder than stated.

**Why peeling is safe.** The tail is not re-derived from source — that would risk
getting integer promotion or sub-word truncation wrong (the class of bug that made
R4.1's narrow-accumulator dot diverge). Each planner records a `PeelTemplate`
holding the ORIGINAL instructions' `elem_bytes`, `unsigned` flags and opcode, and
peeling replays those at constant offsets. The differential then validates it like
any other lowering.

## 5. Results
```
  Corpus (20-case suite, post-optimizer bundles)
    coverage                 14 -> 14        preserved
    R4.2 (always unrolled)          319 bundles
    R4.2.6 (measured choice)        273 bundles     -46  (-14.4%)
    scalar baseline                 276 bundles     (vectorized now BELOW it)
    generated code size    24201 -> 19871 chars     -17.9%
    mismatches                        0             (100% differential)
    rollbacks                         1             (narrow acc, still caught)
    dynamic reduction              89.6%            (94.1% if never compacted)

  END-TO-END, full production optimizer (20 whole programs incl. 6 remainder)
    total bundles           698 -> 639              -59  (-8.5%)
    improved programs       6    (-7, -13, -13, -13 compaction; -7, -6 peeling)
    unchanged programs      14   (unrolled correctly kept)

  Full corpus (124 programs)
    scalar & byte-identical (on/off) : 124/124      NO REGRESSION
```

## 6. Test summary
```
_r4_2_6_test.py ...................... ALL R4.2.6 UNIT TESTS PASS  (51/51 checks)
  incl. 32 realisations validated across 8 kernels, 0 mismatches
_r4_2_5 / _r4_2 / _r4_1 / _r4_0 ...... PASS (unchanged)
_r3_1 / _r3_2 ........................ PASS (unchanged)
vector_compact_corpus.py ............. PASS
pipeline_crosscheck.py ............... 124/124 identical
```

## 7. Honest notes / limitations
- **The probe is now a good predictor, not an exact one.** It models tier 1 and
  superblock; production may select a different tier under spill pressure, and it
  does not model R3.1 SWP. Compile time rises ~10% (1.40 s → 1.53 s on the suite)
  because up to four candidates are each fully optimized.
- **Peeling helps a minority of kernels** (2 of 6 measured). It is offered, not
  assumed, and costs nothing where it loses.
- **Small-trip remainder kernels remain a mild static-size loss** (`add vi8` N=20:
  21 vs 19 scalar). No realisation fixes this — with 2 chunks there is nothing to
  compact and the tail is 20% of the elements. Declining to vectorize would fix
  the size at the cost of a 4.3× dynamic win; that is a policy call, not a
  code-generation one, and is left open.
- Validation remains the packed IR oracle (no hardware simulation, per policy).

# R12.1 — GEMM compact-unroll correctness fix

Branch `feature/r12-partial-unroll`, off `r11-verified`. Diagnosis in
`R12_1_GEMM_VI32_UNROLL_BUG_ANALYSIS.md`.

**One line of compiler code changed. GEMM vi32 M=32: 148 975 → 91 631 ticks
(−38.5%). Shipped 38-program suite bit-identical.**

---

## 1. Root cause

`gemm_lowering.build_compact.emit()` discarded the per-copy element index that
`vector_compact_loop.build_compact_chunk_loop` supplies, forwarding `None` to
`_row_body`. `clone_offset` then re-loaded the IV slot, so all U unrolled copies
re-derived the **same** address while the loop advanced by `U*lanes` — one chunk
accumulated U times, U−1 chunks never written.

Proven three ways (full detail in the diagnosis report): the framework's computed
index `_vci120` had **zero consumers**; the wrong-value pattern repeated with
period `U*lanes` as `[2×, 2×, 0, 0]`; and the first wrong addresses were
identical across U=2/4/8.

## 2. The fix

```diff
-        body, err = _row_body(plan, instrs, def_map, region, a_val, None)
+        body, err = _row_body(plan, instrs, def_map, region, a_val, iv_index)
```

Plus a comment recording the hazard. Nothing else: `vector_compact_loop.py`,
`clone_offset`, the selector, the profitability model, the scheduler, the
bundler, legality and every other vector client are untouched, and no GEMM
special case was introduced.

## 3. Phase 1 — U=1 regression check

Forced compact, U=1, mcode compared byte-for-byte pre-fix vs post-fix:

| type × size | result |
|---|---|
| vi8/vi16/vi32 × M=16/M=32 (6 configurations) | **6/6 byte-identical** |

Copy 0 receives the temp already holding the loaded IV, so substituting it is
equivalent to the previous re-load. U=1 behaviour is unchanged, as predicted.

## 4. Phase 2 — U≥2 correctness

Forced compact, differential oracle verdict, all 36 combinations:

| type | lanes | M=16 | M=24 | M=32 |
|---|---|---|---|---|
| vi8 | 8 | U=1,2,4,8 **all match** | all match | all match |
| vi16 | 4 | all match | all match | all match |
| vi32 | 2 | all match | all match | all match |

**Before the fix every U≥2 entry was `mismatch`. After it, all 36 match.**

## 5. Phases 4/6 — GEMM vi32 measurements

Simulator ticks, and the selector's own choice:

| M | selector (auto) | compact U=1 | U=2 | U=4 | U=8 |
|---|---|---|---|---|---|
| 16 | **4 893** (19.11 t/out) | 20 750 | 15 630 | 13 582 | 14 640 |
| 24 | **31 236** (54.23 t/out) | 65 471 | 48 191 | 41 279 | 41 279 |
| **32** | **91 631** (89.48 t/out) | 148 975 | 108 015 | **91 631** | 93 745 |

The selector's own estimates at M=32 (frequency-weighted dynamic bundles):

| U | estimate | |
|---|---|---|
| 1 | 167 423 | |
| 2 | 118 271 | |
| **4** | **97 791** | **chosen** |
| 8 | 97 856 | |

**The estimator ranked U=4 lowest and the simulator agrees** (U=4 91 631 <
U=8 93 745). The existing selector found the best factor unaided — no
hard-coding, no profitability change.

Selected-build metrics:

| M | realisation | vector IPB | occupancy | vector bundles | static bundles | spills | peak regs |
|---|---|---|---|---|---|---|---|
| 16 | unrolled | 4.900 | 61.3% | 2 560 | 146 | none | 29 |
| 24 | unrolled | 5.179 | 64.7% | 16 128 | 304 | none | 29 |
| 32 | **compact U=4** | 2.562 | 32.0% | 65 536 | 164 | none | 30 |

## 6. Phase 5 — R11 compatibility

| M=24 | ticks |
|---|---|
| R10 | 65 471 |
| R11 (probe rescue) | **31 236** |
| **R12.1** | **31 236 — preserved** |

M=16 is likewise unchanged at 4 893. At both sizes the unrolled realisation still
wins outright, so the compact path is not selected and the fix cannot affect
them.

## 7. Phase 3 — full validation

| check | result |
|---|---|
| 38-program simulator suite | **38/38 PASS** |
| negative controls | **3/3 rejected** |
| unit suites | **21/21 PASS** |
| `pipeline_crosscheck` | **PASS — 124/124** identical |
| **shipped suite ticks** | **67 689 → 67 689, 0 programs changed** |
| GEMM vs gcc golden (all sizes/types measured) | 3/3 PostConditions |
| new spills | none |

The shipped suite is bit-identical because no shipped kernel selects the compact
GEMM realisation — M=16 chooses unrolled. The fix is reachable only at sizes
outside the suite, which is exactly where the bug was.

## 8. Is partial unrolling actually profitable?

**Yes, at M=32 — and only there.**

* **M=32: U=4 beats U=1 by 38.5%** (148 975 → 91 631; 145.48 → 89.48
  ticks/output). This is the first *valid* measurement of U≥2 for GEMM, because
  before the fix no correct U≥2 build existed at any size.
* **M=16 and M=24: unchanged.** Correct U≥2 compact builds now exist there too
  (U=4 gives 13 582 and 41 279), but the unrolled realisation still beats all of
  them, so the selector keeps it. Making U≥2 correct did not make it *win* where
  a better option already existed — the caution in the diagnosis was warranted.

Honest bound: M=32 remains far from vi16's efficiency (89.48 vs 19.51
ticks/output) and its vector-region IPB is 2.562 against 4.900 at M=16. The
compact realisation still pays a loop per U chunks, and 32-bit elements still get
only 2 lanes per packed word. **The cliff is substantially reduced, not
removed** — the residual is the architectural granularity identified in R12.0.

---

**R12.1 CORRECTNESS FIX — VERIFIED**

**M=32 now benefits from U≥2: the selector chooses U=4 and measures 38.5% faster
than the U=1 it was previously forced onto.** M=16 and M=24 are unchanged, and
the shipped suite is bit-identical.

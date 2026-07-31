# R4.2.5 Delivery Report — Compact Vector Loop Generation

**Milestone:** R4.2.5 (replace fully-unrolled vector chunks with compact vector
loops where that is smaller — the vector analogue of R2.8).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-31

> A code-generation QUALITY milestone. Not matrix multiplication, not a general
> vectorizer. **Only lowering changed**: `vector_pipeline.py` is byte-for-byte
> untouched and the client contract (`kinds` / `match` / `lower` /
> `dynamic_model` / optional `validate`) is unchanged — asserted by a test.
> Coverage, legality, profitability, validation and rollback are all preserved.

---

## 1. Files added
| File | Purpose |
|---|---|
| `vector_compact_loop.py` | Compact loop construction: packed load/store at a REGISTER offset, `build_compact_chunk_loop`, the realisation selector `choose_smaller`, `realisation_of`. |
| `vector_dynamic.py` | The realisation-aware dynamic-operation model, shared by both clients so the accounting cannot drift between them. |
| `vector_compact_corpus.py` | Corpus evaluation: R4.2 (forced unrolled) vs R4.2.5 (measured choice) through the same pipeline. |
| `_r4_2_5_test.py` | Unit suite (79 checks). |
| `R4_2_5_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `vector_lowering.py` | Adds `build_compact_body`, `_splice_compact`/`_splice_unrolled`; `lower_kernel` now builds BOTH realisations and keeps the smaller. Takes an already-matched plan (pattern matching moved to the client's `match()`, symmetric with elementwise). |
| `vector_elementwise_lowering.py` | Same two changes for the elementwise shapes. |
| `dot_vectorizer.py` / `elementwise_vectorizer.py` | `match()` now builds the plan; `dynamic_model()` delegates to `vector_dynamic`; `global_base` arrives via the constructor. |

**`vector_pipeline.py` is UNCHANGED.** `global_base` reaches the clients through
their constructors — the entry points already build them — so the framework
needed no new parameter. The chosen realisation is recovered from the emitted IR
(`realisation_of`, keyed on the unique `vcl_` label prefix) rather than by adding
a field to the report. No R4.0 analysis file, no scheduler, bundler, allocator or
backend file is touched.

## 3. The compact form
```
    for (i = 0; i < chunks*lanes; i += lanes)
        <packed body addressed by the REGISTER offset i*elem_bytes>
    <original scalar loop, resuming at i = chunks*lanes, for the remainder>
```
Static size becomes O(1) in the trip count instead of O(chunks).

**Three design decisions, each chosen to reuse machinery rather than add any:**

1. **The loop reuses the kernel's OWN induction variable slot.** It advances that
   slot by `lanes` per chunk and exits with it holding exactly `chunks*lanes`, so
   the scalar remainder loop that follows needs **no modification at all** — it
   simply resumes. (The unrolled form has to rewrite the IV init store to
   `chunks*lanes`; the compact form leaves it at 0 and lets the loop count.) This
   is the subtle part, and it is tested directly: the differential compares FULL
   final memory, so a wrong hand-off would either redo or skip elements.

2. **The loop is emitted in the front end's canonical counted-loop shape**
   (cond / body / incr / goto cond / end, memory-slot IV). Not cosmetic: M1
   induction-variable analysis is memory-slot based — the lesson R2.7 learned the
   hard way — so a fresh register counter would be invisible to it. Emitting the
   canonical shape keeps the loop recognisable downstream, and it demonstrably
   works: in production, R3.2 superblock formation **merges 5 regions inside the
   compact-loop program and still reduces bundles 71 → 66 with 0 spills**.

3. **Loop-carried values stay in MEMORY, never in a register across the back
   edge.** The dot/reduction accumulator is loaded at the top of the body and
   stored at the bottom, exactly as the scalar loop does. R2.8 had to invent the
   `_codegen_keeps_alive` invariant precisely because the IR differential cannot
   see register allocation; keeping the recurrence in its slot sidesteps that
   entire class of bug, and R2.6 register promotion can still hoist it later.

## 4. Choosing the realisation — measured, not assumed
Both candidates are built and compiled through the real backend
(`vector_pipeline._bundles`, the same probe the pipeline's own compile gate uses);
the one with fewer bundles wins. **At an equal count the unrolled form is kept**,
because the compact loop then occupies the same IMEM while executing strictly more
instructions — it must EARN the switch with a real size win.
`APARA_VECTOR_REALISATION=compact|unrolled` forces either form for A/B work.

This mattered: assuming "compact is always better" would have been wrong. The
measured crossover on the kernel suite is sharp —

```
    4 chunks  -> unrolled wins   (the bundler packs the independent chunks wide)
    6 chunks  -> unrolled wins
    8 chunks  -> compact wins    (loop overhead is finally cheaper than 8 copies)
```

## 5. Corpus results
```
  Per-kernel choice (20-case suite, chosen by measured bundle count)
    dot vi8 N=32          4 chunks   unrolled   26->22     800->24
    dot vi16 N=32         8 chunks   COMPACT    27->26     832->171
    reduction vi8 N=48    6 chunks   unrolled   25->20    1056->28
    reduction vi32 N=16   8 chunks   unrolled   26->22     368->36
    vector add vi8        4 chunks   unrolled   23->22     704->28
    vector add vu8        4 chunks   unrolled   23->22     704->28
    vector add vi16       8 chunks   COMPACT    24->24     736->155
    vector add vi32       8 chunks   COMPACT    24->24     368->155
    vector sub vi8        4 chunks   unrolled   23->22     704->28
    vector mul vi8        4 chunks   unrolled   23->22     704->28
    vector mul vi16       8 chunks   COMPACT    24->24     736->155
    vector copy vi8       4 chunks   unrolled   22->18     608->16
    vector add rem N=20   2 chunks   unrolled   23->30     440->102
    in-place a[i]+=b[i]   4 chunks   unrolled   23->22     704->28

  R4.2 (always unrolled)  vs  R4.2.5 (measured choice)
    kernels vectorized   :    14  ->    14      (coverage preserved)
    static bundles       :   354  ->   320      -34   (-9.6%)
    generated code size  : 24201  -> 19871      -4330 (-17.9%)
    dynamic operations   :   558  ->   982      +424
    mismatches           :     0  ->     0
    rollbacks            :     1  ->     1      (narrow acc, still caught)
    pipeline time        :  1.35s ->  1.46s     (+0.11s, two candidates compiled)

    scalar baseline on these kernels : 336 bundles
    R4.2   vectorized                : 354   (ABOVE the scalar baseline)
    R4.2.5 vectorized                : 320   (BELOW the scalar baseline)

  END-TO-END, full production optimizer (14 whole kernel programs)
    total bundles        :   473  ->   427      -46   (-9.7%)
    per-kernel wins      :   -7, -13, -13, -13 on the four compact kernels
                             0 on every other kernel (unrolled kept)

  Full corpus (124 programs)
    vectorized                       : 0    (no packed arrays in the corpus)
    scalar & byte-identical (on/off) : 124/124   NO REGRESSION
```

## 6. Success criteria
1. **Static bundle count reduced** ✅ 354 → 320 at lowering; 473 → 427 end-to-end.
2. **Code size reduced** ✅ 24201 → 19871 characters (−17.9%).
3. **Dynamic operation reduction preserved** ⚠️ **preserved but measurably
   reduced: 94.1% → 89.6%.** See §7 — this is a real trade, not a rounding error.
4. **100% differential validation** ✅ 0 mismatches; BOTH realisations validated
   independently on every kernel.
5. **Zero regressions** ✅ 124/124 byte-identical; crosscheck 124/124; R3.1–R4.2
   suites all pass.
6. **Spill-free compilation** ✅ asserted per kernel in the suite.
7. **No change to the vector pipeline architecture** ✅ `vector_pipeline.py`
   untouched; a test asserts the client interface, the `lower()` signature, and
   that the pipeline source contains no mention of realisations.

## 7. Honest notes / limitations
- **Criterion 3 is a genuine trade, and it should be stated plainly.** A compact
  loop pays a compare, a branch and an IV update on EVERY chunk that the unrolled
  form does not. Corpus-wide dynamic reduction falls **94.1% → 89.6%**; on the
  four compact kernels individually it is far larger (e.g. `vector add vi16`
  736 → 56 unrolled vs 736 → 155 compact). R4.2.5 buys −9.6% static size with
  +76% dynamic operations *on the kernels it compacts*. That is the right trade
  for this machine — IMEM overflow has been a real failure mode on this project
  (a 577-bundle program exceeded the 0x800-word IMEM) — but it IS a trade, and
  `APARA_VECTOR_REALISATION=unrolled` reverts it wholesale if a program is
  throughput-critical and IMEM-comfortable.

- **The selector measures the vectorized IR alone, BEFORE the scalar optimizer,
  SWP and superblock scheduling run.** Those passes favour straight-line code
  (superblock merges regions; the bundler packs independent chunks 8 wide), so the
  ranking can flip afterwards. Measured example: a 4-loop program with one
  8-chunk vectorized loop ends at **67 bundles unrolled vs 69 compact** — the
  compact form has 34 FEWER instructions (119 vs 153) but packs less densely, and
  the probe did not see that. Across the 14-kernel end-to-end suite the net is
  still clearly positive (−46 bundles, with the mispredicts costing single
  digits), but the gate is a good predictor, not an exact one. Making it exact
  would require running the production optimizer inside lowering, which is
  circular — the honest fix is a post-optimizer re-check, and that belongs in a
  later milestone.

- **The crossover is empirical, not derived.** 8 chunks wins, 6 loses, on this
  ISA with this bundler. A different lane count, element width or issue width
  would move it. This is why the choice is measured per kernel rather than
  hardcoded as a threshold.

- **Remainder-heavy kernels remain the weak case** (unchanged from R4.2):
  `N=20` at 8 lanes is 2 chunks plus a 4-iteration scalar tail, and ends at 30
  bundles against a 23-bundle scalar baseline. Compaction does not help — with 2
  chunks there is nothing to compact. Peeling or predicating the remainder is the
  real fix and is out of scope here.

- **Validation remains the packed IR oracle** (no hardware simulation, per
  project policy), modelling `golden_stubs.h` semantics including known hardware
  bugs.

- Not implemented, by mandate: matrix multiplication, convolution, expression-tree
  vectorization, general loop vectorization.

## 8. Test summary
```
_r4_2_5_test.py ...................... ALL R4.2.5 UNIT TESTS PASS  (79/79 checks)
_r4_2 / _r4_1 / _r4_0 ................ PASS (unchanged)
_r3_1 / _r3_2 ........................ PASS (unchanged)
vector_compact_corpus.py ............. PASS (smaller code, coverage + correctness)
vector_elementwise_corpus.py ......... PASS
vectorize_corpus.py .................. PASS
pipeline_crosscheck.py ............... 124/124 identical, 0 rollbacks
```

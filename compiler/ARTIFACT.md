# ARTIFACT — APARA Vectorizing Compiler

**Frozen artifact for the R5.0 final evaluation.**

| field | value |
|---|---|
| repository | `/home/mohithkota/complier_Apara/cmp_wd` (local-only; no remote publication) |
| branch | `feature/vector-compiler` |
| evaluation base commit | `cf33ad4` (tag `r4.6.5-evaluated`) |
| artifact commit | this commit (tag `r5.0-final`) |
| date | 2026-07-31 |
| Python | 3.12.3 (GCC 13.3.0) |
| pycparser | 3.00 |
| git | 2.43.0 |
| OS | Linux 6.8.0-136-generic |
| external deps | **pycparser only** — no numpy, no matplotlib, no plotting library |

## Freeze statement
No scheduler, bundler, vectorizer, backend, IR, legality, profitability,
expression-tree, remainder-framework or optimization-pass file was modified in
R5.0. The only code changed was `evaluation/plots.py` (three additional figures —
reporting), plus regenerated artifacts and documentation.

## Verification performed at freeze

| check | result |
|---|---|
| unit suites (13) | **all pass** — R3.1, R3.2, R4.0, R4.1, R4.2, R4.2.5, R4.2.6, R4.2.8, R4.3, R4.4, R4.4.5, R4.5, R4.6 |
| corpora (8) | **all PASS** — conv, expression, gemm, axpy, compact, elementwise, dot, affine |
| `pipeline_crosscheck.py` | **PASS**, 124/124 identical |
| regression corpus | 124 programs, scalar output byte-identical with vectorization on/off |
| differential mismatches | **0** across every corpus |
| rollbacks in the benchmark suite | **0** |
| evaluation reproducibility | two consecutive runs produce identical numbers |

## Milestone history (this branch)

```
  R2.1 dependence graph          R3.0 oracle ILP analyzer
  R2.2 memory disambiguation     R3.1 production software pipelining
  R2.3 IR scheduler              R3.2 superblock scheduling
  R2.4 scheduler quality         R4.0 vector infrastructure
  R2.5 software pipelining       R4.1 dot / reduction
  R2.6 loop register promotion   R4.2 generic pipeline + elementwise
  R2.7 register-aware SWP        R4.2.5 compact vector loops
  R2.8 modulo variable expansion R4.2.6 post-optimizer gate + peeling
                                 R4.2.8 affine access recognition
  R4.3 AXPY                      R4.4 packed GEMM
  R4.4.5 generalized peeling     R4.5 expression trees
  R4.6 convolution               R4.6.1 2-D stencil recognition
  R4.6.5 performance characterization
  R5.0 final evaluation + freeze
```

Tags: `r4.1-verified` … `r4.6.5-evaluated`, `r5.0-final`.

## Key result

```
  coverage            21/21 vectorizable kernels, 0 rollbacks
  dynamic operations  28544 -> 4231   (-85.2%)
  IPB                 1.873 -> 2.462  (+31.5%), 30.8% of the 8-wide issue width
  static cost         bundles +12.1%, code +57.4%
  remaining gap       74% intrinsic data dependence; 0% compiler-fixable
```

---

# R10 FINAL FREEZE (supersedes the R5.0 freeze above)

The R5.0 section above is retained as the historical record of that artifact.
The compiler was **not** frozen there — milestones R6 through R9.5 followed, and
the validation policy changed fundamentally: from R6.2A onward every correctness
and performance claim is a **measured simulator run against a gcc golden
reference**, not an IR-level model.

| field | value |
|---|---|
| **final commit** | **`df3d49a`** |
| **final tag** | **`r10-final`** (evaluation freeze); `r9.5-verified` = last code change |
| branch | `feature/vector-backend-r6` |
| date | 2026-08-15 |
| Python / pycparser / git | 3.12.3 / 3.00 / 2.43.0 |
| OS | Linux 6.8.0-136-generic x86_64, glibc 2.39 |
| external deps | pycparser only; simulator toolchain is external (`APARA_TOOLS`) |

## Verification at the R10 freeze

| check | result |
|---|---|
| 38-program simulator suite (gcc golden) | **38/38 PASS** |
| negative controls | **3/3 rejected** |
| unit suites | **21/21 PASS** |
| `pipeline_crosscheck` | **PASS — 124/124** identical |
| register spills | **0** |
| tracked `git status` | **clean** |

## Final result

```
  suite ticks             210359 -> 67689   (-67.8%, 3.11x)
  whole-program IPB       2.213 weighted; min 1.260 median 1.663 max 3.506
  vector-region IPB       3.659 aggregate; best 6.000 (reduction vi32)
  best throughput         6.48 ticks/output element (conv3 vi8)
  executed pad bundles    34507 -> 625      (-98.2%, R9.5)
  IMEM cost of R9.5       +4.4%
```

**Density, throughput and execution time are three different things** and this
artifact reports them separately. R9.5 improved execution time 37.88% while
leaving vector-region density bit-identical; `axpy vi16` has 3× lower IPB than
`axpy vi8` and runs faster. **The optimization target is ticks per output
element, not IPB.**

## Milestones after R5.0

```
  R6.1 ILP analysis            R6.2 memory disambiguation + verification
  R6.3 sliding window          R6.4 vector unrolling (adaptive)
  R6.5 cross-iteration sched   R6.6 accumulator expansion
  R6.7 region superblock       R6.8 vector SWP
  R7.1 rematerialization       R8.0 wide memory (stopped, disproved)
  R9.0 bottleneck analysis     R9.1 address value numbering
  R9.2 branch-immediate fold   R9.3 GEMM row base + [reg+imm]
  R9.4 bottleneck re-analysis  R9.5 alignment-aware bundle formation
  R10  final throughput evaluation + freeze
```

Tags: `r4.1-verified` … `r5.0-final`, `r9.2-verified`, `r9.3-verified`,
`r9.5-verified`, `r10-final`.

## Known limitations at the R10 freeze

Superseding the R5.0 list below in one respect — **correctness evidence is no
longer IR-level; it is simulator-measured against gcc**. Still true: 2-D arrays
are never packed, canonical i-j-k matmul is unsupported, the induction variable
must start at zero, expression depth is capped at 8, `scalar − vector` is
refused, tap-innermost convolution is unrecognised.

New at R10, measured and documented rather than fixed:

* **74.5% of suite dynamic bundles are harness initialisation scaffolding.**
* **GEMM vi32 does not scale past M=16** — ticks/output 19.11 → 145.48 at M=32.
  The clearest future-work item; not investigated, because R10 is a freeze.
* 34 bundles of scheduler slack across 12 hot blocks.
* Two gated defects from earlier milestones remain unreachable in shipped output
  (R6.8 pipelined-AXPY behind `APARA_VSWP_UNBLOCK`; the R7.1 latent
  non-termination).

See `FINAL_EVALUATION.md`, `R10_FINAL_THROUGHPUT_EVALUATION.md` and
`REPRODUCIBILITY.md`.

---

## Layout

```
  compiler/                     the compiler (frozen)
    vector_pipeline.py          the one vector pipeline
    vector_affine.py            the one address recognizer
    expression_tree.py          the one expression representation
    expression_lowering.py      vector + scalar evaluators
    vector_remainder_peel.py    the one remainder framework
    {dot,elementwise,axpy,gemm,conv}_vectorizer.py   clients
    _r*_test.py                 21 unit suites
    *_corpus.py                 8 corpora
    verification/               R6.2A+ simulator harness (gcc golden references)
    vector_backend/             R6.1 ILP / occupancy / memory-dependence analysis
    evaluation/                 R4.6.5/R5.0 static evaluation harness
    R*_DELIVERY.md              per-milestone reports
    R4_6_5_EVALUATION.md        performance characterization
    R5_0_FINAL_EVALUATION.md    the R5.0 thesis evaluation
    R10_FINAL_THROUGHPUT_EVALUATION.md   the FINAL evaluation
    FINAL_EVALUATION.md         final summary sheet
    REPRODUCIBILITY.md          environment, commands, kill switches
    STATUS.md                   full engineering history (~6500 lines)
```

## Known limitations
See `R5_0_FINAL_EVALUATION.md` §11 (limitations) and §13 (threats to validity).
Nothing is hidden: 2-D arrays are never packed, canonical i-j-k matmul is
unsupported, the induction variable must start at zero, expression depth is capped
at 8, `scalar − vector` is refused, tap-innermost convolution is unrecognised, and
all correctness evidence is IR-level rather than hardware-simulated.

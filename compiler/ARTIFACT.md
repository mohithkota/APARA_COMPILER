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

## Layout

```
  compiler/                     the compiler (frozen)
    vector_pipeline.py          the one vector pipeline
    vector_affine.py            the one address recognizer
    expression_tree.py          the one expression representation
    expression_lowering.py      vector + scalar evaluators
    vector_remainder_peel.py    the one remainder framework
    {dot,elementwise,axpy,gemm,conv}_vectorizer.py   clients
    _r*_test.py                 13 unit suites
    *_corpus.py                 8 corpora
    evaluation/                 R4.6.5/R5.0 evaluation harness
    R*_DELIVERY.md              per-milestone reports
    R4_6_5_EVALUATION.md        performance characterization
    R5_0_FINAL_EVALUATION.md    the thesis evaluation
    STATUS.md                   full engineering history (~5000 lines)
```

## Known limitations
See `R5_0_FINAL_EVALUATION.md` §11 (limitations) and §13 (threats to validity).
Nothing is hidden: 2-D arrays are never packed, canonical i-j-k matmul is
unsupported, the induction variable must start at zero, expression depth is capped
at 8, `scalar − vector` is refused, tap-innermost convolution is unrecognised, and
all correctness evidence is IR-level rather than hardware-simulated.

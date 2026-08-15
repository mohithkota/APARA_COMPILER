# REPRODUCIBILITY

Covers both evaluation layers:

* the **R5.0 static evaluation harness** (one command, no toolchain needed), and
* the **R6.2A+ simulator verification** used for every performance number from
  R6.2A through the R10 freeze.

## 0. Frozen identity (R10)

| field | value |
|---|---|
| commit | `df3d49abbec923ba3f4219b6f3b57dc9e5ca070b` (`df3d49a`) |
| tags | `r10-final` (evaluation freeze), `r9.5-verified` (last code change) |
| branch | `feature/vector-backend-r6` |
| repository | `/home/mohithkota/complier_Apara/cmp_wd` — local only, never pushed |

## Requirements

| dependency | version used | notes |
|---|---|---|
| Python | 3.12.3 | any 3.8+ should work |
| pycparser | 3.00 | the **only** third-party package |
| git | 2.43.0 | for artifact verification only |
| gcc | system | required for simulator verification (golden references) |

**No numpy, no matplotlib, no pandas.** Figures are emitted as hand-written SVG
and ASCII so the artifact has no plotting dependency.

```bash
pip install pycparser
```

OS used: Linux 6.8.0-136-generic x86_64, glibc 2.39.

## Toolchain (simulator layer only)

External to this repository:

```
/home/mohithkota/complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/
    engine_isp/assembler/bin/{mcode_align,mcode_assemble,mcode_run}
```

Override with `APARA_TOOLS=/path/to/engine_isp/assembler/bin`. The harness checks
for all three and fails loudly if any is missing.

## Regenerate the entire static evaluation

From the repository root:

```bash
python3 compiler/evaluation/__main__.py
```

Runtime ≈ 5 s. It prints the full report and writes:

```
  compiler/evaluation/results/
    benchmarks.csv           one row per benchmark, every raw metric
    summary_by_family.csv    per-family means
    gap_analysis.csv         lost slots per cause
    gap_detail.csv           lost slots per benchmark (audit trail)
    report.txt               the printed report
  compiler/evaluation/plots/
    coverage.svg  dynamic_reduction.svg  ipb_per_benchmark.svg
    ipb_scalar_vs_vector.svg  oracle_comparison.svg  gap_causes.svg
    ipb_distribution.txt
```

Every table and figure in `R5_0_FINAL_EVALUATION.md` / `R4_6_5_EVALUATION.md`
derives from `results/benchmarks.csv`; nothing is transcribed by hand.

## Simulator verification — the R10 performance numbers

Run from `compiler/`. Every benchmark is compiled, aligned, assembled, simulated
and checked against an **independently gcc-compiled reference**; a benchmark that
cannot produce a reference is a FAILURE, never a skip. Three negative controls
prove the harness cannot pass vacuously.

```bash
python3 -m verification                      # PASS, 38/38, 3/3 controls rejected
python3 -m verification --csv metrics.csv    # per-program dynamic metrics
python3 -m verification --quick              # one marker per family
python3 -m verification --no-vectorize       # scalar baseline
```

Metrics come from the simulator's own counters (`Stopped after N ticks`,
`number of non-null instructions executed`). One bundle issues per tick, so
executed bundles = ticks.

## Verify the compiler itself

```bash
cd compiler

# 21 unit suites
for t in _r3_1 _r3_2 _r4_0 _r4_1 _r4_2 _r4_2_5 _r4_2_6 _r4_2_8 _r4_3 \
         _r4_4 _r4_4_5 _r4_5 _r4_6 _r6_1 _r6_2 _r6_6 _r6_8 _r7_1 _r9_1 _r9_2; do
  python3 ${t}_test.py | tail -1
done
python3 loopopt/_r3_0_test.py | tail -1

# 8 corpora (each ends with a RESULT line)
for c in vectorize_corpus.py vector_elementwise_corpus.py vector_compact_corpus.py \
         affine_corpus.py axpy_corpus.py gemm_corpus.py expression_corpus.py \
         conv_corpus.py; do
  python3 $c | grep RESULT
done

# whole-pipeline equivalence over the 124-program corpus
cd .. && python3 compiler/loopopt/pipeline_crosscheck.py | grep RESULT
```

Expected: 21 × `ALL … TESTS PASS`, 8 × `RESULT: PASS`, and `RESULT: PASS` for the
crosscheck (124/124 identical). The corpora dominate the ≈15 min runtime because
each compiles the 124-program regression set.

## Compile a single program

```bash
python3 compiler/compiler.py program.c -o program.mcode -v
```

## Kill switches

Each disables one optimization so its contribution can be measured directly. The
R9.x switches were verified **byte-identical** to the preceding tagged commit.

| variable | disables | milestone |
|---|---|---|
| `APARA_NO_ALIGN_BUNDLE=1` | alignment-aware bundle widening | R9.5 |
| `APARA_NO_GEMM_REG_IMM=1` | GEMM row base + `[reg+imm]` | R9.3 |
| `APARA_NO_AVN=1` | address value numbering (GVN stays on) | R9.1 |
| `APARA_NO_MEMDISAMB=1` | R6.2 symbolic memory disambiguation | R6.2 |
| `APARA_NO_VECTORIZE=1` | the whole vector path | R4 |
| `APARA_NO_SWP=1`, `APARA_NO_SUPERBLOCK=1` | R3.1 / R3.2 | R3 |
| `APARA_NO_IVSR`, `APARA_NO_LICM`, `APARA_NO_GVN`, `APARA_NO_MEM2REG`, `APARA_NO_DCE`, `APARA_NO_COPYPROP`, `APARA_NO_COALESCE`, `APARA_NO_STRENGTH_REDUCE`, `APARA_NO_LOOPOPT` | the named pass | various |
| `APARA_VECTOR_REALISATION=unrolled\|compact\|unrolled+peeled\|compact+peeled` | force one realisation | R4.2.5 |
| `APARA_VECTOR_COMPACT_MARGIN=<float>` | realisation acceptance margin (default 0.10) | R4.2.6 |

Diagnostics: `APARA_BUNDLE_STATS=1` (split reasons + widening count),
`APARA_GVN_DEBUG=1`, `APARA_MEM2REG_DEBUG=1`, `APARA_IVSR_DEBUG=1`,
`APARA_LICM_DEBUG=1`, `APARA_VECTOR_REPORT=1` (with `-v`).

## Reproducing the R10 headline numbers

| number | how |
|---|---|
| suite 67 689 ticks, weighted IPB 2.213 | `python3 -m verification --csv m.csv`; sum `ticks` and `non_null_instructions` |
| R9.5 contribution (−37.88%) | rerun with and without `APARA_NO_ALIGN_BUNDLE=1` |
| R9.3 contribution (−17.09%) | rerun with and without `APARA_NO_GEMM_REG_IMM=1` |
| vector-region IPB 3.659 | `vector_backend/ilp_analysis.py` + `occupancy.analyze_mcode`, vector subset, dynamic |
| oracle ceilings | `loopopt.oracle_ilp.analyze_module`, as `evaluation/metrics.oracle_of` calls it |
| executed pad bundles | count `pad_*` labels in `*.aligned.mcode`, weight by `ilp_analysis.label_frequencies` |

## Determinism

Compilation is deterministic: two consecutive runs produce identical mcode,
because the vectorizer resets its fresh-temp and label counters per module.
Verified for the static harness by diffing `results/benchmarks.csv` across two
runs — **the only column that differs is `compile_seconds`**. `pipeline_crosscheck`
resets every pass's counter before each arm so temp numbering is comparable.
Simulator ticks are exact counts, not wall-clock, so they reproduce across
machines with the same toolchain.

## What is *not* reproducible here, and caveats

* **Real hardware.** Numbers come from `mcode_run`, the ISA simulator, not
  silicon.
  *(This section previously said cycle counts were not reproducible at all,
  because the pre-R6.2A validation policy was IR-level. That has been superseded:
  from R6.2A onward every performance number is a measured simulator run.)*
* **The 124-program regression corpus** lives under `testing/`,
  `new_isa_tests/`, `demo_prof/` and `isa_coverage_tests/`; the corpora scripts
  discover it by glob. Build artifacts there are recreated by running the
  compiler.
* **Frequency-weighted pad estimates** run ≈0.87× measured ticks — only
  arm-to-arm deltas from them are quoted.
* **The R3.0 oracle ceiling** is computed on the **scalar** IR and is not an exact
  hardware bound for vector code.
* **The kernel-dominated tables** in `R10_FINAL_THROUGHPUT_EVALUATION.md` §6 use
  locally scaled templates; internally comparable, not bit-comparable with the
  shipped 38-program suite.

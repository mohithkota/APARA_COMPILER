# APARA_COMPILER

An optimizing **vectorizing C compiler** for the APARA accelerator's custom 8-wide VLIW ISA.
It takes preprocessed C and emits APARA mcode, which the accelerator's own toolchain aligns,
assembles and runs.

Two things distinguish it:

* **Automatic vectorization** of six kernel families (elementwise, AXPY, dot, reduction,
  convolution, packed GEMM) onto the ISA's packed `$v` instructions.
* **Correctness by independent oracle.** Every benchmark is compiled a second time with `gcc`
  and checked value-by-value on the real simulator. A pass means *"produced the right
  numbers"*, not *"did not crash"*. Three negative controls prove the harness cannot pass
  vacuously.

The **frozen thesis artifact is the tag [`r10-final`](../../tree/r10-final)**. `main` carries
that plus two validated post-freeze fixes (R11, R12.1).

---

## Final results

Measured on the 38-program simulator suite at `r10-final`, with the real
`mcode_align` → `mcode_assemble` → `mcode_run` toolchain.

| | |
|---|---|
| **Suite execution time** | **210 359 → 67 689 ticks (−67.8%, 3.11×)** |
| R9.5 alone (alignment-aware bundling) | 108 960 → 67 689 (−37.88%), all 38 kernels improved |
| Verification | **38/38 simulator · 3/3 negative controls · 21/21 unit suites · 124/124 crosscheck** |
| Register spills | 0 |

Instruction density, reported separately from throughput:

| metric | value |
|---|---|
| whole-program weighted IPB | **2.213** |
| vector-region IPB (aggregate) | **3.659** |
| best single kernel (`reduction vi32`) | **6.000** — 100% of its R3.0 oracle ceiling |
| best throughput (`conv3 vi8`) | **6.48 ticks per output element** |

**IPB is a density metric, not a performance metric, and only one kernel reaches 6.0.**
The distinction is load-bearing in this project and is backed by measurement:

* R9.5 cut execution time 37.88% while leaving vector-region density **bit-identical** — it
  removed empty *bundles*, not empty slots.
* `axpy vi16` has **3× lower IPB than `axpy vi8` and runs 7.4% faster** (766 vs 823 ticks on
  the same 64-element job).
* `gemm vi16` (IPB 3.625) and `gemm vi8` (IPB 2.429) finish in **identical** 4 375 ticks.

**Ticks per output element is the throughput metric to use.** Full analysis in
[`compiler/R10_FINAL_THROUGHPUT_EVALUATION.md`](compiler/R10_FINAL_THROUGHPUT_EVALUATION.md).

---

## Quick start

```bash
# 1. Prerequisites: python3 (3.12.3 used), gcc, and pycparser — the ONLY dependency
pip install pycparser

# 2. Point at the APARA engine toolchain binaries (external to this repo)
export APARA_TOOLS=/path/to/engine_isp/assembler/bin

# 3. REQUIRED: the toolchain needs six simulator fixes this compiler is verified
#    against — without them results are silently wrong ($fsqrt returns 0, float
#    casts are garbage, unsigned vreduce is treated as signed).
#    --check reports; --apply patches the SOURCE (rebuild with scons afterwards).
python3 engine_patches/apply_engine_fixes.py --check /path/to/engine_isp/assembler/src

# 4. Compile and run one program, gcc-style
./apara-cc mytest.c --run

# 5. Confirm the setup
bash testing/run_gate.sh
```

## Build and run

```bash
python3 compiler/compiler.py input.c -o out.mcode -v
python3 compiler/compiler.py input.c --global-base 0x200 --stack-top 0x1000
python3 compiler/compiler.py input.c --preprocess      # force gcc -E first
```

The compiler emits `.mcode`, `data.map`, a golden `.result` and a `run.sh`. Executing it needs
the accelerator's toolchain (`mcode_align`, `mcode_assemble`, `mcode_run`) — run `./run.sh`
from the generated directory, or use `./apara-cc --run`, or the verification harness below,
which drives the toolchain end to end.

## Verification

```bash
cd compiler

python3 -m verification                 # 38 programs + 3 negative controls, real simulator
python3 -m verification --csv m.csv     # with per-program dynamic metrics
python3 -m verification --quick         # one marker per kernel family
python3 -m verification --no-vectorize  # scalar baseline for comparison

# 21 unit suites
for t in _r3_1 _r3_2 _r4_0 _r4_1 _r4_2 _r4_2_5 _r4_2_6 _r4_2_8 _r4_3 \
         _r4_4 _r4_4_5 _r4_5 _r4_6 _r6_1 _r6_2 _r6_6 _r6_8 _r7_1 _r9_1 _r9_2; do
  python3 ${t}_test.py | tail -1
done
python3 loopopt/_r3_0_test.py | tail -1

# whole-pipeline equivalence over the 124-program corpus
cd .. && python3 compiler/loopopt/pipeline_crosscheck.py | grep RESULT
```

Expected: `RESULT: PASS` (38/38, 3/3 controls rejected), 21 × `ALL … TESTS PASS`, and
`RESULT: PASS` with 124/124 identical.

## Reproducibility

```bash
python3 compiler/evaluation/__main__.py     # static evaluation harness, ~5 s, no toolchain needed
```

Writes `compiler/evaluation/results/*.csv` and `plots/*.svg`. Every table in the evaluation
reports derives from `benchmarks.csv`; nothing is transcribed by hand.

Full environment, exact commands, kill switches and determinism notes:
**[`compiler/REPRODUCIBILITY.md`](compiler/REPRODUCIBILITY.md)**.

Each optimization has a kill switch so its contribution can be measured directly — e.g.
`APARA_NO_ALIGN_BUNDLE=1` (R9.5), `APARA_NO_GEMM_REG_IMM=1` (R9.3), `APARA_NO_AVN=1` (R9.1),
`APARA_NO_VECTORIZE=1` (the whole vector path). The R9.x switches were each verified to
reproduce the preceding tagged commit **byte-for-byte**.

## Benchmark corpus

**124 programs are tracked**, discovered by glob from `testing/`, `new_isa_tests/`,
`demo_prof/` and `isa_coverage_tests/` — this is the set `pipeline_crosscheck` compares. A
fresh clone reproduces it exactly; no build artifacts are required.

Separately, `python3 -m verification` builds a **38-program simulator suite** of six vector
families × six element types (`vi8/vu8/vi16/vu16/vi32/vu32`) plus two deliberately-scalar
controls, generated by `compiler/verification/suite.py`.

`matmul16/` is a standalone 16×16 packed GEMM benchmark used throughout the R9–R12 reports.

## Pipeline

```
input.c
  → gcc -E → pycparser AST → Three-Address IR
  → vectorization (kernel recognition, legality, profitability, realisation choice)
  → scalar optimizer (IVSR, LICM, GVN, mem2reg, strength reduction, copy-prop, DCE)
  → software pipelining / superblock scheduling
  → register allocation (28-register pool, rematerialization, spilling)
  → APARA mcode → list scheduling → VLIW bundle packing → alignment-aware widening
  → <name>.mcode + data.map + run.sh + golden .result
```

## Milestone history

The **official frozen thesis artifact is `r10-final`**. R11 and R12.1 are validated
post-freeze follow-ups that do not alter it.

| milestone | what it did | suite ticks |
|---|---|---|
| R4.x | vectorization infrastructure: recognition, legality, profitability, the six kernel clients | — |
| R6.x | ILP analysis, memory disambiguation, sliding window, adaptive unroll, accumulator expansion, vector SWP | 210 359 → 136 206 |
| R7.1 | rematerialization of frame-address temps — drove memory spills to 0 | — |
| R9.1 | address value numbering (`IRLoadAddr` in GVN) | 136 206 → 131 743 |
| R9.2 | branch-immediate folding; first use of native `<` / `<=` | 131 743 → 131 424 |
| R9.3 | GEMM invariant row base + `[reg+imm]`; memory edges 17 → 0 in the hot block | 131 424 → 108 960 |
| **R9.5** | **alignment-aware bundle formation** — executed pad bundles 34 507 → 625 | **108 960 → 67 689** |
| **R10** | **final evaluation and freeze** → tag **`r10-final`** | **67 689** |
| R11 | realisation-probe candidate rescue (`r11-verified`) | suite unchanged |
| R12.0 | partial-unroll investigation — found it already existed (`r12.0-analysis`) | no code change |
| R12.1 | GEMM compact-unroll correctness fix (`r12.1-verified`) | suite unchanged |

Per-milestone reports are `compiler/R*_DELIVERY.md`; the dated engineering log is
[`compiler/STATUS.md`](compiler/STATUS.md).

## Repository structure

```
apara-cc                     gcc-like front door: ./apara-cc prog.c --run
compiler/                    the compiler
├── compiler.py               entry point — CLI, preprocessing, data.map, golden results
├── ir.py / ir_gen.py         IR definitions; C AST → Three-Address IR
├── codegen.py                IR → mcode (28-register allocator, spilling, rematerialization)
├── bundler.py                list scheduler, VLIW packer, alignment-aware widening
├── *_vectorizer.py           the six kernel clients (dot, elementwise, axpy, gemm, conv)
├── vector_*.py               one shared vector pipeline, affine analysis, legality, lowering
├── loopopt/                  loop framework: unrolling, LICM, IVSR, SWP, dependence graph
├── vector_backend/           ILP / occupancy / memory-dependence analysis (measurement only)
├── verification/             38-program simulator harness with gcc golden references
├── evaluation/               static evaluation harness (CSV + SVG output)
├── _r*_test.py               21 unit suites
├── STATUS.md                 dated engineering log
├── ARTIFACT.md               artifact identity and freeze record
├── FINAL_EVALUATION.md       summary sheet
├── REPRODUCIBILITY.md        environment, commands, kill switches
└── R*_DELIVERY.md            per-milestone reports

testing/                     121 corpus programs + the golden gate (run_gate.sh)
demo_prof/                   3 corpus programs
matmul16/                    standalone 16x16 packed GEMM benchmark
engine_patches/              REQUIRED simulator fixes (--check / --apply installer)
*_report/                    engineering reports (LaTeX sources + PDFs)
```

## Known limitations

**Architectural** (the ISA, not the compiler):

* **No scaled-index addressing mode** — only `[reg+reg]` and `[reg+imm]`, so every
  element-index → byte-offset conversion needs an explicit shift. Address-ALU waits are 48.5%
  of vector-region empty slots and are largely irreducible.
* **8-slot issue width** — vector regions run at 45.7% occupancy.
* **`$v` granularity**: one 64-bit register per instruction, so 32-bit elements get only 2
  lanes against 8 for `vi8`.
* **28-register pool** — not currently binding (0 spills), but it leaves no headroom for
  deeper pipelining.

**Benchmark**:

* **Harness initialisation dominates.** 74.5% of the suite's dynamic bundles are the
  benchmarks' own scalar init loops, not kernel code. Whole-program IPB (2.213) is diluted
  accordingly; vector-region IPB (3.659) describes the kernels.
* The two scalar controls (`bubblesort`, `divmod`) are **deliberately** not vectorized, and
  some markers select a scalar realisation — both are reported, not hidden.

**Compiler**:

* **GEMM `vi32` scales poorly** past M=16 — 89.48 ticks/output at M=32 against `vi16`'s 19.51.
  R12.1 improved M=32 by 38.5%; the residue is the 2-lane granularity above.
* 34 bundles of scheduler slack remain across 12 hot blocks. R6.5 failed to beat the shipped
  schedule with 12 000 random legal reorderings.
* 2-D arrays are never packed; canonical i-j-k matmul is unsupported (i-k-j is); the induction
  variable must start at zero; expression depth is capped at 8; `scalar − vector` is refused;
  tap-innermost convolution is unrecognised.
* Real string handling is out of scope by design for this accelerator.

The R3.0 oracle ceiling used in the reports is computed on the **scalar** IR and is not an
exact hardware bound for vector code — stated wherever it is quoted.

## C feature coverage

The C subset for the APARA accelerator is complete: integer (scalar + vector), floating point
(bit-exact vs gcc), function pointers, variadics and >4 arguments. Every entry is backed by a
gcc-verified test in `testing/`. The full table, and the verification method behind it, is in
[`compiler/STATUS.md`](compiler/STATUS.md).

Highlights: all integer types and 12 ALU ops · full control flow incl. `switch`/`goto` ·
recursion and mutual recursion · 1-3D arrays · `struct`/`union`/bit-fields/arrays of structs ·
pointers (arithmetic, comparison, `q-p`, `p++`) · `enum`/`typedef`/`sizeof`/`static` locals ·
designated initializers · vector intrinsics · f32/f64 · function pointers · variadics.

## History

Branch [`history`](../../tree/history) and tags `v1`/`v2`/`v3` capture early drafts of the
`compiler/` core files, recovered from an editor backup cache after the project ran for a
while without version control. [`archive/full-history`](../../tree/archive/full-history) holds
older campaigns and per-instruction hardware suites. `main` is the authoritative current line.

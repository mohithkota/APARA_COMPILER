# FINAL EVALUATION — APARA vector compiler

**Frozen at `df3d49a`, tag `r10-final`.** Full detail in
`R10_FINAL_THROUGHPUT_EVALUATION.md`; this is the summary sheet.

## Result

| | |
|---|---|
| **suite ticks** | **67 689** (from 210 359 at the R6 baseline — **3.11×**) |
| **whole-program weighted IPB** | **2.213** |
| **vector-region aggregate IPB** | **3.659** |
| **best kernel density** | reduction vi32 — **6.000 IPB**, 100% of its oracle ceiling |
| **best kernel throughput** | conv3 vi8 — **6.48 ticks/output element** |
| correctness | 38/38 simulator, 3/3 negative controls, 21/21 unit suites, 124/124 crosscheck, 0 spills |

## The three metrics, kept apart

This campaign's central methodological finding is that these move independently:

| | meaning | value |
|---|---|---|
| **density** | real instructions per real bundle | 3.659 (vector regions) |
| **throughput** | real instructions per tick | 2.213 (whole program) |
| **execution time** | ticks — the only performance claim | 67 689 |

Three measured proofs that IPB must not be the objective:

1. **R9.5 cut execution time 37.88% while leaving vector-region density
   bit-identical** — it removed empty *bundles*, not empty slots.
2. **`axpy vi16` has 3.05× lower IPB than `axpy vi8` and runs 7.4% faster**
   (766 vs 823 ticks, same 64-element job).
3. **`gemm vi16` (IPB 3.625) and `gemm vi8` (IPB 2.429) finish in identical
   4 375 ticks.**

**The correct optimization target is ticks per output element.**

## Where the performance came from

| milestone | suite ticks | Δ |
|---|---|---|
| R6 baseline | 210 359 | — |
| R6.4.1 adaptive unroll | 136 847 | −34.9% |
| R6.7 / R6.8 / R8.1a | 136 206 | −0.5% |
| R9.1 address value numbering | 131 743 | −3.28% |
| R9.2 branch-immediate folding | 131 424 | −0.24% |
| R9.3 GEMM `[reg+imm]` | 108 960 | **−17.09%** |
| R9.5 alignment-aware bundling | **67 689** | **−37.88%** |

From R9.1 onward the benchmark set and sources were fixed, so that portion —
**131 743 → 67 689, −48.6%** — is attributable to the compiler alone.

## What limits it now

**Compiler-side issues, all measured and fixed:** redundant frame-address
materialization (R9.1), loop-invariant branch constants (R9.2), per-chunk address
re-derivation that hid disjointness from R6.2 (R9.3 — 152 same-base memory edges
→ 0), and unmodelled alignment padding (R9.5 — executed pads 34 507 → 625).
Register spills are 0 everywhere.

**Architectural constraints remaining:** no scaled-index addressing mode (so every
index→byte conversion needs an explicit shift — address-ALU waits are 48.5% of
vector empty slots); the 8-slot issue width (vector regions at 45.7% occupancy);
`$v` granularity of one 64-bit register per instruction; and kernel
arithmetic/memory ratios — dependence height binds 30 of 32 hot vector blocks,
20 of which ship exactly at that bound.

**Is more compiler work justified?** Not on this evidence. Memory dependence is
spent (2.1% of vector empty slots), registers are not binding, padding is 98.2%
removed, and the scheduler is at its lower bound where it matters.

## Known limitations

1. **74.5% of suite dynamic bundles are harness initialisation scaffolding**, not
   kernel — whole-program IPB is diluted accordingly.
2. **GEMM vi32 does not scale past M=16** (ticks/output 19.11 → 145.48 at M=32,
   density 4.900 → 1.833). Measured, not investigated — the clearest future-work
   item.
3. 34 bundles of scheduler slack remain across 12 hot blocks.
4. The R3.0 oracle ceiling is scalar-derived and is not an exact hardware bound.

## Documents

| file | contents |
|---|---|
| `R10_FINAL_THROUGHPUT_EVALUATION.md` | the full 15-section final evaluation |
| `REPRODUCIBILITY.md` | environment, commands, kill switches, determinism |
| `ARTIFACT.md` | artifact identity and verification record |
| `STATUS.md` | full engineering log, every milestone |
| `R9_5_FINAL_IPB_EVALUATION.md` | IPB characterization in depth |
| `R9_4_POST_R9_3_FINAL_ANALYSIS.md` | bottleneck analysis that motivated R9.5 |
| `R9_*_DELIVERY.md` | per-milestone delivery reports |

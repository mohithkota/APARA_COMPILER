# Remaining Architectural Bottlenecks

Evaluation section for the frozen compiler (`cf7d6e8`). **No optimization
milestone follows this.** Its purpose is to explain, from measurement, why the
compiler stops where it does, and to establish that what remains is architectural
rather than compiler-side.

Method: R6.1's empty-slot classifier, which accounts for **100%** of unused issue
slots and is verified to reproduce the production bundler's output instruction for
instruction. Figures are frequency-weighted dynamic counts over the 38-program
verification suite (31 analysable), plus a vector-region-only view.

---

## 0. Headline

**54.2% of all issue slots are empty** — 363 052 of 669 960, at a whole-program
IPB of 3.665 over the analysable set (median per-program IPB 1.09).

| cause | whole program | vector region only |
|---|---|---|
| waiting-for-address-alu | **36.8%** | **30.3%** |
| region-boundary-label | 22.4% | 11.4% |
| region-boundary-control | 11.9% | — |
| memory-dependence | 8.6% | **22.1%** |
| waiting-for-vector-load | 5.0% | 18.2% |
| waiting-for-vector-alu | 5.1% | 7.2% |
| waiting-for-scalar-load | 4.3% | 2.0% |
| waiting-for-scalar-alu | 2.8% | — |
| store-ordering | 2.1% | 4.6% |
| waiting-for-vector-multiply | 0.9% | 2.3% |
| **memory-lanes-full** | **0.0%** | 1.6% |
| no-ready-instruction | 0.1% | — |

## 1. Address-generation dependency chain

The largest single cause. But the label overstates it, and the correction matters:
the classifier maps *every* scalar ALU producer to `waiting-for-address-alu`,
including the program's own integer arithmetic. Splitting by opcode — APARA forms
addresses only with `+`, `<<`/`*` and `$set`; `&`, `|`, `^`, `-` never address:

| | whole program | vector region |
|---|---|---|
| **genuine ADDRESS generation** | **66.9%** | **89.6%** |
| DATA arithmetic of the program itself | 33.1% | 10.4% |

The mislabelled third is real program work — `&` and `-` from the benchmarks'
`a[i] = i & 7` initialisation, and `|`/`>>` from the R6.3 convolution window.

**So genuine address generation is 24.6% of all empty slots whole-program, and
27.2% within the vector regions** — not 37%.

### What the address instructions actually compute

| | whole program | **vector region** |
|---|---|---|
| base + constant (FP slot / global / SP) | 31.5% | 14.5% |
| **scaled index (`<< k` for elem_bytes)** | 8.4% | **17.9%** |
| **address add: base + index register** | 5.2% | **25.3%** |
| IV / pointer increment | 7.4% | **20.6%** |
| register copy, constant materialisation | 14.5% | 11.3% |

*(percentages of the `waiting-for-address-alu` total in each scope)*

**Inside the vector regions, the scaled-index and base+index pair together account
for 43.2%.** Those two instructions exist because of the load/store format:

```
$ld (type) rd [rs1 + rs2]        ; register + register
$ld (type) rd [rs1 + <imm10>]    ; register + immediate
```

**APARA has no `[base + index*scale]` addressing mode.** Every strided access must
therefore materialise its scaled offset in a register and add it to a base — two
instructions that a scaled-index mode would eliminate outright. A further 20.6% is
the induction-variable/pointer increment, which is inherent to any counted loop.

### This is not compiler scaffolding — it is below the naive ISA minimum

The obvious objection is that the compiler invented these addresses. It did not.
A naive lowering needs **≥ 2 address instructions per strided memory access**
(scale, then add). Measured in the vector regions:

| kernel | memory ops | address ops | **address / memory** |
|---|---|---|---|
| axpy vi16 | 6 | 2 | **0.33** |
| reduction vi32 | 8 | 8 | **1.00** |
| elementwise vi8 | 33 | 40 | **1.21** |
| conv3 vi8 | 44 | 63 | **1.43** |
| dot vi8 | 25 | 44 | **1.76** |
| gemm vi16 | 18 | 97 | 5.39 |

**Five of six kernels sit at or below the naive minimum**, because IVSR converts
index arithmetic into pointer increments, LICM hoists invariant bases, and loop
register promotion keeps them in registers — so one increment serves several
accesses (axpy: 2 address instructions for 6 memory operations).

GEMM at 5.39 is the honest exception: its row base is re-derived per chunk through
`clone_offset` rather than strength-reduced across the outer loop. That is the one
place the measurement points at compiler scaffolding rather than the ISA, and it is
recorded rather than fixed.

**Conclusion: the address-generation chain is architectural.** The compiler has
already amortised address work below the level the ISA's addressing modes
naively require; the residue is the format itself.

## 2. Region boundaries — limited by CFG structure

22.4% (label) + 11.9% (control) = **34.3% whole-program**, falling to **11.4%
inside the vector regions**.

The disparity is the point: R3.2 superblock formation and R6.7's per-region
acceptance have already merged what is legally mergeable in the vector code.
What remains whole-program is the scalar initialisation loops, whose bodies are
2–6 instructions between labels — bundles cannot span a label, so they are cut
short by control-flow structure, not by scheduling. Merging further requires
speculation or duplication, which region formation explicitly excludes.

## 3. Memory dependence — residual conservative ordering

8.6% whole-program, **22.1% inside the vector regions** — the second-largest
vector-region cause.

R6.2's symbolic memory-object analysis compares addresses by subtraction and
proves disjointness where it can; what remains is genuinely unprovable at compile
time, and correctness requires the conservative answer. Store-ordering adds a
further 4.6% in the vector regions.

## 4. Memory bandwidth is not limiting

**`memory-lanes-full` is 0.0% whole-program and 1.6% in the vector regions.** The
four memory lanes are never saturated.

This is an independent confirmation of the R8.0 result: wide `$u128/$u256` loads
would relieve a constraint that does not bind. It also matches the bundle-bound
analysis, where all six vector regions are width-bound (`⌈N/8⌉`) rather than
memory-lane-bound (`⌈M/4⌉`) at 27–40% memory operations.

## 5. Why no further optimization milestone follows

Each remaining cause is closed by a measurement already in the record:

| cause | why it is not compiler-addressable |
|---|---|
| address generation (24.6% / 27.2%) | already below the naive ISA minimum (§1); no `[base + index*scale]` mode exists |
| region boundaries (34.3% / 11.4%) | superblocks already merged what is legal; more requires speculation, excluded by design |
| memory dependence (8.6% / 22.1%) | R6.2 proves disjointness where provable; the rest is required for correctness |
| vector load/ALU latency (11.0% / 27.7%) | real work; the scheduler reaches the dependence lower bound in 20/23 configurations (R6.5) |
| memory bandwidth (0.0%) | not a bottleneck |

And the metric itself is not a performance proxy: **AXPY vi16 at IPB 1.57 runs
1238 ticks; the same kernel at IPB 6.95 runs 1792 — 45% slower.** Raising IPB by
emitting more instructions makes the compiler measurably worse, which is why the
adaptive realisation deliberately selects the low-IPB configuration.

## 6. Threats to validity

* 31 of 38 programs were analysable by the classifier; 7 (scalar-only programs
  with no vector region) were skipped and are excluded from the totals.
* The address/data split is by **opcode**, which is checkable but not perfect: a
  `<<` can serve either purpose, and `-` is counted as data although address
  computation could in principle use it. Both choices are stated so the numbers
  can be re-derived.
* The `address / memory` ratio counts static instructions in the vector region,
  not dynamic executions.
* GEMM's 5.39 ratio is a genuine outlier and is reported rather than excluded.

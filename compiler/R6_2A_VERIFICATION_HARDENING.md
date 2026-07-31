# R6.2A — Verification Infrastructure Hardening

**No compiler optimization was performed in this milestone.** The only compiler
file touched is `golden_stubs.h`, a test-only reference header used exclusively
by the native gcc build. Everything else is new verification infrastructure.

The milestone did what it was meant to do, and then some: hardening the
verification immediately exposed **three pre-existing defects that the previous
process could not see**, two of them in shipped vector code paths that were
recorded as fully validated.

---

## 1. Root cause analysis

### 1.1 The mechanism

`compile_c_to_mcode` obtains independent ground truth through
`try_golden_verify`: it compiles the test source **natively with gcc** against
`golden_stubs.h`, runs it, and writes the observed values as `PostCondition`
lines into `<name>.result`. The simulator is then invoked as

```
mcode_run -p 0x0 -i prog.obj -d data.map -r prog.result
```

and compares its final memory against that file.

The packed vector markers — `vi8_t`, `vu8_t`, `vi16_t`, `vu16_t`, `vi32_t`,
`vu32_t`, `vf32_t` — are **compiler-only layout markers**. They exist in
`compiler.py`'s `_FAKE_TYPEDEFS` to request naturally-strided (packed) array
layout, and they are unknown to gcc. `golden_stubs.h` therefore has to declare
them for the native reference build.

**It declared exactly one: `vu8_t`.**

### 1.2 The failure chain

Every test written with any other marker followed this path:

```
gcc: error: unknown type name 'vi8_t'
        |
        v
try_golden_verify  -> prints the error, returns False
        |
        v
compile_c_to_mcode -> falls back to write_result_file / empty placeholder
        |
        v
prog.result        -> EMPTY
        |
        v
mcode_run -r prog.result -> zero PostCondition lines, zero errors, exit 0
        |
        v
"the run passed"
```

Each individual step behaves reasonably. The fallback is deliberate and it
prints its reason. What is missing is anyone **requiring** that a comparison
actually happened — so the composition of correct steps produces a verification
that verifies nothing.

### 1.3 Three independent ways the old flow reported false success

| # | mechanism | observable |
|---|---|---|
| 1 | native reference build failed (missing marker typedef) | placeholder `.result` written, compile still "OK" |
| 2 | placeholder `.result` is empty | `mcode_run` performs **0** comparisons and reports no error |
| 3 | `mcode_run` exits **0 even when a PostCondition fails** | a harness trusting `$?` passes on wrong output |

Mechanism 3 was confirmed directly rather than assumed — see §5.

---

## 2. The previous verification weakness, stated precisely

The claim "the vector corpus runs correctly on the simulator" decomposed into:

* **the run happened** — true;
* **the run was compared against ground truth** — *false for every marker except
  `vu8_t`*.

Correctness for R4.1–R4.6 therefore rested almost entirely on
`vector_lowering.differential_packed`, the **IR-level** differential oracle. That
oracle is a good tool and it caught real defects during development (STATUS.md
records several). But it is a *model*: it executes IR against a flat
`{byte_address: value}` dictionary. It does not model

* the hardware's **alignment requirement** for a 64-bit load, or
* the **DMEM word layout** that packed addressing depends on.

§6 shows it returning `match` for kernels that demonstrably miscompile.

---

## 3. Infrastructure changes

### 3.1 `golden_stubs.h` — complete the marker declarations

All seven markers the compiler defines are now declared for the native build:

```c
typedef unsigned char  vu8_t;
typedef signed char    vi8_t;
typedef unsigned short vu16_t;
typedef short          vi16_t;
typedef unsigned int   vu32_t;
typedef int            vi32_t;
typedef float          vf32_t;
```

These are declarations **for gcc only**. They do not appear in, and cannot
affect, the APARA front end (`_FAKE_TYPEDEFS` supplies its own), the IR, the
generated mcode, the DMEM layout or simulator execution — the markers are a
request for packed array *stride* on the APARA side, which has no counterpart in
native memory layout. Verified by construction: `golden_stubs.h` is included
only by the temporary `_driver.c` that `try_golden_verify` hands to gcc.

**`vi64_t` is deliberately NOT declared.** No such marker exists in the compiler:
a 64-bit element yields a one-lane "vector", so `vector_capability_db`
`ELEMENT_TYPES` stops at 32 bits and `_FAKE_TYPEDEFS` defines nothing wider.
Declaring it in `golden_stubs.h` alone would let a test compile natively and
then fail to parse on the APARA side — a new asymmetry of exactly the kind this
milestone exists to remove.

### 3.2 `compiler/verification/` — a harness that cannot pass vacuously

| file | role |
|---|---|
| `harness.py` | compile → native reference → assemble → simulate → **six independent checks** |
| `suite.py` | one program per (packed marker × kernel family), each with a real reference |
| `__main__.py` | CLI, negative controls, dynamic-metric CSV |

The harness never infers success from the absence of a complaint. It requires
**positive evidence** at every stage:

| # | check | rejects |
|---|---|---|
| 1 | `native` | gcc reference build failed / placeholder fallback taken |
| 2 | `golden` | `.result` missing, empty, or not the declared size |
| 3 | `build` | `mcode_align` / `mcode_assemble` reported an error |
| 4 | `executed` | simulator did not run to completion (no tick count) |
| 5 | `compared` | **number of comparisons performed ≠ number declared** |
| 6 | `clean` | any `Error:` line from the simulator |

**Check 5 is the one that closes the hole.** The declared count comes from the
*test*, not from the tool output, so a missing reference produces zero
comparisons and fails, instead of passing silently.

This is also the only code in the repository that invokes the external
toolchain. It is confined to `verification/`, imported by no compiler module and
no unit test, and must be run explicitly:

```sh
cd compiler
python3 -m verification                 # full suite + negative controls
python3 -m verification --no-vectorize  # scalar baseline
python3 -m verification --csv out.csv   # dynamic metrics
```

`mcode_run` is invoked **without `-v`** — the verbose trace of a loop kernel
reaches gigabytes.

### 3.3 Test sources must initialise what they read

Every program in the suite fills its arrays in a loop with deterministic values
before use. This is not cosmetic: the same source is compiled twice, once by gcc
and once by this compiler, and an uninitialised local holds different garbage in
each. Comparing two runs over undefined data is another way to obtain a
meaningless green result.

---

## 4. Verification pipeline, before and after

**Before**

```
compile ──> (golden?) ──no──> placeholder ──> mcode_run ──> exit 0 ──> "pass"
                │                                  │
               yes                            0 comparisons
                │                                  │
                └────────> real reference ─────────┘
```

**After**

```
compile ──> native reference MUST succeed        (1 native)
        ──> reference MUST be real and sized     (2 golden)
        ──> assembler MUST be clean              (3 build)
        ──> simulator MUST reach a tick count    (4 executed)
        ──> comparisons performed MUST == declared (5 compared)
        ──> zero Error: lines                    (6 clean)
                          │
                     any failure => FAIL, with the stage named
```

Exit status is never consulted as evidence.

---

## 5. Intentional failure demonstration

Three negative controls run on every invocation. Each reproduces one way the old
flow reported false success; **each must now fail**, and the harness reports its
own failure if any of them passes.

```
negative controls (each MUST fail):
  ok   corrupted expected value           -> FAIL[clean]
  ok   no golden reference available      -> FAIL[native]
  ok   declared count exceeds reference   -> FAIL[golden]
```

The corruption control flips one byte of one expected value in an otherwise
correct axpy run. The simulator responds:

```
Info:  PostCondition Mem[0x80] = 0x0  (mask=0xffffffffffffffff)
Info:  PostCondition Mem[0x81] = 0x4  (mask=0xffffffffffffffff)
Info:  PostCondition Mem[0x82] = 0x4  (mask=0xffffffffffffffff)
Error: PostCondition Mem[0x83] = 0x18, expected 0xff (mask=0xffffffffffffffff)
```

and **still exits 0**. The harness fails it at stage `clean`. An incorrect
result can no longer pass verification.

---

## 6. Regression results — three pre-existing defects, now visible

Full suite: 6 markers × 6 families + 2 scalar controls = **38 programs**, each
against a real golden reference.

| configuration | verified | failed |
|---|---|---|
| vectorization **ON** (production default) | **27 / 38** | 11 |
| vectorization **OFF** (`APARA_NO_VECTORIZE`) | **37 / 38** | 1 |

Running both configurations is what separates the defects. **None of these are
caused by R6.2A** — the only compiler-visible change in this milestone is a set
of gcc-only typedefs.

### D1 — convolution emits UNALIGNED 64-bit loads *(vectorizer; all six markers)*

Fails with vectorization on, passes with it off, for `vi8/vu8/vi16/vu16/vi32/vu32`.

```
Error: Unaligned address in load nbytes= 8, addr= 32689
Error: Unaligned address in load nbytes= 8, addr= 32697          (x14)
...
Error: PostCondition Mem[0x80] = 0x0, expected 0x3
Error: PostCondition Mem[0x81] = 0x9, expected 0xc
```

`out[i] = in[i] + in[i+1] + in[i+2]` is vectorized by loading three *shifted*
packed windows. A shifted window starts at `base + 1`, `base + 2`, … so **two of
every three 64-bit loads are unaligned by construction**, whatever the base
alignment. The hardware rejects them and the computed values are wrong
(`out[0]` = 0 instead of 3). R4.0's capability database already records that
wide `$ld`/`$st` require alignment; the 1-D/2-D convolution lowering added in
R4.6/R4.6.1 does not honour it for the 64-bit packed form.

### D2 — packed GEMM is wrong for 16- and 32-bit elements *(vectorizer)*

`vi8` is correct. `vi16`, `vu16`, `vi32`, `vu32` all produce **all-zero** output
with no alignment error at all:

```
Error: PostCondition Mem[0x80] = 0x0, expected 0x18
Error: PostCondition Mem[0x81] = 0x0, expected 0x30
Error: PostCondition Mem[0x82] = 0x0, expected 0xc0
```

`C` is never correctly written. A 64-bit chunk holds 8 `vi8` lanes but only 4
`vi16` or 2 `vi32`, so this has the signature of a chunk-stride assumption that
holds only for 8-bit elements — but the root cause has **not** been confirmed
here, and this report does not claim one.

### D3 — `vu8_t` loads are sign-extended *(scalar codegen, not the vectorizer)*

The single failure that occurs with vectorization **off** as well:

```
Error: PostCondition Mem[0x82] = 0xffffffffffffffc0, expected 0xc0
```

`0xc0` is 192; `0xffff…c0` is −64. An **unsigned** char element is being read
back sign-extended. This one was always reachable — `vu8_t` was the one marker
gcc did know about — but nothing ever ran that comparison on the simulator.
Note that the `vu16`/`vu32` kernels here never exceed their signed range, so an
analogous defect at those widths would not be exposed by this suite.

### 6.1 Why the existing validation did not catch D1 or D2

The IR-level differential oracle was asked directly about the same kernels:

| kernel | `differential_packed` verdict | simulator |
|---|---|---|
| gemm `vi8_t` | `match` | **PASS** |
| gemm `vi16_t` | `match` | **FAIL** (all zeros) |
| gemm `vi32_t` | `match` | **FAIL** (all zeros) |
| conv3 `vi8_t` | `match` | **FAIL** (unaligned) |
| conv3 `vi16_t` | `match` | **FAIL** (unaligned) |

The oracle reports `match` for every kernel that miscompiles. It is not broken —
it faithfully executes the IR it is given — but it models memory as a flat byte
dictionary and therefore cannot see an alignment fault or a DMEM-layout error.
**Every "0 mismatches" claim in R4.1–R4.6 was made by this oracle.** That is the
real finding of this milestone: the previous correctness baseline was weaker
than the record suggested, in a way no amount of re-running it would reveal.

---

## 7. Dynamic simulator metrics (the new baseline)

Measured, not modelled: `ticks` is bundles actually executed, `non-null` the
instructions actually issued, `null` the empty slots actually issued. These
replace R6.1's projected figures as the baseline for R6.2 onward.

`dynamic IPB = non-null / ticks`, `occupancy = non-null / (non-null + null)`.

| program | ticks | non-null | null | dyn IPB | occupancy |
|---|---|---|---|---|---|
| elementwise vi8 | 1601 | 1335 | 5145 | 0.834 | 20.6% |
| axpy vi8 | 1538 | 1281 | 5183 | 0.833 | 19.8% |
| dot vi8 | 1679 | 1414 | 5346 | 0.842 | 20.9% |
| reduction vi8 | 752 | 525 | 2251 | 0.698 | 18.9% |
| gemm vi8 | 8160 | 9325 | 27955 | 1.143 | 25.0% |
| elementwise vi16 | 1678 | 1643 | 5433 | 0.979 | 23.2% |
| axpy vi16 | 1550 | 1597 | 5461 | 1.030 | 22.6% |
| dot vi16 | 1568 | 1663 | 4959 | 1.061 | 25.1% |
| reduction vi16 | 723 | 762 | 2226 | 1.054 | 25.5% |
| elementwise vi32 | 1969 | 1939 | 6373 | 0.985 | 23.3% |
| axpy vi32 | 1827 | 1889 | 6401 | 1.034 | 22.8% |
| reduction vi32 | 883 | 954 | 2802 | 1.080 | 25.4% |
| scalar bubblesort | 19923 | 24484 | 71156 | 1.229 | 25.6% |
| scalar divmod | 985 | 867 | 2837 | 0.880 | 23.4% |

Full data, including the scalar baseline for every program, is in the CSVs
produced by `--csv`.

**Two observations that matter for R6.2 onward.**

*Measured dynamic IPB is far below R6.1's static estimate* (0.83 vs 1.32 for
axpy vi8). R6.1 counted a bundle once per execution; the simulator counts
**ticks**, which include the pipeline's own stall and alignment behaviour. The
measured occupancy (~20–25%) nonetheless lands close to R6.1's modelled ~16–24%,
so the *shape* of the R6.1 analysis holds — its absolute IPB did not.

*Vectorization currently costs dynamic performance on these small kernels.*
`dot vi32` runs 3401 ticks scalar; `elementwise vu32` 3476 scalar vs 1969
vectorized (a win), but `dot vi8` is 1679 vectorized against a scalar path that
is faster per tick. Vector kernels issue far fewer instructions but at much
lower occupancy — precisely the ILP deficit R6.1 identified, now confirmed with
measured numbers rather than a model.

---

## 8. Remaining limitations

* **The three defects are reported, not fixed.** D1 and D2 are vectorizer bugs,
  and R6.2's charter explicitly forbids vectorizer changes; D3 is in scalar
  codegen. Fixing them is a separate milestone and should precede any further
  backend aggression — **11 of 38 vector programs currently produce wrong
  results on hardware.**
* **The suite is small** (38 programs, 6 families). It is a marker-coverage
  suite, not a workload mix. It does not cover 2-D convolution, expression
  trees, remainder peeling, or `vf32_t`.
* **`vf32_t` is declared but untested.** Floating-point vector support is
  recorded as under construction; no FP kernel is in the suite.
* **Test values are small by design**, to keep both builds inside defined
  behaviour. That is why D3 shows at 8 bits and not at 16 or 32 — a sign-related
  defect at wider element types would not be exposed by these inputs.
* **`mcode_align` chatter is filtered heuristically.** It prints per-instruction
  parse traces to stderr on every run; the harness treats only `Error`/`Fatal`
  markers as real. A future assembler change to that format could mask a build
  error.
* **One simulator, one machine, one toolchain build.** The behaviour observed
  here is that of the `mcode_run` binary in `engine_isp/assembler/bin`.
* **Ticks are not cycles.** The simulator reports ticks; no claim is made that a
  tick equals a hardware cycle.

---

## 9. Reproduction

```sh
cd compiler
python3 -m verification --csv r62a_vector.csv                 # 38 programs
python3 -m verification --no-vectorize --csv r62a_scalar.csv  # baseline
```

Requires gcc and the APARA toolchain (`APARA_TOOLS`, or the default path). The
run takes ~35 s vectorized, ~21 s scalar.

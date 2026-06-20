# APARA Compiler Verification Report

Systematic instruction-level verification of the APARA compiler, built in two phases: first an
ISA-coverage audit to find and close gaps the existing test suite left unchecked, then a full
rebuild of the verification methodology itself after discovering the original pass/fail signals
(`r1` return codes, placeholder `.result` files) weren't independently checking anything. This
file is the consolidated report; `compiler/STATUS.md` has the full chronological development log.

Scope: integer scalar and vector datatypes only (`vi4`/`vu4` and all floating-point types excluded
per established project direction — float support is under construction and not a current
priority).

## Verification methodology ("no bias")

Every test in this project writes each value it wants to check into a global `results[]` array
(`#define N_RESULTS <n>` + `long long results[N_RESULTS];`) instead of an `if (check) return -N;`
aggregate pass/fail code. This matters because:

- **A single `r1` value tells you almost nothing on failure** — which of N checks failed?
- **An aggregate code can't be cross-checked against an independent ground truth** — if the
  expected value is hand-computed by the same person writing the compiler, a shared blind spot in
  both produces a false pass.

So instead, ground truth is established independently:

1. **Pure C semantics** (arithmetic, overflow, casts, sign extension) → compile and run the exact
   same C source **natively with `gcc`** — a completely separate toolchain from the compiler under
   test, so it can't share a blind spot with it.
2. **APARA-ISA-specific operations** (`$dot`, `$pack`, `$slice`, `$vreduce`, vector ops — things
   `gcc` can't compile directly) → `isa_coverage_tests/golden/golden_stubs.h` provides plain-C
   reference implementations derived **directly from the ISA specification** (`isa.txt`) and
   confirmed empirical hardware behavior — never by reading `ir_gen.py`/`codegen.py` and mirroring
   their logic.
3. **Known hardware/simulator bugs** → golden values stay architecturally correct even when the
   real simulator is confirmed buggy (see `$vreduce` below). A mismatch there is the bug, reported
   honestly, not something to paper over by writing a "wrong" expected value just to make a test
   pass.

`compiler.py` runs this automatically on every compile (`try_golden_verify()`): it finds a global
literally named `results` in its own IR (no source parsing needed — the address and size are
already known internally), compiles the same preprocessed source natively with `gcc` against
`golden_stubs.h`, captures every slot's ground-truth value, and writes a real `.result` file —
one `PostCondition` line per slot, in the exact format the real simulator binary requires
(confirmed empirically, not assumed):

```
<thread_id> mem <word_addr_hex> <value_hex>     e.g.  0 mem 0x80 0x0000000000000063
<thread_id> reg <reg_id_hex> <value_hex>        e.g.  0 reg 0x1 0x0000000000000001
```

`python3 compiler.py test_X.c` is the **one command** that produces both `data.map` (the initial
DMEM state) and this real `.result` file. The simulator's own built-in checker (`mcode_run -r`)
then verifies every line independently and prints `Info: PostCondition ...` (match) or
`Error: PostCondition ..., expected ...` (mismatch) — this report's pass/fail numbers come directly
from that simulator output, not from anything this compiler or its own test harness computed.

Falls back to a (now also-fixed) static-evaluation path or an empty placeholder for tests that
don't use the `results[]` convention, always printing why, never silently.

## Final tally across the whole project

| Suite | Checks | Errors |
|---|---|---|
| `isa_coverage_tests/` (12 files, see matrix below) | 159 | 3 *(documented, expected — see `$vreduce` below)* |
| `matmul_tests/` (N=8/16/32/64, every cell individually) | 5440 | 0 |
| Pre-existing baseline suite (`alu/`, `array/`, `branch/`, `ldst/`, `pointer/`, `new_isa_tests/` — 25 files) | 393 | 0 |
| **Total** | **5992** | **3** (all in one place, all explained) |

## `isa_coverage_tests/` coverage matrix

| File | Instruction(s) | Checks | What it adds beyond the original baseline suite |
|---|---|---|---|
| `test_alu_full.c` | `+ - * / % & \| ^ << >> ~& ~\| ~^` | 13 | All 12 scalar integer ALU ops. `~&`/`~\|`/`~^` (nand/nor/xnor) had **zero** prior coverage anywhere — no C operator reaches them, only the `__nand`/`__nor`/`__xnor` intrinsics. |
| `test_subword_full.c` | `$ld`/`$st` (i8/u8/i16/u16/i32/u32) | 21 | Unsigned types, sign-extension, and overflow wraparound — the original test never used unsigned types or boundary values. |
| `test_cast_full.c` | `$cast` | 14 | Signed/unsigned narrowing matrix, **direct-use** (not pre-assigned to a narrow variable) — the path that exposed the `$cast` no-op bug. |
| `test_cmov_full.c` | `$cmov` | 14 | All 6 conditions (gt/lt/eq/ne/ge/le), both true and false branches — original covered 3 conditions, true branch only. |
| `test_pack_full.c` | `$pack` | 4 | All 4 legal total/word combinations (64/64, 64/32, 32/32, 32/16) — original covered one combo, with the bit order backwards. |
| `test_slice_full.c` | `$slice` | 10 | Nibble/byte/word/dword extracts plus a non-byte-aligned extract — original covered two byte-aligned extracts. |
| `test_valu_full.c` | `$v` (+/-/*, `$replicate`) | 24 | All 6 widths (vi8/vu8/vi16/vu16/vi32/vu32), full-packed-register verification (every element, not just the low one) — original covered signed-only, low-element-only. |
| `test_vreduce_full.c` | `$vreduce` | 12 (9 pass, 3 documented expected fail) | All 6 widths, plus a **confirmed simulator bug** (see below). |
| `test_dot_full.c` | `$dot`/`$dot $accumulate` | 12 | vi8/vu8/vi16/vu16, plain and accumulate — original covered vi16 only. |
| `test_branch_full.c` | `$goto`/branch | 14 | All 6 conditions, true AND false outcomes — original covered true-outcome only, no aggregate signal. |
| `test_load_store_full.c` | `$ld`/`$st`, `$ld`/`$st ($u128)/($u256)` | 13 | Pointer-indirection access for signed/unsigned types, plus a chained wide-load-then-wide-store round trip. |
| `test_call_return_full.c` | `$call`/`$return` | 8 | 4-argument call, 3-level nesting, and **genuine recursion** (factorial + fibonacci) — original used a while-loop "factorial," never real recursion. |

## `matmul_tests/` — every output cell individually verified

New folder, separate from the instruction-coverage suite. Each size's full result matrix is
written cell-by-cell into `results[]` (not spot-checks) and independently verified via the same
`gcc` + `golden_stubs.h` ground truth, using the proven fused `__dot128_direct_vu8` intrinsic.

| File | Matrix size | Cells verified | Errors |
|---|---|---|---|
| `matmul_n8.c` | 8×8 | 64 | 0 |
| `matmul_n16.c` | 16×16 | 256 | 0 *(cross-checked against the original hand-written `16x16_loop/` reference's values first)* |
| `matmul_n32.c` | 32×32 | 1024 | 0 |
| `matmul_n64.c` | 64×64 | 4096 | 0 |

## Real bugs found and fixed in the compiler

1. **Unsigned char/short/int sign-extended on every load** instead of zero-extending —
   `codegen.py`'s `_atype` had no unsigned variant at all. Fixed.
2. **`$cast` was a no-op** whenever casting directly from a 64-bit value (the common case) —
   traced to the simulator using the *second* type tag's width for the actual computation, not the
   first; `ir_gen.py` had the two swapped. Fixed.
3. **The global data area could silently overlap the stack**, corrupting both — no check existed
   before emitting code. Fixed with a compile-time error.
4. **`$st ($u128)/($u256)` (wide store) didn't exist** — only wide load had ever been built.
   Implemented.
5. **Function parameters narrower than 64 bits always read back as garbage** (typically 0) — the
   single most significant finding. The prologue stored every parameter as a full 64-bit value
   regardless of its C-level type, while reads correctly used the parameter's real width; the
   mismatch reads back as zero for any small value. This silently broke every function taking an
   `int`/`short`/`char` (or unsigned) parameter, compiler-wide. Fixed.
6. **The calling convention's hard 4-argument ceiling silently dropped the 5th+ argument/parameter**
   with no error. Now fails loudly at compile time instead.
7. **The verification mechanism itself was broken project-wide.** `compiler.py`'s static-eval
   fallback (`write_result_file`, used for any branch-free program with no `results[]`) wrote
   `.result` lines missing a required leading thread-id token — the real simulator's
   `PostCondition` checker silently ignored every such line, project-wide, before this audit.
   Fixed.
8. **`eval_ir` (that same static-eval fallback) didn't scope its evaluation to `main`** — it walks
   the full flattened instruction list (every function's body back to back, in declaration order)
   and breaks on the *first* `IRReturn` it finds, which can belong to any function declared before
   `main`. `test_spill.c` defines `f01()..f30()` before `main`; the old code broke on `f01`'s
   `return 1` and confidently reported `r1=1` instead of the correct `465` — **a wrong answer that
   looked like a working result**, not a placeholder. Fixed by tracking `IRFuncBegin`/`IRFuncEnd`
   boundaries.
9. **A test's own intrinsic declaration conflicted with its golden reference implementation**
   (`test_slice.c`'s `__slice` declared `(int,int,int)` vs `golden_stubs.h`'s `(long long,int,int)`)
   — caught loudly by `gcc` itself (a compile error, not a silent wrong answer), exactly the
   "fail loudly" design this verification system was built around. Fixed.

## Found, NOT fixed (out of compiler scope)

**`$vreduce` on unsigned vector types (`$vu8`/`$vu16`/`$vu32`) sign-extends instead of
zero-extending** — confirmed via a one-negative-element probe across all three widths. This is a
**simulator/hardware bug**, not a compiler bug: the compiler correctly emits `$vreduce + rd ($vu8)
rs`; the simulator's `__vreduce_operation__` (`McodeOperations.cpp`) has a variable-shadowing bug
where the unsigned branch silently reuses an already-sign-extended value instead of the raw one.
`test_vreduce_full.c`'s golden values are deliberately the architecturally-correct ones even for
this known-buggy case — running it against the real simulator produces exactly 9 `Info` + 3
`Error`, with the 3 errors landing precisely on the unsigned+negative-element cases and showing the
simulator's actual (buggy) output side by side with the correct expected value. Fixing this means
editing the simulator's C++ source — a different codebase/scope than this Python compiler —
flagged for a decision rather than silently fixed or silently ignored.

## Confirmed gaps, not pursued (explicitly out of scope or pre-existing)

- `$abs`, standalone `$max` (ALU-level, distinct from `$vreduce $max`), `$nop`'s parse bug, and
  float arithmetic as a whole category — see `compiler/STATUS.md` for the full ISA-coverage audit
  that identified these before this test suite was built.
- `$vreduce`'s MAX/MUL/AND/OR/XOR/XNOR sub-ops are real per the ISA doc but never emitted by this
  compiler (`_gen_IRVecReduce` hardcodes `$vreduce +`) — confirmed again while building
  `test_vreduce_full.c`.
- `vi4`/`vu4` (4-bit vector elements) — explicitly deprioritized per established project
  direction, not tested here.

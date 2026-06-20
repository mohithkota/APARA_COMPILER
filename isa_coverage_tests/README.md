# ISA Coverage Test Suite

Systematic instruction-level coverage of the APARA ISA, built to find and close gaps the
existing test suite (`alu/`, `array/`, `branch/`, `ldst/`, `pointer/`, `new_isa_tests/`) left
unchecked. Scope: integer scalar and vector datatypes only (`vi4`/`vu4` and all floating-point
types excluded per established project direction — float support is under construction and not
a current priority). Each file is self-contained, hand-verified (expected values computed
independently, not assumed), and returns `1` on full success or a distinct negative code at the
first failing sub-check.

## Coverage matrix

| File | Instruction(s) | What it adds beyond the existing suite |
|---|---|---|
| `test_alu_full.c` | `+ - * / % & \| ^ << >> ~& ~\| ~^` | All 12 scalar integer ALU ops. `~&`/`~\|`/`~^` (nand/nor/xnor) had **zero** prior coverage anywhere — no C operator reaches them, only the `__nand`/`__nor`/`__xnor` intrinsics. |
| `test_subword_full.c` | `$ld`/`$st` (i8/u8/i16/u16/i32/u32) | Unsigned types, sign-extension, and overflow wraparound — the existing test never used unsigned types or boundary values. |
| `test_cast_full.c` | `$cast` | Signed/unsigned narrowing matrix, **direct-use** (not pre-assigned to a narrow variable) — the path that exposed the $cast no-op bug. |
| `test_cmov_full.c` | `$cmov` | All 6 conditions (gt/lt/eq/ne/ge/le), both true and false branches — existing test covered 3 conditions, true branch only. |
| `test_pack_full.c` | `$pack` | All 4 legal total/word combinations (64/64, 64/32, 32/32, 32/16) — existing test covered one combo, with the wrong bit-order assumption. |
| `test_slice_full.c` | `$slice` | Nibble/byte/word/dword extracts plus a non-byte-aligned extract — existing test covered two byte-aligned extracts. |
| `test_valu_full.c` | `$v` (+/-/*,  `$replicate`) | All 6 widths (vi8/vu8/vi16/vu16/vi32/vu32), full-packed-register verification (every element, not just the low one) — existing test covered signed-only, low-element-only. |
| `test_vreduce_full.c` | `$vreduce` | All 6 widths, plus a **confirmed simulator bug** (see below). |
| `test_dot_full.c` | `$dot`/`$dot $accumulate` | vi8/vu8/vi16/vu16, plain and accumulate — existing test covered vi16 only. |
| `test_branch_full.c` | `$goto`/branch | All 6 conditions, true AND false outcomes with an aggregate pass/fail signal — existing test covered all 6 conditions but true-outcome only, no aggregate signal. |
| `test_load_store_full.c` | `$ld`/`$st`, `$ld ($u128)/($u256)` | Pointer-indirection access for signed/unsigned types (not tested anywhere else), plus a chained wide-load-then-wide-store round trip. |
| `test_call_return_full.c` | `$call`/`$return` | 4-argument call, 3-level nesting, and **genuine recursion** (factorial + fibonacci) — existing test used a while-loop "factorial," never real recursion. |

All 12 files: `r1=0x1`, zero pipeline errors, verified on hardware via the standard
`compiler.py` → `run.sh` pipeline.

## Real bugs found and fixed in the compiler (this audit)

1. **Unsigned char/short/int sign-extended on every load** instead of zero-extending.
   `codegen.py`'s `_atype` had no unsigned variant at all. Fixed.
2. **`$cast` was a no-op** whenever casting directly from a 64-bit value (the common case) —
   traced to the simulator using the *second* type tag's width for the actual computation, not
   the first; `ir_gen.py` had the two swapped. Fixed.
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

## Found, NOT fixed (out of compiler scope)

**`$vreduce` on unsigned vector types (`$vu8`/`$vu16`/`$vu32`) sign-extends instead of
zero-extending** — confirmed via a one-negative-element probe across all three widths. This is a
**simulator/hardware bug**, not a compiler bug: the compiler correctly emits `$vreduce + rd ($vu8)
rs`; the simulator's `__vreduce_operation__` (`McodeOperations.cpp`) has a variable-shadowing bug
where the unsigned branch silently reuses an already-sign-extended value instead of the raw one.
`test_vreduce_full.c` asserts the confirmed *actual* behavior (with clear comments on the
discrepancy from the architecturally-correct one), so the suite reflects reality. Fixing this
means editing the simulator's C++ source — a different codebase/scope than this Python compiler —
flagged for a decision rather than silently fixed or silently ignored.

## Confirmed gaps, not pursued (explicitly out of scope or pre-existing, documented elsewhere)

- `$abs`, standalone `$max` (ALU-level, distinct from `$vreduce $max`), `$nop`'s parse bug, and
  float arithmetic as a whole category — see `compiler/STATUS.md` for the full ISA-coverage audit
  that identified these before this test suite was built.
- `$vreduce`'s MAX/MUL/AND/OR/XOR/XNOR sub-ops are real per the ISA doc but never emitted by this
  compiler (`_gen_IRVecReduce` hardcodes `$vreduce +`) — confirmed again while building
  `test_vreduce_full.c`.
- `vi4`/`vu4` (4-bit vector elements) — explicitly deprioritized per established project
  direction, not tested here.

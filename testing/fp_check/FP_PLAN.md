# Floating-point support — campaign plan

Goal: implement C `float`/`double` (arithmetic, literals, load/store, casts,
comparisons) with the same gated, gcc-verified discipline used for the integer
campaigns. `f32`=IEEE single, `f64`=IEEE double, so **gcc is a valid oracle**.

## Toolchain facts (verified by reading the .cpp source + hand tests)
- **Arithmetic**: `__alu_operation__` does `if(dest_type.Get_Float_Flag()) fp_add/…`.
  So emit the ordinary ALU op with a **`$f32`/`$f64` type tag** -> real float math.
  Verified: `3.5+1.5=5.0` (0x40a00000), `3.5*1.5=5.25`.
- **Casts**: `$cast` supports int→float (`cast_int_to_float`), float→int
  (`cast_float_to_int`), float→float. Order: `$cast (dest_type) rd (src_type) rs`.
- **Load/store**: `$st($f32)`/`$ld($f32)` round-trip a float and the value is
  directly usable in float arithmetic (verified). Width-based, like ints.
- **Placement**: an `f32` value lives in the low 32 bits of the register.
- Formats: f32=E8M23, f64=E11M52 (standard IEEE). Smaller f4/f8/f16 are non-std;
  ignore (C has no type for them).

## The core work: float TYPE-TRACKING
The compiler currently treats everything as int. It must learn which
expressions are float and then pick `$f32`/`$f64` instead of `$i64`. Reuse the
existing `_var_ctype` (added for sizeof) + a new `_is_float_expr(node)` helper
(mirror `_expr_is_unsigned`): a node is float if it is a float literal, a float
variable, a float binop operand, a float-returning call, or a cast to float.

## Test/verify convention
results[] is `long long`, so a float test ends with a **cast to int**:
`results[0] = (int)(a+b);` -> gcc computes the same int. That exercises float
arithmetic AND float→int cast together (both needed anyway). Battery in
`testing/fp_check/` (fp01..).

## Implementation order (gate after EVERY step; real suite + integer battery must stay green)
1. **DONE 2026-07-17** — Float literals + scalar arithmetic + float→int cast.
   fp01 = 5, 7, 12, 2 vs gcc. (Also fixed sim: cast dispatch swap, execute-cast
   float stub, fp_sub bit-pattern/plus bugs.)
2. **DONE 2026-07-18** — Float variables (load/store). fp02 = 5, 2, 5, 2 vs gcc.
3. **DONE 2026-07-18** — int→float cast. fp03 = 14, -6, 3, 17 vs gcc.
   (Also fixed sim cast_int_to_float ternary-promotion bug — negative ints
   became 2^64-n; and two bundler holes: float $ld/$st untracked, memory-hazard
   check was tuple-equality instead of alias-aware.)
4. **DONE 2026-07-18** — Float comparisons. fp04 8/8 (all 6 operators, negative
   operands, float-driven while loop). Mechanism: float-subtract the operands,
   `+0.0` to canonicalize -0, then branch on the diff's SIGN with an ($i32)
   test tag for f32 (($i64) for f64) — the branch sign-extends from the test
   type's width, so bit 31/63 = the float sign bit. NaN out of scope.
5. **DONE 2026-07-18** — double (f64). fp05 8/8: arithmetic, literals,
   comparisons, negatives. Also implemented C usual arithmetic conversions
   (`_float_operand`): mixed int·float operands convert (const re-encoded at
   compile time, else runtime $cast), f32 widens to f64. Fixed float unary
   minus (was an integer negate of the bit pattern).
6. **DONE 2026-07-18** — Float params/returns. fp06 4/4: float/double params,
   returns, compare-in-callee, mixed double·int param. Params recorded in
   _var_ctype; function return float tags pre-collected (_func_ret_ftag) so
   forward calls work. ALSO float globals + float ARRAYS (fp07 4/4): global
   initializers IEEE-encoded per declared element width (_flatten_init ftag);
   _float_tag_of extended to ArrayRef (1D/2D) and *ptr via declared elem type.
7. **DONE 2026-07-18** — Bit-exact float result verification. fp09 6/6:
   new golden conventions `fresults[]` (float) / `dresults[]` (double) —
   compiler.py's try_golden_verify captures gcc's IEEE bit patterns
   (memcpy, not value cast) and compares each DMEM word bit-exactly.
   Notes: element count = total_bytes / STRIDE (DMEM footprint, not C bytes);
   f32 expectations shifted <<32 (APARA stores sub-word data in the HIGH half
   of the 8-byte word). Confirmed the sim's f32 AND f64 arithmetic are
   IEEE-bit-exact vs gcc (incl. 1.1*3.0 rounding, 1.1-0.1 == 1.0 exactly).

## Known gaps — ALL CLOSED 2026-07-18 (fp08 8/8)
- struct float fields: `_float_tag_of` handles StructRef (s.f, p->f, pts[i].f,
  nested s.a.b) via `_struct_field_ftag` recorded in `_register_struct`.
- int args to float params: pre-pass records `_func_param_ftags`; `_call`
  converts each arg via `_float_operand`.
- int returns in float functions: `visit_Return` converts via `_func_ret_ftag`.
- float truthiness `if(f)` / `!f`: IRCondJump gets the float tag (float
  compare, so -0.0 is falsy).
- Also: `float a = 3` (decl init), `a = 3` (assign), and float COMPOUND
  assignment (`x /= 2.0f` was an integer binop on bits) all convert now.

Remaining open: step 7 (optional float-bit-exact result verification).

## Gate
Integer feature sweep 16/16, universal 14/14, pointer battery 15/15, real suite
0 err — must all stay green after every FP step (float changes must be gated on
`_is_float_expr`, so integer paths are untouched).

## Definition of done
`float` and `double` arithmetic / literals / load-store / casts / comparisons all
pass vs gcc; integer suite unaffected; coverage table FP row -> ✅. Update
STATUS.md + README.

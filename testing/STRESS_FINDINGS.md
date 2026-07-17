# Stress-test findings (2026-07-17, fixed toolchain)

Differential testing (compiler output vs gcc golden). ~14 stress programs.

## BUG 1 — return of a pointer-dereferenced int is mis-placed in the DMEM word (REAL, open)
Minimal repro (`testing/ptr_isolate/F.c`):
```c
int arr[6]={10,20,30,40,50,60}; long long results[1];
int f(int *p){ return p[0]; }        /* returns 10 */
int main(){ results[0]=f(arr); return 1; }
```
- gcc golden: `0xa`  |  hardware: **`0xa0000000a`** (value 10 duplicated into bits[31:0] AND bits[63:32]).
Localization:
- direct `arr[0]+arr[3]`  -> correct (E.c)
- `f(){return 50;}` plain -> correct (G.c)
- `f(int*p){return p[0];}` -> WRONG (F.c)  <-- pointer-param deref + return is the trigger
- `int results[]` variant -> `0x3200000000` (value lands in bits[63:32] only, H.c)
Related: sub-word DMEM convention ($ld/$st ($i32) uses bits[63:32]); the int
return value isn't normalized to the full-word position before the $i64 store.
**Very likely the same root cause as the still-failing `test_scalar_full`.**
Not yet fixed -- needs a focused session.

## BUG 2 — unsigned 64-bit `>>` (SIMULATOR limitation, not compiler)
`unsigned long long x` with high bit set, `x >> n` -> arithmetic (sign-fill).
Correct fix is to emit `>> ($u64)` (logical), but the sim's $u64 ALU path
zeroes the operand (`Cast_Up_To_u64` -> `__mmask__(64)` UB, same class as the
__vabs nbits=64 bug). So $u64 returns 0. Compiler keeps $i64 (correct for every
value with bit63 clear = all sub-64-bit unsigned). Only `unsigned long long >>`
with the top bit set is wrong. **Report to prof** (fix __mmask__(64)); then the
compiler can emit $u64. Documented in ir_gen._binop / codegen._gen_IRBinOp.

## Signed-overflow divergences (UB, not bugs)
`INT_MIN-1`, `INT_MIN/-1`: APARA does 64-bit int math (no 32-bit wrap); gcc
wraps at 32 bits. Both are C undefined behaviour. Low priority.

## PASSED clean (good coverage)
subword truncation/extension, division/modulo incl. div-by-immediate, recursion
+ mutual recursion + deeper depth, register spilling (30 live vars), 2D arrays /
structs / direct array access, all ALU ops + nand/nor/xnor + comparisons +
logical, and `$vreduce $max`.

## data.map optimization insight (from user)
Initialized *globals* put data in data.map (loaded directly); initialized
*locals* cost runtime `$set`+`$st` to build on the stack. Measured 47 vs 63
executed instrs for the same 8-value sum. Application: precompute matmul A/BT as
initialized globals to delete the fill loop (hundreds of executed stores).

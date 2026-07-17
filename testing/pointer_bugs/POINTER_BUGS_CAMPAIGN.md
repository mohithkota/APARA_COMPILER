# Pointer bugs — finding & fixing campaign

## ✅ CAMPAIGN COMPLETE (2026-07-17, session 3): battery 15/15, zero regressions

REFACTOR_PLAN.md executed in full: central `_eval_addr` address evaluator in
`ir_gen.py` (one place for decay, pointer loads and stride scaling), wrappers
`_addr_load`/`_addr_store`/`_addr_value`, decaying `_visit_operand` at value
sites. All of RC1–RC4 fixed; ad-hoc helpers (`_visit_rvalue`,
`_ptr_stride_of_node`, `_binop` scaling block, call-arg `is_arr` hack)
deleted. Battery **t01–t15 all PASS**; real suite 0 err after every step;
full-suite A/B (78 tests) shows only improvements (t15 + `ptr_isolate/L` now
pass). `test_scalar_full` root-caused separately: program exceeds the
half-sized IMEM (loads stop at word 0x800) — not a pointer/compiler bug.
Details: compiler/STATUS.md 2026-07-17 (top entry).

---

## PROGRESS (2026-07-17, session 2)
Landed (kept, zero real-suite regression): **array-to-pointer decay at rvalue
SITES** — new `_visit_rvalue` helper + `_is_array_name` (excludes structs), used
at init-RHS and assignment-RHS. Fixes `int *p = arr` / `p = arr` and unblocks
**t09 (char*), t10 (long long*), t12 (ptr-struct), t13 (loop via *(p+i))**.
Battery now **4/15** (was 2/15); real suite clean (pointer/array/2d/struct 0 err,
matmul 256/256).

Lessons (do NOT repeat): (1) decaying in `_load_var` blanket regresses struct/2D
(t12/t13) even with the struct-excluding predicate. (2) decaying binop operands
blanket regressed `pointer/test_pointer` + t13 (interferes with array-index /
pointer-stride scaling). So decay must stay SITE-specific.

Still open: t11 (ptr comparison — needs comparison-operand decay; its earlier
"pass" was accidental value-compare), t01 (`arr+off` as arg), t02 (still 1 err),
t03/t04 (`&x`, `&arr[i]`), t05 (`arr+n` needs decay+stride scale), t06 (`q-p`),
t07/t15 (pointer STORE width), t08 (`p++`), t14 (return `a+i`).
NEXT: give array operands in `arr+n` a decay-then-scale-by-element path; add
comparison-operand decay; then RC2 (store width) mirroring the load-width fix.

---


Comprehensive pointer-variation battery, run on the fixed toolchain 2026-07-17.
Each test verifies against a gcc golden (results[]). Reproducers in this dir
(`t01`..`t15`). Run one: `python3 compiler.py tNN.c -o tNN.mcode --stack-top 0xfff8`
then `mcode_align|assemble|run -r tNN.result -v`.

## Scorecard

| Test | What it exercises | Status |
|------|-------------------|--------|
| t11_ptr_cmp     | pointer comparison (`p<q`, `p==arr`) | **PASS** |
| t12_ptr_struct  | pointer-to-struct (`s->x`, `s->x=`) | **PASS** |
| t01_param_deref | `f(int*p){p[0]+p[2]}`; `f(arr)` ok, **`f(arr+1)` fails** | partial |
| t02_local_ptr_arr | `int *p=arr; p[i]` | FAIL |
| t03_addr_of_scalar | `int *p=&x; *p; *p=..` | FAIL |
| t04_addr_of_elem | `int *p=&arr[1]` | FAIL |
| t05_ptr_arith   | `*(p+2)`, `p=p+1` | FAIL |
| t06_ptr_diff    | `q-p` | FAIL |
| t07_ptr_store   | `*p=`, `p[i]=` via local ptr | FAIL |
| t08_ptr_incdec  | `p++`, `++p`, `p--` | FAIL |
| t09_char_ptr    | `char *p=buf; p[i]` | FAIL |
| t10_ll_ptr      | `long long *p=data; p[i]` | FAIL |
| t13_ptr_loop_sum| `*(p+i)` in a loop | FAIL |
| t14_ret_ptr     | function returning a pointer | FAIL |
| t15_ptr_param_store | `void f(int*p){p[0]=v;}` | FAIL |

## What ALREADY works
- Passing a bare array as a param + **reading** `p[i]`/`*p` (fixed this session:
  pointer element LOAD width, `_ptr_elem_bytes`).
- Pointer comparison, pointer-to-struct member access.

## Root-cause hypotheses (fix order for next session)

**RC1 — array-to-pointer decay is missing for plain rvalue uses. ROOT CAUSE FOUND.**
(t02,t04,t05,t08,t09,t10,t13,t14.) `int *p = arr` loaded arr[0]'s VALUE into p
instead of `&arr[0]`. Traced: `_load_var(name)` (ir_gen.py) emits IRGlobalLoad
for a global array name, i.e. it loads element 0 instead of decaying to the
address. The FUNCTION-CALL path already decays (visit_FuncCall's `is_arr`
check, ~line 1160) which is why `f(arr)` works but `int *p = arr` doesn't.
ATTEMPTED FIX (reverted): adding the same array->address decay directly in
`_load_var` (IRGlobalAddrOf for global / IRLoadAddr for local when the name is
an array) fixed t09/t10 but REGRESSED t11 (ptr comparison) and t12 (ptr-to-
struct). So a blanket decay in `_load_var` is too broad -- it must be applied at
the specific expression sites that want a decayed array (assignment/init RHS,
binop operand for `arr+n`, return value), NOT unconditionally in `_load_var`
(comparison and struct paths break). Next session: add a `_visit_expr_decayed`
(or decay in `_binop`/assignment-RHS/return) rather than in `_load_var`.

**RC1b — storing/reloading a pointer VALUE.** Once decayed, verify the address
survives the pointer's stack slot round-trip (8-byte $i64 store/load); the L.c
mcode also showed the reloaded pointer used to deref garbage. Check after RC1.

**RC2 — pointer element STORE width.** (t07, t15.) This session fixed the LOAD
width (`_arrayref`); the STORE path (`_assign_lval` pointer/`*p` case) still uses
the wrong width ($i64 vs pointee). Mirror the load fix on the store side.

**RC3 — `arr + offset` as a function argument.** (t01 `f(arr+1)`.) Passing a
computed pointer (array base + offset) as an argument yields 0. Related to RC1
(computed pointer value) but at a call site.

**RC4 — pointer difference `q - p`.** (t06.) Needs `(q-p)/sizeof(elem)` scaling;
currently wrong.

## Note
Fixed this session (already in tree): pointer element LOAD width -- see
STATUS.md 2026-07-17 and `../ptr_isolate/F.c`. This campaign is the follow-up to
make ALL pointer variations work.

## Session-2 conclusion (2026-07-17): piecemeal fixes hit a wall
Landed (kept, 0 real-suite regression): array->pointer decay at init/assign RHS
(battery 4/15: t09,t10,t12,t13). Attempts to fix `arr+n` by extending
`_ptr_stride_of_node` + decaying operands in the `+/-` block FIXED t01 but
REGRESSED `pointer/test_pointer` (real suite) and t13 -> reverted.

**The pointer-arithmetic path (`_binop` / `_ptr_stride_of_node` /
`_scale_by_stride` / `_array_base_off`) is too interconnected for incremental
patches -- every local change regresses an already-working array/pointer test.**

RECOMMENDED next approach (dedicated refactor, not hacking): a single "evaluate
expression AS an address" path returning (base_addr, elem_stride), used uniformly
by array subscript, pointer arithmetic, address-of, and call args -- so decay and
stride-scaling live in ONE place. Build it with the t01-t15 battery + the real
suite as the regression gate.

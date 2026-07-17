# Pointer support — refactor plan (start here)

> **STATUS: EXECUTED 2026-07-17 — DONE.** All 9 steps landed, gated each step.
> Battery 15/15, real suite 0 err, full-suite A/B clean, ad-hoc helpers
> deleted, STATUS.md updated. See POINTER_BUGS_CAMPAIGN.md top entry.

Goal: make ALL pointer variations work (the `t01`..`t15` battery) with ZERO
regressions on the real suite. This plan replaces the piecemeal patching that
kept regressing (see POINTER_BUGS_CAMPAIGN.md "Session-2 conclusion").

## Read first (context, ~10 min)
1. `POINTER_BUGS_CAMPAIGN.md` — the battery scorecard, root causes RC1–RC4, and
   every approach already TRIED AND REVERTED (do not repeat them).
2. `compiler/STATUS.md` top entries — the load-width fix + array-decay-at-RHS fix
   already landed.
3. Files you'll touch: `compiler/ir_gen.py` (mainly), `compiler/codegen.py`,
   `compiler/ir.py`.

## Current landed state (safe baseline — do not lose)
- Pointer element LOAD width fixed (`_ptr_elem_bytes`, `_arrayref`).
- Array→pointer decay at init-RHS / assign-RHS (`_visit_rvalue`, `_is_array_name`).
- Battery 4/15 pass (t09, t10, t12, t13). Real suite: 0 errors.

## The core problem (why piecemeal fails)
Address computation, array→pointer decay, and element-stride scaling are
re-derived AD HOC at many sites: `_arrayref`, `_array_base_off`,
`_ptr_stride_of_node`, `_scale_by_stride`, the `_binop` +/- block,
`_assign_lval`, the call-arg `is_arr` path, `_load_var`. Any local edit to one
site contradicts another → regressions. FIX = centralize.

## The design: ONE address-evaluator
Add a single method that evaluates any expression that denotes a memory location
or a pointer, returning a uniform result:

    def _eval_addr(self, node) -> (base_temp_or_addr, elem_stride, elem_bytes)
        # base    : a Temp/const holding the BYTE ADDRESS of the location
        # stride  : DMEM stride to advance one element (for +n arithmetic)
        # elem_bytes: pointee/element width for the $ld/$st type tag

Handle these node shapes in ONE place:
- `ID` naming a global/local ARRAY   -> (&arr[0], arr_stride, arr_elem_bytes)
- `ID` naming a POINTER variable      -> (load p's value, ptr_stride=8, _ptr_elem_bytes[p])
- `ID` naming a scalar / struct        -> (&var, 8, its width)   [for &x]
- `ArrayRef base[idx]`                 -> recurse on base, add idx*stride
- `UnaryOp '*' (deref)`                -> recurse on the operand's value as address
- `UnaryOp '&' (addr-of)`              -> the address of the operand (no final load)
- `BinaryOp '+'/'-'` with a pointer/array operand
                                       -> recurse on the pointer side; scale the
                                          int side by stride; add/sub

Then EVERY site becomes a thin wrapper:
- read `p[i]` / `*p`      : (a,_,eb)=_eval_addr(node); IRLoad(res,a,0,eb)
- write `p[i]=v` / `*p=v` : (a,_,eb)=_eval_addr(lval); IRStore(a,0,v,eb)   # RC2
- `arr+n`, `&x`, `p++`, `q-p` as RVALUE : return the address temp from _eval_addr
- pointer diff `q-p`      : (byte(q)-byte(p)) / elem_bytes                 # RC4
- call arg that is array/ptr : the address from _eval_addr (replaces is_arr hack)

Keep the CURRENT working paths intact until the new one is proven, then delete
the ad-hoc ones (`_ptr_stride_of_node`, the +/- block, the is_arr branch,
`_visit_rvalue`) once _eval_addr subsumes them.

## Implementation order (test after EVERY step — see gate below)
1. Write `_eval_addr` for the READ cases only (ID-array, ID-pointer, ArrayRef,
   `*p`). Route `_arrayref` and `*p` reads through it. Gate.
2. Add `&x` / `&arr[i]` (address-of). Route `_unary '&'`. Gate. (t03, t04)
3. Add pointer arithmetic (`arr+n`, `p+n`, `p-n`) as rvalue via _eval_addr,
   returning the address temp. Route `_binop` +/- for pointer/array operands
   ONLY (leave pure-integer +/- alone). Gate. (t01, t05, t14)
4. Pointer element STORE via _eval_addr (RC2). Route `_assign_lval` `*p`/`p[i]`.
   Gate. (t07, t15)
5. `p++`, `++p`, `p--` (they are `p = p + 1` with stride). Gate. (t08)
6. `q - p` difference (RC4): both pointers -> byte diff / elem_bytes. Gate. (t06)
7. Pointer comparison `p == arr` (t11): both sides evaluated as addresses.
   Gate. (t11)
8. Route call-args through _eval_addr, delete the `is_arr` hack. Gate.
9. Delete the now-dead ad-hoc helpers; final full-suite run.

## The regression GATE (run after EVERY step; non-negotiable)
```
# battery
cd cmp_wd/testing/pointer_bugs
for f in t*.c; do n=${f%.c}; python3 ../../compiler/compiler.py $f -o $n.mcode --stack-top 0xfff8 >/dev/null 2>&1
  BIN=/home/mohithkota/complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin
  $BIN/mcode_align $n.mcode>_a 2>/dev/null; $BIN/mcode_assemble _a>_o 2>/dev/null
  e=$($BIN/mcode_run -p 0x0 -i _o -d data.map -r $n.result -v 2>&1|grep -cE "Error: PostCondition")
  echo "$n: $([ $e -eq 0 ] && echo PASS || echo FAIL($e))"; rm -f _a _o; done
# real suite (MUST stay 0 errors)
cd cmp_wd; for t in pointer/test_pointer array/test_array array/test_2d array/test_struct matmul_tests/matmul_n16; do
  d=$(dirname $t);n=$(basename $t);cd cmp_wd/$d; python3 ../../compiler/compiler.py $n.c -o $n.mcode --stack-top 0xfff8 >/dev/null 2>&1
  timeout 60 bash run.sh >/dev/null 2>&1; log=$(ls -t *.log|head -1)
  echo "$t: $(grep -ic 'Error: PostCondition' $log) err"; cd cmp_wd; done
```
RULE: if ANY real-suite test gains an error, REVERT that step immediately and
rethink — a real-suite regression is never acceptable. The battery should only
ever go UP.

## Landmines already hit (do NOT repeat)
- Decaying arrays blanket in `_load_var` -> breaks struct/2D (t12/t13).
- Decaying binop operands blanket -> breaks pointer/test_pointer + array index.
- `_global_array_elem` / `_array_elem` also contain STRUCTS (n_elems>1) — always
  use `_is_array_name` (excludes `_var_struct_type`) to mean "genuine array".
- Sub-word DMEM convention: int lives in bits[63:32]; loads/stores must use the
  element width ($i32), pointers themselves are $i64 (8-byte address).

## Definition of done
`t01`..`t15` all PASS; real suite 0 errors; ad-hoc pointer helpers removed;
STATUS.md updated. Likely also fixes the orphaned test_scalar_full.

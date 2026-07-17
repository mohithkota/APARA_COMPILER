# Struct-array support — campaign plan (start here)

> **STATUS: EXECUTED 2026-07-17 — DONE.** u7 fixed (sa.c 8/8, u7 pass); universal 11/12; pointer battery 15/15; real suite 0 err.

Goal: make arrays of structs work — `pts[i].field`, `&pts[i]`, and
`struct T *p = &pts[i]; p->field`, `(p+1)->field` — with ZERO regressions on the
struct/2D tests. Follows the gated-campaign method that made the pointer refactor
succeed (see `../pointer_bugs/REFACTOR_PLAN.md`).

## Read first (~10 min)
1. `UNIVERSALITY_CAMPAIGN.md` — the u7 root-cause writeup.
2. Reproducer: `u7_isolate_struct_array.c` (and `../ptr_isolate/sa.c`).
3. Files: `compiler/ir_gen.py` only (struct + array handling live there).

## Current state (root cause — already diagnosed)
Struct arrays are NOT modeled — they are flattened at allocation. For
`struct Pt {int x; int y;}; struct Pt pts[3]={{1,2},{3,4},{5,6}};`:
- The initializer flattens to 6 scalars, so `_alloc_global` records `pts` as a
  6-element stride-8 array (`_global_array_elem['pts']==8`), NOT a 3-element
  stride-16 struct array (`_struct_total_dmem['Pt']==16`).
- `_record_struct_var` (~ir_gen.py:650) handles TypeDecl-struct (-> _var_struct_type)
  and PtrDecl-struct (-> _var_struct_ptr_type) but NOT ArrayDecl-of-struct, so no
  element struct type is tracked for the array.
- `_structref_base_and_total_off` (~ir_gen.py:673) handles node.name of type ID,
  chained StructRef, and '->' — but NOT ArrayRef, so `pts[i].field` hits the
  `else: return Const(0), 0, 8` branch and reads address 0 -> all zeros.

GOOD NEWS: the DATA LAYOUT is already correct. Struct fields are 8-byte-strided,
so pts in data.map is `1@0x80, 2@0x81, 3@0x82, 4@0x83, 5@0x84, 6@0x85` — exactly
right for a struct array with x@+0, y@+8, struct stride 16. So this is an
ADDRESSING/ACCESS fix, not a re-layout. Existing struct layout registry is
correct: `_struct_layouts['Pt']={'x':(0,8,None),'y':(8,8,None)}`,
`_struct_total_dmem['Pt']==16`.

## Design
Model a struct array as: base + i*struct_stride + field_offset.
- struct_stride = `_struct_total_dmem[elem_struct]` (16 for Pt).
- field_offset  = `_struct_layouts[elem_struct][field][0]`.
Introduce one map: `_array_struct_elem[name] = elem_struct_name` (the struct type
of an array's elements). Then reuse the existing `_eval_addr` for the element
address and the existing `_struct_layouts` for the field offset.

## Implementation order (gate after EVERY step)
1. **Track element struct type + fix stride.**
   - In `_record_struct_var`, add an `ArrayDecl`-whose-element-is-a-struct case:
     register the struct and set `_array_struct_elem[name] = struct_name`.
   - Where the array stride is recorded (`_global_array_elem` / `_array_elem`),
     for a struct array use `_struct_total_dmem[struct]` (16), not the flattened
     per-scalar 8. Simplest: after alloc, if `name in _array_struct_elem`,
     override the stride entry to the struct size.
   - Verify with a probe: `_global_array_elem['pts']==16`. Gate.
2. **`pts[i].field` read/write.** In `_structref_base_and_total_off`, add:
   ```
   elif isinstance(node.name, A.ArrayRef):
       a = self._eval_addr(node.name)          # &pts[i] using struct stride
       base = <materialize a as an address temp>
       struct_name = self._array_struct_elem.get(<array name>, '')
       parent_off = 0
   ```
   then the existing `layout[field]` lookup adds the field offset. (_eval_addr
   returns an _Addr with gaddr/base/off; convert to a single address temp — see
   how `_addr_load`/`_addr_store` already materialize addresses.) Gate. (fixes
   `pts[i].x`, direct read + write)
3. **`&pts[i]` and pointer-to-element.** `struct T *p = &pts[i]`:
   - `_eval_addr(&pts[i])` already yields the address (step 1 gives stride 16).
   - Ensure `_var_struct_ptr_type['p']='T'` (PtrDecl-struct already does this) and
     the pointer's element stride = struct size (16) so `(p+1)->field` and `p++`
     advance by a whole struct. Set `_ptr_elem_bytes`/`_ptr_stride` for such a
     pointer to the struct size.
   - `p->field` then = load[p + field_off] via the existing '->' path. Gate.
     (fixes `p->x`, `(p+1)->x`)
4. Full reproducer `u7_isolate_struct_array.c` and `u7_struct_ptr_algo.c` pass.

## Regression GATE (run after EVERY step; non-negotiable)
```
BIN=/home/mohithkota/complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin
COMP=/home/mohithkota/complier_Apara/cmp_wd/compiler/compiler.py
# struct/2D MUST stay green (these are what struct-array changes can break):
for t in array/test_struct array/test_2d array/test_array pointer/test_pointer matmul_tests/matmul_n16; do
  d=cmp_wd/$(dirname $t); n=$(basename $t); (cd $d; python3 $COMP $n.c -o $n.mcode --stack-top 0xfff8 >/dev/null 2>&1
   bash run.sh >/dev/null 2>&1; log=$(ls -t *.log|head -1); echo "$t: $(grep -ic 'Error: PostCondition' $log) err"); done
# pointer battery must stay 15/15; universal battery must only go UP.
```
RULE: any real-suite / struct / 2D / pointer-battery regression -> REVERT that
step immediately. u7 should go green without breaking test_struct / test_2d.

## Landmines
- Struct globals also land in `_global_array_elem` (n_elems>1) — use the element
  struct type / `_is_array_name` to distinguish an ARRAY-of-structs from a single
  struct. A single `struct Pt g;` is a scalar struct var (in `_var_struct_type`),
  NOT a struct array.
- Do not disturb the flat data layout (it's already correct); only change
  addressing (stride 16 + field offset).
- Local struct arrays: mirror the global fix (frame address + i*16 + field_off),
  using the unified $i32/bits[63:32] element convention.

## Definition of done
`u7_isolate_struct_array.c` + `u7_struct_ptr_algo.c` PASS; `array/test_struct`,
`array/test_2d`, `array/test_array`, `pointer/test_pointer` 0 err; pointer battery
15/15; universal battery up. Update STATUS.md + UNIVERSALITY_CAMPAIGN.md.

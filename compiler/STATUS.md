# APARA C Compiler — Project Status

---

## 2026-06-17 — Register Spilling (Latest)

### Done today — register spilling when >28 temps are simultaneously live

#### Problem
APARA has only 32 physical registers. The compiler uses a pool of 28 (`$r1`–`$r25`, `$r29`–`$r31`).
If a C expression keeps more than 28 temps simultaneously live, the old code raised `RuntimeError`.

#### Trigger pattern
Right-nested function calls force spilling: `f01() + (f02() + (f03() + ... + f30()))`.  
`ir_gen` visits LEFT before RIGHT, so `_t1` (result of f01) stays live in a register while ALL
inner calls are evaluated. By the time f29() is being processed, `_t1`..`_t28` are all live
simultaneously — 28 registers full — and f29()'s return value needs a 29th. Spill fires.

#### Implementation (codegen.py v6)

| Component | Description |
|-----------|-------------|
| `MAX_SPILL_SLOTS = 64` | 64 × 8 = 512 bytes reserved in every function frame |
| `SPILL_RESERVE = 512` | Added to `fs = ir.frame_size + CALLER_SAVE_BYTES + SPILL_RESERVE` |
| `_spill_map` | `{temp_name → FP_offset}` — which temps are currently evicted to RAM |
| `_spill_counter` | monotonically assigns spill slot indices (reused across calls via `_get_spill_slot`) |
| `_get_spill_slot(name)` | allocates slot `-(frame + CALLER_SAVE_BYTES + 8 + idx*8)` on first call |
| `_spill_evict(protect)` | picks unprotected live temp, emits `$st [FP+slot] reg`, frees register |
| `_alloc_reg(temp, protect)` | unified: fast-path if in reg; reload-from-spill if evicted; fresh-allocate otherwise |
| `_safe_borrow(protect)` | spills before borrow if pool empty |

#### Key bugs found and fixed

1. **`_alloc_reg` spill reload**: the "previously spilled" branch unconditionally called
   `_spill_evict()` even when the pool had free slots.  With only 2 live temps and all in
   the protect set this caused a deadlock.  **Fix**: guard with `if not self._ra.has_free()`.

2. **Post-call stale register**: after `$call`, callee may clobber pool registers.  Cannot
   call `_spill_evict()` at that point — it would store the wrong (clobbered) value.
   **Fix**: when pool is full after the call, load the victim's CORRECT value from its
   caller-save slot, then write to spill, before freeing the register.

#### Spill slot layout (FP-relative)

```
FP - 0           prologue save of old FP  ($r26)
FP - 8..(-8-n*8)  local vars from ir_gen  (frame_size bytes)
FP - (fs+8)..(fs+224)   caller-save area  (28 × 8 = 224 bytes)
FP - (fs+232)..(fs+743)  spill area        (64 × 8 = 512 bytes)
```

#### Verification — test_spill.c

30 functions f01..f30 each return their index. `main` computes the sum via right-nested
addition (see `new_isa_tests/test_spill.c`).  Hardware-expected result: `r1 = 465 = 0x1d1`.

Spill instructions confirmed in generated mcode at offsets `[$r26 + -304]`, `-312`, `-320`
(beyond the caller-save area that ends at `-296`).

All 18 tests pass after the fix.

---

## 2026-06-17 — Struct Member Access (Latest)

### Done today — structs

| Feature | Status | Notes |
|---|---|---|
| `struct Foo { ... };` standalone definition | Done | pycparser: `Decl(name=None, type=Struct(...))` |
| `struct Foo var;` local + global | Done | 8 bytes per field (APARA alignment) |
| `var.field` read | Done | `IRLoad(base_addr, field_offset)` |
| `var.field` write | Done | `IRStore(base_addr, field_offset, val)` |
| `ptr->field` read | Done | pointer value is the base |
| `ptr->field` write | Done | same |
| `typedef struct {...} Name;` | Done | anonymous struct gets typedef name |
| Nested struct: `struct A { struct B b; }` | Done | field offsets accumulate recursively |
| Chained access: `ptr->outer.inner` | Done | recursive `_structref_base_and_total_off` |
| `&s.field`, `&p->field` | Done | address = base + field_offset |
| `struct Foo *p` param | Done | pointer-to-struct correctly 8 bytes |
| Struct initializer: `s.x = ...; s.y = ...;` | Done | field-by-field assignment |

Fixes required along the way:
- Standalone struct `Decl(name=None, type=Struct(...))` was previously ignored (type=Struct, NOT TypeDecl(Struct))
- `_elem_size(PtrDecl)` was returning pointed-to struct size (40 bytes for `struct Line *`) — changed to always return 8
- `_record_struct_var` not called in param loop — added

Test: `array/test_struct.c` — 8 sub-tests: local/global field access, pointer (`->`), pass-by-pointer, nested struct, chained access. All compile clean.

---

## 2026-06-17 — 2D Arrays

### Done today — 2D array support

| Feature | Status | Notes |
|---|---|---|
| `type mat[R][C]` global declaration | Done | rows×cols×8 bytes in DMEM |
| `type mat[R][C]` local (stack) | Done | same layout on frame |
| `mat[i][j]` read | Done | offset = i×(C×8) + j×8 |
| `mat[i][j]` write | Done | same offset |
| 2D array param `f(type A[R][C])` | Done | decays to pointer; inner dims tracked |
| Array name decay in call args | Done | name alone → passes base address |
| Verified: `gMat[1][2]`→offset 40 (3×3) | ✓ | 1×24 + 2×8 = 40 |
| Verified: `loc[1][1]`→offset 40 (2×4 local) | ✓ | 1×32 + 1×8 = 40 |
| matmul2 test (2×2 triple-nested loop) | Compiles ✓ | hardware run pending |

Test: `array/test_2d.c` — 6 sub-tests covering global/local 2D write/read, row-major isolation, 2×2 matmul, read/write via function param.

---

## 2026-06-17 — Pointer Support

### Done — pointer arithmetic

| Feature | Status | Notes |
|---|---|---|
| `long long *p` declaration | Done | PtrDecl detected, stride=8 recorded |
| `p = &x` pointer to local | Done | IRLoadAddr gives address of local |
| `p = &arr[i]` pointer to array element | Done | base + i*stride computed |
| `*p` dereference read | Done | IRLoad from pointer value |
| `*p = val` dereference write | Done | IRStore through pointer |
| `p + n`, `p - n` | Done | n scaled by stride=8 before add |
| `p++`, `p--`, `++p`, `--p` | Done | increment/decrement by stride=8 |
| `p += n`, `p -= n` | Done | n scaled by stride=8 |
| `p[i]` pointer indexing | Done | loads pointer value, uses as base |
| `&arr[i]` address of element | Done | `_unary '&'` now handles ArrayRef |

All 15 tests pass (14 prior + test_pointer.c with 10 sub-tests).

---

## 2026-06-17 — 28-register allocator

### Done today

#### 1. Full 28-Register Dynamic Allocator (v5)

**Before**: 11 registers permanently wasted as fixed/scratch.
**After**: only 2 registers are fixed forever — `r0` (hardware ZERO) and `r28` (GBASE).

| Register | Before | After |
|----------|--------|-------|
| r0 | ZERO (fixed) | ZERO (fixed — hardware) |
| r1 | RET (fixed) | **Pool** — recycled between calls |
| r2–r5 | ARG (fixed) | **Pool** — recycled between call sites |
| r6–r25 | GEN pool (20) | **Pool** (same) |
| r26 | FP (fixed) | Reserved per-function (frame pointer) |
| r27 | SP (fixed) | Reserved per-function (stack pointer) |
| r28 | GBASE (fixed) | GBASE (fixed — global base address) |
| r29 | ONE=1 (fixed) | **Pool** — eliminated, no longer needed |
| r30 | SCR (fixed) | **Pool** — borrowed dynamically as scratch |
| r31 | SCIDX (fixed) | **Pool** — borrowed dynamically as scratch |

**Pool size**: 28 registers (`$r1`–`$r25`, `$r29`–`$r31`).

#### 2. Key Algorithmic Changes

- **Unconditional jump**: was `? $r29 > $goto label` (requires ONE=1).
  Now: `? ($i64) $r0 == $goto label` (0==0 is always true — no dedicated register needed).
- **Scratch registers**: `borrow()` / `unborrow()` dynamically pop/push from the free pool
  for each intermediate computation (address computation, subtraction for compare, etc.).
- **`$pack` consecutive pair**: `borrow_pair()` scans the free list for any two
  physically consecutive register numbers at emit time — works with all 28 pool regs.
- **Call site aliasing fix**: arguments always read from the saved stack slots (not live regs)
  before being written to `r2–r5`, preventing register-aliasing bugs.
- **Return value capture order**: `r1` (return value) copied to `dest` BEFORE restoring
  saved registers, because restoring may clobber `r1` if it held a live temp.
- **Always-on preprocessing**: `gcc -E -P` now runs unconditionally, stripping comments
  and `#define`/`#include` — no longer requires `--preprocess` flag.

#### 3. Verification

All 14 test programs compile and produce correct results:

| Program | Status | Bundle reduction |
|---------|--------|-----------------|
| test_alu | [OK] | 48% |
| test_branch | [OK] | 30% |
| test_array | [OK] | 36% |
| test_ldst | [OK] | 50% |
| test_cast | [OK] | 48% |
| test_cmov | [OK] | 53% |
| test_dot | [OK] | 55% |
| test_fsqrt | [OK] | 54% |
| test_logic | [OK] | 44% |
| test_pack | [OK] | 56% |
| test_scalar_full | [OK] | 39% |
| test_slice | [OK] | 54% |
| test_vadd | [OK] | 55% |
| test_vreduce | [OK] | 56% |

Register proof: `test_scalar_full` uses **all 32 registers** (`$r0`–`$r31`).

---

## 2026-06-16

### Done
- Expanded GEN register pool from 17 → 20 registers (r6–r25)
- Fixed constant-vs-constant comparison bug in `_emit_cond_branch`
- Compiled and verified comprehensive scalar test (`test_scalar_full.c`)

### Register layout (at that time — now superseded by v5 above)
```
r6–r25 = GEN (20)  r29=ONE  r30=SCR  r31=SCIDX
```

---

## 2026-06-15

### Done
- Implemented ALL missing ISA instructions (9 new IR node types + codegen):
  - `$nop`, `~|` NOR, `~&` NAND, `~~` XNOR
  - `$fsqrt`, `$cmov`, `$slice`, `$pack`, `$cast`
  - `$v +/-/*` vector arithmetic, `$dot/$dot $accumulate`, `$vreduce`
- Fixed 3 bugs in `ir_gen.py`:
  - Hex literal `0x0F` stripping 'F' → parsed as 0
  - Function declarations creating false DMEM globals
  - Standalone call statements not dispatching to `_call()` handler
- Created 10 test programs in `new_isa_tests/`; all produce correct mcode

---

## 2026-06-14 (earlier)

### Done
- Verified load/store on hardware: `test_ldst.c` — all 5 PostConditions passed
- Verified all 6 branch comparisons on hardware: `test_branch.c` — all pass
- Implemented VLIW bundle optimizer (`bundler.py`) — RAW/WAW hazard detection,
  greedy packing up to 8 instructions/bundle
- Hardware verified bundled mcode — ALU, LDST, branch, array all pass

### Bundle reduction results (hardware verified)
| Program | Before | After | Reduction | Hardware |
|---|---|---|---|---|
| test_alu | 82 | 41 | 50% | All 11 PostConditions ✓ |
| test_ldst | 52 | 25 | 51% | All 6 PostConditions ✓ |
| test_branch | 106 | 76 | 28% | r1=0x1 correct ✓ |
| test_array | 55 | 34 | 38% | r1=0x96 correct ✓ |

---

## ISA Instruction Coverage (100% opcodes)

| # | Instruction | How exposed in C |
|---|-------------|-----------------|
| 1–4 | `+ - * /` | `a+b`, `a-b`, `a*b`, `a/b` |
| 5 | `\|` | `a\|b` |
| 6 | `&` | `a&b` |
| 7 | `^` | `a^b` |
| 8 | `~\|` | `__nor(a,b)` |
| 9 | `~&` | `__nand(a,b)` |
| 10 | `~~` | `__xnor(a,b)` |
| 11 | `<<` | `a<<n` |
| 12 | `>>` | `a>>n` |
| 13 | `$fsqrt` | `__fsqrt_f32(x)` etc. |
| 14–16 | `$v +/-/*` | `__vadd/vsub/vmul_vi32()` etc. |
| 17 | `$dot` | `__dot_vi16(a,b)` |
| 18 | `$dot $accumulate` | `__dot_acc_vi16(acc,a,b)` |
| 19 | `$vreduce` | `__vreduce_vi32(v)` |
| 20–25 | `? ==,!=,>,>=,<,<=` | if/while/for conditions |
| 26 | `$call` | function call |
| 27 | `$return` | return statement |
| 28 | `$cmov` | `__cmov_gt/lt/eq/ge/le/ne(check,t,f)` |
| 29 | `$ld` | variable/array read |
| 30 | `$st` | variable/array write |
| 31 | `$set` | large constant loading |
| 32 | `$slice` | `__slice(val, hi, lo)` |
| 33 | `$cast` | `(int8_t)x`, `(int16_t)x` etc. |
| 34 | `$pack` | `__pack(a, b, rbits, sbits)` |
| 35 | `$nop` | `__nop()` |
| 36 | `$null` | bundle padding (internal) |
| 37 | `$halt` | `halt()` or program end |

---

## Compiler feature status

| Feature | Status | Notes |
|---|---|---|
| C parsing (always preprocessed via gcc -E) | Done | No flag needed |
| AST → Three-Address IR | Done | All C operators, all control flow |
| IR → APARA mcode | Done | |
| Register allocation | **Done — 28 regs, fully dynamic** | All 32 registers used |
| Global variables | Done | Hardware verified |
| ALU (12 ops: + - * / % & \| ^ ~ << >> + synthetic %) | Done | Hardware verified |
| NOR / NAND / XNOR | Done | via intrinsics |
| Load / Store ($i64) | Done | Hardware verified |
| If/else, all 6 comparisons | Done | Hardware verified |
| While / for / do-while loops | Done | Hardware verified |
| Switch / case / break | Done | |
| Compound assignments (+=, -=, *=, etc.) | Done | |
| Ternary operator ?: | Done | |
| Logical &&, \|\|, ! | Done | |
| Pre/post increment ++ / -- | Done | |
| 1D arrays (global + local) | Done | Hardware verified |
| Local variables (stack frame) | Done | Hardware verified |
| Function calls (up to 4 args) | Done | |
| Multiple functions | Done | |
| Recursion | Done | |
| data.map generation | Done | |
| result file generation | Done | |
| run.sh generation | Done | |
| VLIW bundling optimizer | Done | Hardware verified, 30–56% reduction |
| $fsqrt (f4/f8/f16/f32/f64) | Done | via intrinsics |
| $cmov (all 6 conditions) | Done | via intrinsics |
| $slice | Done | via intrinsic |
| $cast (scalar int/float) | Done | via C cast syntax |
| $pack (dynamic consecutive pair) | Done | via intrinsic |
| $v +/-/* (vector arithmetic) | Done | via intrinsics |
| $dot / $dot $accumulate | Done | via intrinsics |
| $vreduce | Done | via intrinsics |
| Const-vs-const comparison folding | Done | |
| Sub-word LD/ST ($i32,$i16,$i8) | Blocked | Engine hardware bug |
| Register spilling (>28 live vars) | **Done** | 64-slot spill area; hardware-pending |
| Struct member access (`s.x`, `p->x`, nested) | **Done** | 8B/field, recursive chain |
| Function pointers | Not started | |
| Pointer arithmetic (all ops) | **Done** | stride=8 APARA alignment |
| 2D arrays (global + local + params) | **Done** | row-major, array decay |
| Float arithmetic (+,-,*,/) | Not started | Only sqrt via intrinsic |
| String literals | Partial | address-of only |
| Variadic functions | Not started | |

## Remaining work (priority order)

| # | Feature | Effort | Blocker |
|---|---------|--------|---------|
| 1 | **Register spilling** (>28 live vars) | Done ✓ | — |
| 2 | **Function pointers** | Medium | None — next |
| 3 | **Float arithmetic** (+,-,*,/) | Low | ISA `$fadd/$fsub/$fmul/$fdiv` needed |
| 4 | **Sub-word LD/ST** ($i32/$i16/$i8) | Low | **Hardware engine bug** |

**Overall compiler completeness: ~88% of a basic C compiler**

---

## Directory structure

```
cmp_wd/
├── compiler/               ← compiler source
│   ├── compiler.py        ← entry point + preprocessing + data.map + result file
│   ├── ir.py              ← 37 IR node class definitions
│   ├── ir_gen.py          ← C AST → Three-Address IR (pycparser NodeVisitor)
│   ├── codegen.py         ← IR → APARA mcode  (v6: 28-reg dynamic allocator + spilling)
│   ├── bundler.py         ← VLIW bundle optimizer (RAW/WAW hazard detection)
│   └── STATUS.md          ← this file
├── alu/                    ← test_alu (hardware ✓)
├── array/                  ← test_array (hardware ✓)
├── branch/                 ← test_branch (hardware ✓)
├── ldst/                   ← test_ldst (hardware ✓)
└── new_isa_tests/          ← 14 ISA instruction tests (all compile ✓)
```

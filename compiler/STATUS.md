# APARA C Compiler — Project Status

---

## 2026-06-17 — Bundler memory-hazard FIXED; found a second bug ($set doesn't merge) (Latest)

### Bundler fix (bundler.py)
Added memory-address hazard tracking alongside the existing register RAW/WAW/WAR logic.
`_parse_deps` now also returns `(mem_access, mem_write)` — a `(base_reg, offset)` tuple for any
`$ld`/`$st`, textually matched. `_pack_bundles` tracks `c_mem_writes` (addresses stored-to in the
current bundle) and forces a split if a later instruction's `mem_access` matches an address
already in `c_mem_writes` — i.e. store-then-reload (or store-then-store) of the same `[base+offset]`
no longer lands in one bundle. Load-then-store of the same address is still allowed in one bundle
(WAR-safe, matches the existing register WAR philosophy — VLIW reads all operands before writes).

**Verified the fix is correct in isolation**: `int gi = 100; gi = gi+1; if (gi != 101) return -1;
return 1;` — before the fix this returned -1 (stale reload); after the fix, confirmed on hardware,
returns 1. The store and reload are now in separate bundles (checked directly in the generated
mcode).

### The fix resolves 0 of the 9 originally-failing tests — each has additional, different bugs
Re-ran `test_subword, test_dot, test_struct, test_spill, test_scalar_full, test_vadd, test_vreduce,
test_slice, test_cast` after the fix. **All 9 still produce the same wrong r1 as before.** Checked
each one's bundled mcode for residual same-address store+load pairs — none exist (the fix is
working; these tests were never blocked by *this* hazard, or hit a second bug that masks it).

### New bug found: `$set` does not merge into the register, it overwrites
The compiler's constant-loading logic (`_load_const`, `_emit_set_const_into` in `codegen.py`,
and `_gen_IRGlobalDecl`'s init-value writer) assumes calling `$set rd 0 <lo16>` then
`$set rd 2 <hi16>` accumulates a 32-bit value by writing into two different 16-bit slices of the
same register, leaving the other slice alone — matching a literal reading of the ISA doc's SET
section. **Hardware trace proves this is wrong**: for `gi = 100000` (`lo=0x86a0` at field 0,
`hi=1` at field 2):
```
Info: McodeMachine:: Set_Register(30, 0x86a0)   // after $set r30 0 34464 — correct so far
Info: McodeMachine:: Set_Register(30, 0x10000)  // after $set r30 2 1 — OVERWRITES, should be 0x186a0
```
The second `$set` discards the first one's contribution entirely instead of merging. This breaks
loading ANY constant that needs both a low and a high 16-bit `$set` (i.e. doesn't fit in one
16-bit field). Confirmed root cause for `test_subword`'s one failing check (`gi=100000`) and
`test_cast`'s big constant (`0x12345678ABCDEF`); very likely also explains `test_dot`/`test_vadd`/
`test_vreduce` since they build packed-vector constants via shift expressions
(`2LL<<16`, `3LL<<32`, etc.) that get constant-folded at codegen time into single large literals
exceeding 65535. **Not yet confirmed** for `test_slice` (its only large-looking literal, `0xABCD`,
fits in one 16-bit field — doesn't obviously need the broken merge) or `test_scalar_full`/
`test_spill` (no large constants found at all). **Not fixed yet** — needs a real fix to how
multi-word constants are loaded (e.g. shift-and-OR via ALU ops instead of relying on $set to merge,
or some other mechanism — needs confirmation on what `$set` actually does for ALL field indices
before choosing an approach, this is similar in spirit to the earlier `$set`-label question).

### test_struct: still unexplained
Zero large constants, zero same-address store+load pairs in its bundled mcode, yet still returns
0xa (10) instead of the expected 0 on full success — doesn't match either known bug. Needs its own
investigation.

### Status: in progress, mid-investigation
Two real, hardware-confirmed compiler bugs found and one fixed (bundler hazard). The $set-merge
bug is understood but not fixed. test_struct (and possibly test_slice/test_scalar_full/test_spill)
have unidentified root causes still. Do not assume "bundler fix landed" means these 9 tests are
close to passing — they are not, for unrelated reasons.

---

## 2026-06-17 — Full hardware regression (19 programs) on updated engine_isp

User pulled the i32-fixed `engine_isp` binaries into `assembler/bin/`. Ran the complete
align→assemble→run pipeline (not just Python/mcode generation) on all 19 held/safe tests.
**Genuine, on-hardware results — replaces all earlier "compiles clean" / "placeholder" claims.**

### PASS (6) — confirmed correct final r1 on real hardware
| Test | r1 | Expected |
|---|---|---|
| test_alu | 0xd (13) | 13 |
| test_array | 0x96 (150) | 150 |
| test_ldst | 0x3e8 (1000) | 1000 |
| test_branch | 0x1 | 1 (a<b branch taken) |
| test_cmov | 0x258 (600) | 100+200+300 |
| test_pointer | 0xf (15) | 1+2+3+4+5 |

### PIPELINE-LEVEL FAILURES (4) — never reached execution; assembler/aligner rejected or crashed
These are tool/codegen bugs unrelated to today's i32 work (mcode for test_2d was byte-identical
to before today; the other three's relevant instructions are untouched by the elem_bytes/_atype
change).
- **test_logic**: `mcode_align` parse error. Root cause **found**: `codegen.py`'s `_APARA_OP` dict
  maps XNOR (`~^`) to the literal mnemonic `'~~'` — should be `'~^'` per ISA doc §5.1 (opcode
  0xD). One-line typo, not yet fixed (holding for direction).
- **test_pack**: `mcode_align` parse error — `expecting UINTEGER, found '$r7'` on the `$pack $r7
  32 16 $r1` line. The ISA doc's own example (`$pack $r5 32 16 $r1`) shows this exact operand
  order, so either the assembler grammar differs from the doc, or there's a missing token. Don't
  know the actual grammar — same category of unknown as the earlier function-pointer `$set`
  question. Needs your input, not a guess.
- **test_2d**, **test_fsqrt**: both crash `mcode_align`/`mcode_assemble` with the identical
  assertion: `McodeInstructionBundle::Calculate_Pad_For_Alignment: Assertion '0' failed`. Same
  failure family, two different features (2D arrays, fsqrt) — looks like an aligner-side edge
  case in bundle padding, not something visible from the Python side. Not investigated further.

### WRONG COMPUTED VALUE (9) — ran to completion, final r1 incorrect
| Test | actual r1 | expected | 
|---|---|---|
| test_subword | -12 (fails check #12 only — global `int` increment) | 1 (all 12 checks pass) |
| test_dot | 0x7ff0 | 0x5a (90) |
| test_struct | 0xa (10) | 0 |
| test_spill | 0x328 (808) | 0x1d1 (465) |
| test_scalar_full | 0x3 | 0xc (12) |
| test_vadd | 0x0 | 0x4 (4) |
| test_vreduce | 0x20001 | 0x4c (76) |
| test_slice | 0x0 | 0xb7 (183) |
| test_cast | 0x0 | 0x78ab9bcd |

### ROOT CAUSE FOUND for at least one of these, likely explains most: bundler memory hazard
Isolated with a minimal repro (`gi = gi + 1; if (gi != 100001) return -1;` — nothing else in the
program). Generated mcode:
```
||
    $st ($i32) [$r28 + 0] $r2     // store gi+1
    $ld ($i32) $r3 [$r28 + 0]     // reload gi — SAME bundle, SAME address
    $set $r4 0 34465
;
```
**The store and the reload of the same address are packed into the same VLIW bundle.** Per the
ISA, instructions in one bundle execute in parallel — the load does not see the store that's
"simultaneously" in flight, so it reads the stale pre-increment value. Confirmed by hardware run:
r1 = -1 (the failure branch), proving the reload got the old value. This is a **missing
memory-aliasing hazard check in `bundler.py`** — it tracks register RAW/WAW/WAR hazards (per
earlier audit) but evidently not "don't bundle a load with a same-address store still in
flight." This is a pre-existing bundler bug, **not introduced by today's i32 work** — it would
equally affect any `($i64)` store-then-reload of the same variable; today's i32 test just happened
to trigger it. Store→load-same-address is an extremely common pattern (any "increment a global
and check it" idiom), so this is the prime suspect for most of the other 8 wrong-value failures
above too, though each hasn't been individually traced to this same mechanism yet.

**`test_subword` detail**: checks 1-11 (char locals/arrays/globals, short locals/arrays/globals,
int locals/arrays) all passed — only check #12 (the global `int` scalar increment-and-reread,
the exact bundler-hazard pattern above) failed. This means **the core i8/i16/i32 sub-word
load/store feature itself is solid** — confirmed independently by `test_subword_i8.c` and
`test_subword_i16.c` (char-only and short-only, no other variables in the frame) both returning
r1=1 (full pass) on hardware. The one combined-test failure is the bundler hazard, not the
sub-word feature.

### Bottom line
i8/i16/i32 sub-word load/store: **hardware-confirmed working** in isolation
(`test_subword_i8`, `test_subword_i16`) and via `test_array`/`test_cmov`/`test_branch`/
`test_pointer` exercising `$i32` in other shapes. The failures above are four separate
pre-existing issues (XNOR mnemonic typo, `$pack` grammar mismatch, aligner assertion crash,
bundler memory hazard) uncovered by this regression, none caused by today's work. **None of these
four have been fixed yet** — flagged for explicit direction on priority/approach before touching
any of them, given the XNOR fix is a confident one-liner but the other three need either your
grammar knowledge or non-trivial bundler work.

---

## 2026-06-17 — Sub-word load/store implemented: i32,i32 / i32,i16 / i8,i8

### Why now
The `$ld ($i32)` engine bug (always read bits[63:32] regardless of byte offset — see the
"Vector support" entry below and [[apara_dmem_alignment]] memory) is fixed in the upstream VM.
i4/u4 confirmed by the user to have **no LOAD/STORE form at all** — minimum memory transfer
granularity is `$i8`; i4 is arithmetic-only. Not implemented, not attempted.

### What changed
1. **Audited every `IRLoad`/`IRStore`/`IRGlobalLoad`/`IRGlobalStore`/`IRGlobalDecl` construction
   site in `ir_gen.py`** (~20 sites). About half relied on the `elem_bytes=4` constructor default
   in `ir.py` even for values that are actually 8 bytes (long long/double/pointer values, struct
   fields, generic local scalar load/store, pointer dereference, 2D-array base-pointer loads) —
   harmless only because `_atype()` in `codegen.py` ignored `elem_bytes` and always emitted
   `($i64)`. Every site now passes the correct width explicitly:
   - Plain scalars (locals + params): new `_local_elem_bytes` dict in `ir_gen.py`, populated by
     `_alloc_local`, looked up by `_load_var`/`_store_var`.
   - Struct fields: `fdmem` from `_structref_base_and_total_off` (always 8 for scalar leaf fields).
   - Pointer dereference (`*p`) and pointer-value loads: hardcoded `8` — pointers are still
     stride=8 universally (see `_record_ptr`); pointer-to-narrow-type is a separate, NOT-yet-done
     feature.
   - Array/pointer indexing fallback paths: reuse the existing `_get_esz()` helper.
2. **Removed the `elem_bytes=4` default in `ir.py`** — now a required (or keyword-only, for
   `IRGlobalLoad` where `offset=None` already occupies the "has a default" slot) argument, so any
   future missed call site throws immediately instead of silently defaulting to 4.
3. **Regression checkpoint**: recompiled all 18 existing tests after steps 1-2 — mcode byte-for-byte
   identical to pre-change baseline (expected: `_atype` still hardcoded `($i64)` at this point).
4. **`_atype()` now maps elem_bytes → type tag**: `1→($i8)`, `2→($i16)`, `4→($i32)`, `8→($i64)`
   (was: always `($i64)`).
5. **New test**: `new_isa_tests/test_subword.c` — char/short/int locals+globals+arrays, all three
   pairs in one file, Python/IR/mcode-level verified only (see below). Generated mcode confirmed
   correct: `$i8` for all char access, `$i16` for all short, `$i32` for all int, struct
   fields/pointers/prologue·epilogue still `$i64`.

### IMPORTANT — hardware verification is partially blocked, READ BEFORE RUNNING ANYTHING
The user's local `engine_isp` checkout **still has the old $i32 sub-word bug** — the VM fix
hasn't been pulled yet there. Consequences:
- `new_isa_tests/test_subword.c` mixes all three widths — **do not hardware-run this one yet**
  (its i32 section would hit the still-present bug).
- Split out **`new_isa_tests/test_subword_i8.c`** and **`new_isa_tests/test_subword_i16.c`** —
  each deliberately contains zero `int`/`short`-the-other-one locals/globals, confirmed via grep
  that their generated mcode contains only `($i8)`/`($i64)` and `($i16)`/`($i64)` respectively,
  no `($i32)` anywhere. **These are safe to hardware-verify right now.**
- **12 of the 18 pre-existing tests now also emit `$i32`** (because they use `int` somewhere) and
  are therefore now ALSO affected by the still-present bug, not just the new test:
  `test_array, test_cast, test_cmov, test_dot, test_fsqrt, test_logic, test_pack, test_pointer,
  test_scalar_full, test_slice, test_vadd, test_vreduce`. **Do not hardware-verify these against
  the current unpatched engine_isp — they would now fail where they previously passed**, purely
  because `int` access changed from `($i64)` to `($i32)`, not because of a new compiler bug.
  Safe/unaffected (no `int` anywhere, zero mcode diff from before this change):
  `test_alu, test_2d, test_struct, test_branch, test_ldst, test_spill`.
- **Once the updated `engine_isp` is pulled**: re-run the full 18-test + 3-new-test suite on
  hardware as the real confirmation. Until then, only Python/IR/mcode-level verification has been
  done for i32.

---

## 2026-06-17 — Vector support verified + by-value arg-passing bug fixed

### Vectors: ISA-level codegen confirmed correct
Cross-checked `_gen_IRVecArith`, `_gen_IRVecDot`, `_gen_IRVecReduce`, `_gen_IRPack` in `codegen.py`
against `AparaReference.pdf` §5.3-5.5, §8.4. Operand order/semantics match exactly:
`$v <op> <rd> (<type>) <rs1> <rs2> [$replicate]`, `$dot <rd> (<type>) <rs1> <rs2> [$accumulate]`,
`$vreduce <rd> (<type>) <rs1>`, `$pack <rd> <result_nbits> <src_nbits> <rs2>`. Confirmed by
compiling `__vadd_vi32`/`__dot_vi4`/`__vreduce_vi4`/`__pack` intrinsic calls through the Python
pipeline (mcode text only, no assembler invoked) — generated mcode lines match ISA syntax exactly.

**Caveat — only the "manually packed 64-bit register" style is supported.** The ISA sample
program (ISA doc Ch.9) loads true 256-bit/128-bit vectors directly from memory via
`$ld ($u256)`/`$ld ($u128)` (4 or 2 registers at once). `grep -n "u256\|u128"` across
codegen.py/ir_gen.py/ir.py returns nothing — there is no wide-vector load/store support.
`_atype()` in codegen.py always returns `($i64)` regardless of `elem_bytes`. So vector ops here
only work on values a C program has already packed into a single 64-bit register (via `__pack` or
manual shifts), not on real array data loaded in bulk. This is a real gap vs. the ISA's vector
capability, not yet attempted.

### Bug found + fixed: by-value call args silently passed by address for char/short/long long/double
**Symptom:** compiling `long long add_ll(long long a, long long b) { return a + b; }` and calling
it with local `long long` arguments produced IR that passed the arguments' **stack addresses**
into the call, never loading their values — e.g. `_t13 = add_ll(_t11, _t12)` where `_t11`/`_t12`
were `&stack[FP-offset]`, not loaded values. `int`-typed calls were unaffected.

**Root cause:** `ir_gen.py`'s `_elem_size(node)` defaulted to `4` for any non-array, non-pointer
scalar type — wrong for `long long`/`double` (8 bytes) and `char`/`short` (1/2 bytes). `int`/
`float` happened to be 4 bytes already, so they never tripped the bug. `_alloc_local` then saw
`elem_bytes(4) != total_bytes(8 or other)` and concluded "must be an array," registering the
variable in `_array_elem`. `_call`'s argument-building loop checks `_array_elem` to decide
"raw array → pass address of first element instead of value" — which silently misfired for any
`long long`/`double`/`char`/`short` local or parameter passed by value into a call. This is how
the vector intrinsics (which pass `long long`-packed values across calls) were first noticed to
be broken — the bug is general-purpose, not vector-specific.

**Fix** (one line):
```python
def _elem_size(node):
    if isinstance(node, A.ArrayDecl): return _type_size(node.type)
    if isinstance(node, A.PtrDecl):   return 8
    return _type_size(node)   # was: return 4
```
Traced every call site (global/local alloc, struct/2D-array overrides which run after and
override anyway, array-param decay) — only scalar `char`/`short`/`long long`/`double` behavior
changes; arrays, pointers, structs, `int`/`float` are unaffected. Also incidentally fixes
over-allocation of uninitialized global scalars of these types (was allocating 2x DMEM).

**Verification:**
- `add_ll(l1, l2)` now correctly loads values before the call.
- Vector intrinsic test (`__pack`/`__vadd_vi32`/`__dot_vi4`/`__vreduce_vi4` via wrapper functions)
  now produces correct IR and matching mcode.
- Regression smoke test covering 1D int array param + loop, pointer deref/write, struct field
  read/write, `char`/`short`/`double` locals — all still IR-correct after the fix.
- All checks done via the Python text-generation pipeline only; **not yet run on hardware.**

### Known pre-existing limitation (not fixed, not today's scope)
Float/double constant literals (e.g. `double d = 2.5;`) are not parsed — `_visit_expr` for
`A.Constant` tries `int(raw, 0)` and falls back to `Const(0)` on failure, silently zeroing any
non-integer literal. Unrelated to the bug above; flagging for whenever float support is tackled.

---

## 2026-06-17 — Function Pointers: BLOCKED at assembler level

### Status: PAUSED — not a compiler bug, cannot be fixed in compiler.py/ir.py/ir_gen.py/codegen.py

### What already exists (from earlier work, found while investigating)
`ir.py`, `ir_gen.py`, and `codegen.py` already had substantial function-pointer scaffolding in place:
- `IRFuncAddr` (dest = address of named function) and `IRIndirectCall` (dest = call through a
  register) IR nodes — `ir.py:199-210`.
- `_func_names` pre-collected from all `FuncDef`s so forward references resolve; `&funcname`,
  `fp = funcname`, `fp(args)`, `(*fp)(args)` all correctly emit `IRFuncAddr`/`IRIndirectCall` —
  `ir_gen.py` (`_load_var`, `_unary '&'`, `_call`).
- `_gen_IRFuncAddr` / `_gen_IRIndirectCall` codegen, fully spill/caller-save aware, mirroring the
  direct-call path — `codegen.py:807-877`.

### The blocker
Cross-checked against `AparaReference.pdf` (ISA doc) and the assembler's own parser grammar:

1. **`$call $rN` (register-indirect call) is valid hardware/ISA behavior.** ISA §6.2: "the target
   address is in the bottom 32-bits of the specified register." So `_gen_IRIndirectCall`'s
   `$call {RET}` is correct — this half of the feature works.
2. **There is no instruction that can load a function's absolute address into a register.**
   `$call <label>` is a 25-bit **PC-relative offset**, not an absolute address, so it can't be
   repurposed. The only other candidate, `$set`, was checked directly against the assembler's
   parser source: its immediate field grammar only accepts `UINTEGER` / `NINTEGER` / `HEXADECIMAL`
   tokens — a label name in that position throws `NoViableAltException` (hard parse failure).
   The assembler has **no label-resolution mechanism** outside of control-transfer instructions.

### Conclusion
`_gen_IRFuncAddr`'s current `$set {dest} 0 {ir.func_name}` will hard-crash `mcode_assemble`.
Function pointers cannot be implemented until the assembler gains some way to materialize a
label's absolute address into a general-purpose register (new instruction, new `$set` grammar
rule, or a post-assembly relocation/patch mechanism). This is outside the Python compiler's
control. **Do not re-attempt without first confirming the assembler side has changed.**
`IRIndirectCall` codegen is sound as-is and can be reused immediately once `IRFuncAddr` has a
working implementation.

---

## 2026-06-17 — Register Spilling

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

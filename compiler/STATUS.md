# APARA C Compiler — Project Status

---

## 2026-06-19 — Steps 1+2 done: confirmed no half-implementation existed, then added opt-in packed-array stride. Zero regressions (Latest)

### Step 1 — checked for existing partial work first
Grepped `ir_gen.py`/`codegen.py`/`ir.py`/`bundler.py` for "packed"/"natural stride"/anything
narrow-type-stride related: nothing exists. `dmem_stride = max(elem_bytes, 8)` is hardcoded with
no opt-out path anywhere, in both `_alloc_global` and `_alloc_local`. Confirmed before building
anything new, as instructed.

### Step 2 — opt-in natural stride for char/short/int arrays, long long/pointer/struct untouched
`__attribute__((packed))` is not parseable by this pycparser setup (confirmed by testing it
directly — hard parse error). Used the same mechanism the compiler already relies on for
`int64_t`/etc. (`_FAKE_TYPEDEFS` in `compiler.py`): six new opt-in marker typedefs --
`vu8_t`/`vi8_t`/`vu16_t`/`vi16_t`/`vu32_t`/`vi32_t` -- aliasing the obvious base types. An array
declared with one of these specific type names gets `dmem_stride = elem_bytes` (no padding);
**every other array, including plain `char`/`short`/`int`, is completely unaffected** (default
unchanged: `max(elem_bytes, 8)`). `ir_gen.py`: new `_is_packed_array_decl()` checks the element's
literal `IdentifierType` name against the marker set; naturally scoped to plain 1D arrays only —
2D arrays (own separate `col_stride` path) and struct fields (forced `esz=8`) never match this
check, so they're unaffected without needing extra exclusion logic. Wired through `visit_Decl` →
`_alloc_global`/`_alloc_local`'s new `packed=` parameter.

Verified directly: `vu8_t A[16]` → `GLOBAL A @0x400 (16B stride=1)`, indexing offset `+1` for
`A[1]`. `unsigned char B[16]` (same probe file) → `GLOBAL B @0x410 (128B stride=8)`, completely
unchanged. **Full regression, all 20 pre-existing tests**: zero regressions, every value exactly
matches prior runs. `test_ld128_then_dot` (Stage 3's reproduction evidence, uses plain
`unsigned char` deliberately) still fails exactly as documented — expected, since it never opts in.

### Status: Steps 1-2 done and committed. Step 3 (16x16 matmul with packed arrays) next.

---

## 2026-06-19 — Stage 3 (full matmul) blocked by a real architectural finding, not a bug to fix in passing: byte arrays are NOT tightly packed in this compiler, so u128/u256 loads can't see them as packed byte vectors (Latest)

### What was attempted
Composed the already-verified Stage 1 (`__ld128`) and Stage 2 (`__dot128_vu8`) pieces into an
actual 16x16 `vu8` matmul (`new_isa_tests/test_matmul_u128.c`), matching the hand-written 16x16
reference's data layout (B pre-transposed) and verified against its own checksum (67517440) and
spot-checked output values. **Failed**: `C[0]` came out wrong.

### Root cause — found, not guessed, and it's bigger than this stage
Isolated with a minimal repro (`new_isa_tests/test_ld128_then_dot.c`: load two 16-byte `unsigned
char` arrays via `__ld128`, dot them with `__dot128_vu8`, expect 136). Checked the actual IR:
```
GLOBAL A @0x400 (128B stride=8)
...
_t9 = _t8 * 8        // index k -> byte offset k*8, not k*1
```
**Every array element in this compiler — regardless of its C type's actual size — gets its own
8-byte-aligned DMEM slot** (the established convention, documented elsewhere as working around an
`$ld ($i32)` hardware quirk). A 16-element `unsigned char A[16]` therefore occupies 128 bytes in
memory, not 16: each real byte is followed by 7 bytes of padding. A `$ld ($u128)` reads 16
*physically consecutive bytes* — which under this layout is `A[0]`'s one real byte, 7 padding
zeros, and 1 byte of `A[1]`'s slot. It never sees 16 packed logical elements. Confirmed in the
runtime trace: the loaded "vector" came back as `0x0100000000000000` — exactly one real byte
(value 1) followed by zeros, matching this explanation precisely.

**Why Stage 1 didn't catch this**: it used `long long src[2]` — for an 8-byte C type, the 8-byte
stride *is* the element size, so there's no padding to expose. Stage 2 was fed literal constants
directly, no memory layout involved at all. Byte vectors are specifically what u128/u256 loads
exist for, so this stage was the first one that could possibly hit it.

### This is an architectural decision, not a quick fix — stopping per instruction
This isn't "implement the obvious fix" — it's "decide how byte arrays meant for vector use should
be laid out," which has real tradeoffs (a separate tightly-packed array kind? a `__packed`
attribute? change the global stride rule only for `vu8`-tagged arrays?) that aren't mine to choose
unilaterally. Flagging precisely and stopping here rather than guessing at a fix, per the explicit
instruction for exactly this kind of finding.

### What's verified and what's not (precise state, nothing half-applied)
- **Verified, hardware-confirmed, solid**: `$ld ($u128)`/`$ld ($u256)` register-pair/quad load
  mechanics (Stage 1), alignment-correct `borrow_pair`/`borrow_quad` (with a targeted unit test),
  the `$dot`+`$dot $accumulate` split lowering for 128-bit-wide dot products fed from registers
  directly (Stage 2, `test_dot128_split.c`, exact match).
- **NOT verified, blocked**: composing a wide load's output into a vector op when the source data
  is a C byte array — blocked by the stride-8-per-element layout above, not by anything in the
  Stage 1/2 code itself.
- **Test files left in place, not reverted**: `test_matmul_u128.c` and `test_ld128_then_dot.c` are
  committed as-is (the latter with its one failing check) — they're the reproduction evidence for
  this finding, not a regression to fix later. `test_matmul_u128.c`'s checksum check is commented
  out (the spot-checks below it already fail first; left the comment explaining why rather than
  deleting it).

---

## 2026-06-19 — Dot-split stage DONE: u128-wide $dot auto-lowering implemented and hardware-verified (Latest)

### What was implemented (no re-derivation — emitted the proven 16x16 reference pattern exactly)
New `IRVecDot128(dest, a_lo, a_hi, b_lo, b_hi, type_str)` + `_gen_IRVecDot128` in `codegen.py`,
emitting exactly:
```
$dot              dest (type) lo_a lo_b
$dot $accumulate  dest (type) hi_a hi_b
```
New intrinsic `__dot128_{type}(a_lo, a_hi, b_lo, b_hi)` in `ir_gen.py`. No bundler change needed —
`$dot`/`$dot $accumulate` hazard tracking already existed and already matches this exact emitted
form. `$dot`'s own operands have no register-pair alignment requirement (unlike `$ld ($u128)`/
`$pack`), so `a_lo`/`a_hi`/`b_lo`/`b_hi` are ordinary, independently-allocated registers here —
no borrow_pair/borrow_quad involved in this stage.

### Verification — `new_isa_tests/test_dot128_split.c`
16-element `vu8` dot: A = 1..16, B = all 1s. Hand-computed: sum(1..16)×1 = 136 = `0x88`.
Emitted mcode confirmed exact target pattern: `$dot $r13 ($vu8) $r6 $r10` then
`$dot $accumulate $r13 ($vu8) $r8 $r12`. Register trace: `$r13` goes `0x24` (36, lo-half sum,
matches 1+2+...+8 by hand) → `0x88` (136, after accumulate, matches 36+100 by hand) — exact match
at both steps, not just the final aggregate. `r1=0x1`, zero pipeline errors. Quick regression on
overlapping tests (`test_dot`, `test_pack`, `test_u128_load`, `test_u256_load`, `test_matmul`):
unchanged.

### Status: dot-split stage verified and committed. Ready for Stage 3 (full matmul) on confirmation.
All three staged pieces (u128/u256 load mechanics, alignment-correct borrow_pair/borrow_quad, and
now the dot-split lowering) are independently hardware-verified. Not yet wired into an actual
matrix multiply — that's the next stage, not started, awaiting go-ahead per the staged plan.

---

## 2026-06-19 — u128/u256 redesigned to transient borrow (mirroring $pack exactly); found and fixed a real latent alignment bug in borrow_pair() along the way; u256 now hardware-verified too (Latest)

### borrow_pair() had NO alignment check at all -- not just "needs generalizing"
Re-read the actual code before touching anything (per instruction): `borrow_pair()` only checked
`n2 == n1+1` (consecutive). It never checked that the start index is even. ISA doc 12.2 confirmed
verbatim: "When a pair of registers is used... the register specified... must have an even
index... When a quad... an index which is a multiple of 4." So this wasn't "extend an existing
even-check to also handle multiple-of-4" -- it was **add the missing even-check to `borrow_pair`
itself** (a latent bug that happened not to bite `$pack` yet, presumably because the free pool's
first consecutive run has so far always happened to start even) **and** add a correctly,
independently-parameterized multiple-of-4 check for the new `borrow_quad`. Implemented both via
one shared `_find_aligned_group(count, alignment)` scan so the two checks can't drift apart by
accident.

### Stage 1 reworked: permanent reg_pair() reverted, transient borrow like $pack
Removed `RegAlloc.reg_pair()`/`_alloc_reg_pair()` entirely. `IRLoad128` generalized to
`IRLoadWide(dests, base, offset)` (length 2 or 4). `_gen_IRLoadWide` now mirrors `_gen_IRPack`
exactly: borrow an aligned pair/quad just for the one `$ld` instruction, copy each register out to
an ordinary unconstrained register immediately, release the borrowed group right away. This means
loaded values don't tie up alignment-sensitive registers for their whole lifetime -- important
once Stage 2/3 need many loads live at once.

### Unit test specifically targeting the trap flagged before implementing
Direct `RegAlloc` test (not C-level): pool = `{$r2,$r3,$r4,$r5,$r8,$r9,$r10,$r11}` -- a tempting
*contiguous* run at 2..5 (invalid, start=2 isn't a multiple of 4) alongside a genuinely valid one
at 8..11. `borrow_quad()` returns `($r8,$r9,$r10,$r11)`, never touches `$r2`. Second case: pool =
`{$r2,$r3,$r4,$r5}` only (no valid run anywhere) -- `has_free_quad()` is `False` and `borrow_quad()`
raises rather than silently accepting the misaligned run. Third (bonus) case confirms
`borrow_pair()` itself skips an odd-start pair (`$r3,$r4`) in favor of an even one (`$r6,$r7`) when
both are present. All three pass.

### Hardware verification, both widths
`test_u128_load.c` (re-run after the rework): `$ld ($u128) $r6 [...]` (r6 is even) →
`r1=0x1`, register trace `$r6=0x1111...111`, `$r7=0x2222...222`, exact match.
`test_u256_load.c` (new): `$ld ($u256) $r8 [...]` (r8 is a multiple of 4) → `r1=0x1`, register trace
`$r8=0x1111...`, `$r9=0x2222...`, `$r10=0x3333...`, `$r11=0x4444...`, exact match across all four
quarters. Full 20-test regression (everything from the suite plus both new tests): zero
regressions, including `test_pack` (also uses `borrow_pair()`) — unaffected by the new alignment
check, consistent with it having always gotten lucky rather than ever needing an odd-start pair.

### Status
Both u128 and u256 load mechanics are now hardware-verified with correct ISA-mandated register
alignment, proven by both a hardware test and a targeted allocator unit test. Stopping here per
the staged plan — not touching the dot-split stage without confirmation.

---

## 2026-06-19 — u128 register-pair load: Stage 1 (load mechanics only) PASSES. Stopping here per the staged plan, awaiting confirmation before Stage 2 (Latest)

### What was built
- `ir.py`: new `IRLoad128(dest_lo, dest_hi, base, offset)` node.
- `codegen.py`: `RegAlloc.reg_pair()` (permanent consecutive-pair allocation, mirrors the
  existing transient `borrow_pair()` used by `$pack`) + `_alloc_reg_pair()` (spill-aware
  wrapper) + `_gen_IRLoad128` (emits `$ld ($u128) {lo} [{base}+{offset}]`).
- `bundler.py`: generalized the `$ld` hazard regex from `($iN)`-only to `($[iu]N)`, and made it
  compute the write-set as a register *range* (`{rd..rd+nbits/64-1}`) instead of always `{rd}` --
  needed because a single `$u128` load writes two registers, not one.
- `ir_gen.py`: new intrinsic `__ld128(dst, src)` — one `$ld ($u128)` into a register pair, then
  two plain 64-bit stores of the halves to `dst[0]`/`dst[8]`.

### A real, separate, pre-existing bug found and fixed along the way
Bare global array names passed to a call were never decaying to their address — only *local*
arrays did. Root cause: `_array_elem` (the dict the call-arg decay check consults) gets reset to
`{}` at the top of `visit_FuncDef` for per-function local scoping, which silently wiped out any
global array registered before `main` was visited. (`_array_row_stride`, used for 2D arrays,
already avoids this — it's never reset, which is exactly why global 2D arrays never had this bug.)
**Fix**: new `self._global_array_elem` dict, populated by `_alloc_global`, never reset, consulted
alongside `_array_elem`/`_array_row_stride` in the call-arg decay check. This was diagnosed in a
few steps (read the IR dump, found values instead of addresses, found the exact reset line) — a
contained, well-understood Python fix, not the kind of source-archaeology that warranted stopping
to ask first. Verified zero regressions on every array-using test (`test_array`, `test_2d`,
`test_struct`, `test_scalar_full`, `test_matmul`, `test_spill`).

### Stage 1 verification — `new_isa_tests/test_u128_load.c`
```c
src[0] = 0x1111111111111111LL; src[1] = 0x2222222222222222LL;
__ld128(dst, src);
// dst[0] must equal src[0], dst[1] must equal src[1]
```
Register trace confirms exactly: `$ld ($u128) $r1 [...]` set `$r1=0x1111111111111111`,
`$r2=0x2222222222222222` — lower register gets the lower address, matching every other
consecutive-register convention already in this compiler ($pack, $cast). r1=`0x1` (both checks
pass). Zero pipeline errors.

### Stopping here per the staged plan
Per explicit instruction: report back after each stage, don't continue without confirmation.
**Stage 1 (load mechanics) is hardware-verified.** Stage 2 (auto-split `$dot`/`$v` across the pair
into plain + `$accumulate`, matching the 16x16 reference) not started — awaiting go-ahead.

---

## 2026-06-19 — $vreduce FIXED: missing sub-opcode token, same bug family as $cmov's missing '?'. All vector instructions now confirmed working (non-4-bit integer types) (Latest)

### Root cause (isolated the same way as $cmov: minimal repro, read the actual grammar)
`$vreduce $rd ($type) $rs` (what we emitted, and what the ISA doc's own example shows) fails to
parse: `unexpected token: $r6` right after `$vreduce`. The real grammar
(`mcode_vreduce_instruction` in `isa.g`) is:
```
opcode = mcode_vreduce_op_code        // consumes the $vreduce mnemonic itself
sub_opcode = mcode_vreduce_sub_op_code  // REQUIRED: one of + * | & ^ ~^ $max $min
rd = mcode_reg_specifier
mcode_type_specifier
rs1 = mcode_reg_specifier
```
The sub-opcode selects *what kind* of reduction (`+`=sum, `*`=product, `\|`/`&`/`^`/`~^`=bitwise,
`$max`/`$min`). The ISA doc's example (§5.5) omits it entirely — third time this exact failure
mode has shown up (`$cmov`'s missing `?`, now this), all because the doc's own examples are
incomplete relative to the actual grammar. Our `__vreduce_*` intrinsics only ever do sum-reduce,
so the fix is to always emit the `+` sub-opcode.

### Fix
`codegen.py`'s `_gen_IRVecReduce`: `$vreduce {dest} ({type}) {src}` → `$vreduce + {dest} ({type})
{src}`. `bundler.py`'s `$vreduce` hazard regex updated to skip the new sub-opcode token.

### Verification
`test_vec_reduce2.c` (6 checks: vi8/vi16/vi32/vu8/vu16/vu32 sum-reduce): all pass, r1=`0x1`.
Original `test_vreduce.c`: r1=`0x4c` (76) — exact match with the historical expected value, now
running cleanly instead of crashing the aligner. Full 19-test regression: zero regressions.

### Bottom line: every non-4-bit-integer vector instruction is now confirmed working
`$v` (add/sub/mul): all of `vi8`/`vi16`/`vi32`/`vu8`/`vu16`/`vu32`. `$dot`/`$dot $accumulate`:
`vi8`/`vi16`/`vu8`/`vu16` (the only widths the ISA defines dot for). `$vreduce`: all six widths.
`vi4`/`vu4` remain known-broken/skipped per explicit direction (not used frequently). Float
vectors (`vf*`) deferred entirely per explicit direction. **Vectors are now solid enough to build
matrix multiplication on top of** — next step per the user's plan.

---

## 2026-06-19 — $dot/$dot $accumulate fully hand-verified for matmul readiness (8/8 exact); $vreduce hits a second, separate, not-yet-root-caused bundler/aligner crash (Latest)

Per explicit direction: skip `vi4`/`vu4` (not used frequently), prioritize `$dot`/`$dot $accumulate`
(most important for matrix multiplication), float vectors (`vf*`) deferred entirely, `$nop` not
urgent. Goal: get enough of `$v`/`$dot`/`$vreduce` verified to trust vector-based matmul.

### $dot / $dot $accumulate — ALL 8 checks pass exactly (`new_isa_tests/test_vec_dot.c`)
Hand-computed sum-of-products for `vi8`/`vi16`/`vu8`/`vu16`, both plain and `$accumulate` forms
(`vi32`/`vu32` correctly excluded — ISA doc §5.4: "dot is defined only for elements <=16 bits").
Final r1 = `0x1` (all pass). **This is the piece the user said matters most for matmul, and it's
solid.**

### $v add for the remaining untested widths — both pass exactly (`test_vec_add16_32.c`)
`vu16`: `0x00060008000a000c` (element-wise (1+5),(2+6),(3+7),(4+8)) — exact match.
`vu32`: `0x000000080000000a` (element-wise (3+5),(4+6)) — exact match.
Combined with earlier confirmation of `vi8`/`vi16`/`vi32`/`vu8`, **`$v` add is now verified across
every non-4-bit integer width.**

### $vreduce — still broken, but now isolated to a SECOND, different bug than the label-merge fix
`test_vec_reduce2.c` (6 plain `__vreduce_*` checks, no `$dot`/`$v` mixed in) crashes
`mcode_align` with the same `Calculate_Pad_For_Alignment` assertion — but **confirmed via the same
bisection technique used for the label bug that there is no consecutive-label pattern here**, so
this is NOT the bug fixed yesterday. Bisected down to the crash appearing right around the
*first* `$vreduce` call's bundle (`$vreduce $r6 ($vi8) $r5` + an address-compute ALU op, followed
by the store of its result) — consistent with `test_vreduce` (the pre-existing official test)
failing the exact same way. **Not root-caused this session** — ran out of quota for the
bisect-deeper-into-bundler.py work this would need (same rigor as yesterday's label-merge fix,
just not finished). This is the one piece standing between "vectors are matmul-ready" and "fully
verified" — `$vreduce` itself (sum-reduction) isn't needed for a dot-product-based matmul, only
`$dot`/`$dot $accumulate` are, so this doesn't block vector matmul, just full ISA coverage.

### Bottom line for vector-based matrix multiplication
The two operations that actually matter for matmul — `$dot` and `$dot $accumulate` — are now
fully hand-verified across every non-4-bit integer width. `$v` add is fully verified too (useful
for elementwise vector ops alongside matmul). `$vreduce` remains broken but isn't on the matmul
critical path. **Next planned step** (not started this session, flagged for next time): `u128`/
`u256` wide vector load/store — needed for real 32x32 vector matmul, currently zero compiler
support (confirmed via grep, see earlier entries).

---

## 2026-06-19 — vi4 garbage value confirmed reproducible (4/4 runs); audited all other switch(nbits) blocks — CastToU64 appears to be an isolated bug, not a pattern (Latest)

### Reproducibility check
Re-ran the `test_vi4_check` repro 4 more times (fresh `run.sh` invocation each time, full
align→assemble→run). **All 4 runs: `Set_Register(7, 0x0)` — identical every time.** Not
coincidental; consistent with reading a deterministic (if uninitialized) stack slot reached via
the exact same call path every run, not random garbage that happens to vary.

### Audit of the other switch(nbits) blocks flagged yesterday
Checked `McodeNumeric.cpp:493` and **all seven** `switch(nbits)` blocks in `McodeFpuUtils.cpp`
(corrected count — said "five" yesterday without actually counting; there are 7) for the same
missing-`case 4`-with-uninitialized-fallthrough pattern as `CastToU64`. None of them have it:

| Location | Function | Has `case 4`? | Fallback if no match |
|---|---|---|---|
| `McodeNumeric.cpp:493` | `to_ufp64` | yes | `default:` reinterprets bits directly (defined behavior) |
| `McodeFpuUtils.cpp:318` | `fp_mul` | yes | `result` pre-initialized to 0; `default:` no-op |
| `McodeFpuUtils.cpp:344` | `fp_add` | yes | same |
| `McodeFpuUtils.cpp:371` | `fp_sub` | yes | same |
| `McodeFpuUtils.cpp:398` | `fp_div` | **no** (only 32/64) | `default: assert(0)` — **crashes loudly**, doesn't silently return garbage |
| `McodeFpuUtils.cpp:417` | `fp_sqrt` | **no** (only 32/64) | `default: assert(0)` — same, loud crash |
| `McodeFpuUtils.cpp:539` | `double_to_fp` | yes | `default: assert(0)` |
| `McodeFpuUtils.cpp:553` | `fp_to_double` | yes | `default: assert(0)` |

Also checked the three related cast helpers right next to these (`cast_int_to_float`,
`cast_float_to_int`, `cast_float_to_float`, all in `McodeFpuUtils.cpp`) since they weren't in the
original flagged list but are directly adjacent and relevant — all three have `case 4:` and
`default: assert(0)`.

**Conclusion: `CastToU64`'s bug looks isolated, not systemic.** Every other switch(nbits) either
explicitly handles 4 bits, or fails loudly (`assert(0)`) instead of silently returning an
uninitialized value. `fp_div`/`fp_sqrt` simply don't support 4-bit floats by design (consistent
with there being no ISA-documented 4-bit float div/sqrt) and crash rather than corrupt — that's a
deliberate restriction, not the same bug class as `CastToU64`. Only `CastToU64` declares `result`
without an initializer and has no `default:` label at all, which is exactly why it alone returns
silent garbage instead of crashing or working.

---

## 2026-06-19 — Hand-verified vi4/vu8: vu8 correct, vi4 confirmed BROKEN (engine bug, exact line found) (Latest)

Yesterday's "vi4/vu8 don't crash" claim was correctly challenged as insufficient. Wrote two
minimal, hand-computable tests (`new_isa_tests/test_vi4_check.c`, `test_vu8_check.c`) and compared
the exact hardware register value against a hand-computed expected value — not just "did it run."

| Type | a | b | Expected (hand-computed) | Hardware actual | Result |
|---|---|---|---|---|---|
| `vu8` | `0x0102030405060708` | `0x1010101010101010` | `0x1112131415161718` (each byte +0x10, no overflow) | `0x1112131415161718` | **exact match** |
| `vi4` | `0x1111111111111111` | `0x2222222222222222` | `0x3333333333333333` (each nibble 1+2=3, no overflow) | `0x0` | **WRONG** |

### Root cause for vi4 — found, not guessed (engine bug)
Traced `$v +` for `(vi4)` through `___execute_valu_operation___` → `__valu_operation__` →
`__alu_operation__` → `CastToU64()` (all in `McodeOperations.cpp`). `CastToU64(int signed_flag,
uint32_t nbits, uint64_t ival)` (line 50) has a `switch(nbits)` with cases for **8, 16, 32, 64
only** — no `case 4`. For any 4-bit-wide result (every `vi4`/`vu4`/`vf4` element op), the switch
falls through with no case matching, `result` is declared but **never assigned**, and the function
returns whatever garbage was already on the stack — which happened to be `0` in this run, hence
every element of the vi4 add silently became `0`, concatenating to a final `0x0`. `vu8`/`vi8`/etc.
all hit the `case 8` (or 16/32/64) branch correctly, which is why every other tested vector width
is fine and only the 4-bit path is broken.

Type parsing itself is correct (confirmed in `isa.g`'s `mcode_type_specifier` rule: `vi4_t` →
`nbits=4, vector_flag=1`) — the bug is purely in this one switch statement's missing case, not in
how `vi4` is recognized or decoded.

**Not fixed** (engine-side, same protocol as today's other engine findings — needs the professor).
The fix is mechanical: add a `case 4:` doing a manual 4-bit sign-extend/mask (no native C++ `int4_t`
to reuse the existing `___signed_cast___`/`___unsigned_cast___` macros with). **Flagging, not
guessing further**: there are similar `switch(nbits)` statements in `McodeNumeric.cpp:493` (used by
`$cast`) and five in `McodeFpuUtils.cpp` (float ops) — not checked for the same missing-case-4 gap;
don't assume `i4`/`u4`/`f4` scalar casts or float-4 ops are safe until checked the same way.

### Bottom line on vectors
`vi8`/`vi16`/`vi32` (signed) and `vu8` (unsigned, newly hand-verified) are confirmed correct.
**`vi4` is confirmed broken with a precise, citable root cause** — do not use it, and don't claim
4-bit vector types work in general until `CastToU64` is fixed and re-verified. `vu4`/`vf4` are
untested but share the exact same code path, so should be assumed broken too until checked.

---

## 2026-06-19 — Three real compiler-side bugs found and fixed: $cmov operand grammar, and a bundler bug that was silently causing test_2d/test_fsqrt/test_matmul's aligner crashes (Latest)

### 1. `$cmov` fixed — `codegen.py`'s `_gen_IRCmov`
Professor's freshly-pulled engine (`10.107.90.220:/students/mohith/AjitHpc_new/...`) confirmed
`$cmov` requires a `?` token right after the mnemonic, matching `engine_new`'s grammar (not the
historical `engine_isp` grammar our codegen targeted). Traced the actual register-role wiring
empirically (`McodeInstructions.cpp`'s `Get_Operands`/`Execute`) rather than trusting grammar
comments, since theoretical grammar-position reasoning gave contradictory results twice. **Fix**:
add `?`; the check/src_true register ORDER in the text is unchanged (`check` first, `src_true`
last) — only the `?` was missing. `bundler.py`'s `$cmov` hazard regex updated to match. Verified:
`test_cmov` now returns `0x258` (600), exact match, zero regressions on anything else.

### 2. Real bundler bug found: consecutive labels on one bundle aren't valid syntax
While building a matrix-multiply test (`new_isa_tests/test_matmul.c`, 3x3, flattened 1D arrays to
avoid the already-known 2D-array aligner issue), hit the *same* `Calculate_Pad_For_Alignment`
assertion crash that blocks `test_2d`/`test_fsqrt`/`test_vreduce`. Root-caused properly this time
(bisected the mcode by bundle boundaries): `bundler.py`'s `_emit_bundles` prints **every** label
attached to a bundle on its own line before `||` — but the assembler grammar only allows ONE label
directly before a bundle (confirmed: `expecting PARALLEL, found <label>` when two appear in a
row). This happens whenever, e.g., an inner loop's exit label lands on the exact same bundle as an
outer loop's increment label with no real instruction between them — common in nested loops. The
resulting parse error leaves a zero-instruction bundle, and `Calculate_Capacity()` in
`McodeBundle.cpp` silently returns 0 for it (logs an error but doesn't abort) instead of crashing
there — the crash only surfaces later in `Calculate_Pad_For_Alignment`'s division-by-zero guard,
which is why this looked unrelated for so long.

**Fix** (`bundler.py`, new `_merge_duplicate_labels`, wired into `bundle_mcode` between
`_pack_bundles` and `_emit_bundles`): when a bundle ends up with multiple labels, collapse to one
canonical label (the first) and rewrite every `$goto`/`$call` reference to the dropped labels so
they point at the canonical one instead. (`$call $rN`, register-indirect, is never matched — the
regex requires a bare identifier with no leading `$`.)

**This one fix resolved four programs at once**: `test_matmul` now returns `0x26d` (621, exact —
hand-verified 3x3 matrix multiply), and as a bonus, `test_2d` and `test_fsqrt` — both blocked by
this exact crash for weeks — now align and run cleanly (`test_2d`=`0x0`, matching expected).
`test_vreduce` still crashes, but confirmed via the same bisection technique that it has **no**
consecutive-label pattern — a different, separate, not-yet-diagnosed cause.

### 3. Vector type coverage: `vi4`/`vu8` confirmed not to crash (not bit-level verified)
Wrote `new_isa_tests/test_vec_extra.c` exercising `__vadd_vi4`/`__vadd_vu8` (previously completely
untested — the intrinsic parser does zero suffix validation). Compiles and runs cleanly, zero
pipeline errors. **Not yet hand-verified bit-exact** — that needs dedicated test design, not a
quick check; flagging honestly rather than claiming full confidence here.

### Also fixed: `compiler.py`'s `write_run_script` template
Was still emitting the stale March-6 `engine_isp` `BIN_DIR` for any newly-compiled test (flagged
yesterday, actually bit us today recompiling `test_cmov`). Now points at `engine_new`.

### Full regression after all of the above
`test_alu`=0xd, `test_array`=0x96, `test_struct`=0x0, `test_branch`=0x1, `test_ldst`=0x3e8,
`test_pointer`=0xf, `test_subword`=0x1, `test_dot`=0x5a, `test_spill`=0x1d1,
`test_scalar_full`=0xc, `test_vadd`=0x4, `test_slice`=0xb7, `test_cast`=0x78ab9bcd,
`test_pack`=0xdead, `test_cmov`=0x258 — all exactly correct, zero regressions from today's changes.

---

## 2026-06-18 — IMEM size bug CONFIRMED against the official ISA doc and fixed for real this time: simulator was [2048] (8KB), spec says 16KB; corrected to [4096]. test_struct/test_spill/test_scalar_full ALL pass (Latest)

### What changed since the last entry
User checked `AparaReference.pdf`, p.6, §1, Figure 1.1 directly: **"The instruction memory provides
16KB of instruction space to each accelerator. Each instruction is 4-bytes."** 16KB ÷ 4 bytes/instr
= **4096 words**. `McodeClasses.hpp` had `__instruction_memory[2048]` / `Instr_Mem_Size_In_Words()
= 2*1024` — **half the documented size**, mislabeled with a stale `// 16KB` comment that never
matched the actual 8KB the array provided. This is not a "maybe the simulator default is smaller
than hardware" situation (the open question from the previous entry) — it's a confirmed, citable
discrepancy between the simulator and the spec. (Data memory was already correct: `__data_memory[8
* 1024]` qwords = 64KB, matching the doc's "data memory provides 64KB" exactly — only instruction
memory was wrong.)

### Fix applied (no longer "verification only")
`McodeClasses.hpp:139,143`: `__instruction_memory[2048]` → `[4096]`,
`Instr_Mem_Size_In_Words()` → `4*1024`. Rebuilt with `scons`.

| Test | Before (8KB IMEM, the bug) | After (16KB IMEM, matches spec) | Expected |
|---|---|---|---|
| test_scalar_full | 0x7ff0 | **0xc** | 0xc (12) |
| test_spill | 0x19 | **0x1d1** | 0x1d1 (465) |

Both real program sizes (2688 / 2496 words) now fit comfortably under the corrected 4096-word
budget — zero "beyond the i-mem size" errors. Full 19-test regression re-run: all 14
runnable/passing tests produce exactly their expected values
(`test_alu`=0xd, `test_array`=0x96, `test_struct`=0x0, `test_branch`=0x1, `test_ldst`=0x3e8,
`test_pointer`=0xf, `test_subword`=0x1, `test_dot`=0x5a, `test_spill`=0x1d1,
`test_scalar_full`=0xc, `test_vadd`=0x4, `test_slice`=0xb7, `test_cast`=0x78ab9bcd,
`test_pack`=0xdead). Zero regressions from this change. `test_vreduce`/`test_cmov` still fail at
the pipeline level for their already-documented, unrelated `engine_new`-divergence reason;
`test_2d`/`test_logic`/`test_fsqrt` remain blocked for their own pre-existing, unrelated reasons.

### Bottom line
**All three originally-broken tests (`test_struct`, `test_spill`, `test_scalar_full`) are now fully
fixed**, via two independent, confirmed root causes: (1) CALL's disassembler sign-extend using bit
index 25 instead of 24 (`McodeDisassemble.cpp:266`), and (2) the simulator's instruction memory
being built to half the size the ISA reference document specifies (`McodeClasses.hpp:139,143`).
Both are precise, citable, reproducible bugs — not hypotheses. Still pending: confirming with the
professor whether this IMEM correction should be applied to the distributed/official engine build
(it should be, per the doc, but it's still his binary to update), and the separate, still-open
`engine_new`-divergence issue behind `test_vreduce`/`test_cmov`'s pipeline failures.

### Data-type coverage note (asked separately, recorded for the record)
Confirmed: `i4`/`u4` are arithmetic-only, no load/store form (matches hardware — minimum transfer
granularity is a byte). `$ld`/`$st` support `i8`/`u8` through `i64`/`u64`, plus `u128`/`u256` wide
loads at the ISA level — but the **compiler** does not yet generate `u128`/`u256` loads/stores
(confirmed via grep: zero references in `codegen.py`/`ir_gen.py`/`ir.py`); vector arithmetic
(`$v`/`$dot`/`$vreduce`) only operates on values already manually packed into a 64-bit register.
Tested/hardware-confirmed vector element widths: `vi32`/`vi16`/`vi8` for `$v` ops and `$vreduce`,
`vi16` for `$dot`. Untested: `vi4`, any unsigned vector (`vu*`) type — the intrinsic parser
(`ir_gen.py` `__vadd_`/`__dot_`/`__vreduce_` handlers) does no validation on the type suffix, so
these would compile silently but have never actually been run.

---

## 2026-06-18 — IMEM bump reverted; confirmed by user that 2048 words IS the real hardware limit, not a simulator default (Latest)

The 2048→16384-word IMEM bump from the previous entry was explicitly a verification-only change.
**User confirmed 2048 words is the actual hardware IMEM capacity** — not something the simulator
can legitimately just expand. Reverted `McodeClasses.hpp:139,143` back to `[2048]` / `2*1024`,
rebuilt with `scons`, and re-confirmed `test_spill`/`test_scalar_full` are back to their original
truncated-program values (`0x19` / `0x7ff0`) — i.e. the build is hardware-faithful again.

**The diagnosis from the previous entry stands and is still useful**: both tests' wrongness is
fully and exactly explained by program-too-big-for-IMEM (640 / 448 words silently dropped), not by
any remaining logic bug. The fix now has exactly one viable path: reduce `bundler.py`'s padding
overhead (currently >80% of `test_scalar_full`'s compiled size is mandatory 8-slot control-transfer
padding) so real programs fit in the real 2048-word budget. Not started — this is compiler-side
work, distinct from anything engine-side, and doesn't need the professor's involvement the way the
IMEM question did.

---

## 2026-06-18 — SECOND ROOT CAUSE FOUND: fixed-size 2048-word IMEM silently truncates larger programs. With both fixes together, `test_struct` / `test_spill` / `test_scalar_full` ALL now produce exactly the expected values (Latest)

### The bug
`test_spill`'s and `test_scalar_full`'s remaining wrongness (`0x19`/`0x7ff0` after yesterday's CALL
fix) was never a logic bug at all. `McodeClasses.hpp:139` declares a fixed
`uint32_t __instruction_memory[2048]` (`Instr_Mem_Size_In_Words()` returns `2*1024`, line 143).
`McodeAccelerator.cpp:88-101` (`Init_Instruction_Memory`) silently drops — logs an `Error:`, does
not write, does not abort — any instruction whose `pc >= 2048`. Both programs are bigger than that:

| Test | Real program size | Overflow ("beyond the i-mem size") errors |
|---|---|---|
| test_scalar_full | 2688 words | 640 |
| test_spill | 2496 words | 448 |

The actual `+ $r1 = $r0 + $r9` (the real return-value write, computed correctly from
`g_arith+g_compare+g_logical`) for `test_scalar_full` lives at `pc=0xa68` (2664) — **never loaded**.
Execution runs straight off the end of the truncated program at `pc=0x800`, into zero-filled
("$null") memory, until the tick budget runs out, with r1 frozen at whatever it last held —
in this case the address (`FP-8`, `0x7ff0`) of local `a`, computed many instructions earlier inside
the `while(a>0)` loop's `a--;`, which only *looked* like a meaningful wrong value by coincidence.
Same mechanism for `test_spill` (`0x19` was likewise a stale leftover, not a computed wrong sum).

Both programs are this large mostly because of `bundler.py`'s mandatory full-8-slot padding on any
bundle containing a control-transfer instruction: `test_scalar_full`'s own run reported
"439 non-null / 1921 null" instructions executed — **over 80% of the program is padding**.

### Verification fix (local build only — see caveat below)
`McodeClasses.hpp:139,143`: `__instruction_memory[2048]` → `[16384]`,
`Instr_Mem_Size_In_Words()` → `16*1024`. Rebuilt with `scons`. Re-ran both tests:

| Test | Before (2048-word IMEM) | After (16384-word IMEM) | Expected |
|---|---|---|---|
| test_scalar_full | 0x7ff0 | **0xc** | 0xc (12) |
| test_spill | 0x19 | **0x1d1** | 0x1d1 (465) |

**Exact match, both of them.** Combined with yesterday's CALL sign-extend fix, all three originally
broken tests (`test_struct`, `test_spill`, `test_scalar_full`) now produce exactly the expected
value. Full 19-test re-run with the IMEM-bumped build: all 14 previously-runnable/passing tests
still correct (`test_alu`=0xd, `test_array`=0x96, `test_branch`=0x1, `test_ldst`=0x3e8,
`test_pointer`=0xf, `test_subword`=0x1, `test_dot`=0x5a, `test_cast`=0x78ab9bcd, `test_vadd`=0x4,
`test_slice`=0xb7, `test_pack`=0xdead, plus the three above) — zero regressions from the IMEM bump
itself. `test_vreduce`/`test_cmov` still fail at the pipeline level exactly as before (unrelated —
already traced to `engine_new`'s broader divergence from the `engine_isp` baseline, not to IMEM
size or either CALL/RAS fix). `test_2d`/`test_logic`/`test_fsqrt` remain blocked for their own
pre-existing, unrelated reasons.

### Important caveat — do not treat the IMEM bump as a verified real fix
Unlike the CALL sign-extend bug (a clear-cut decode error against the ISA's own 25-bit field
width), **it is not known whether 2048 words is a real hardware IMEM capacity limit or just an
arbitrary simulator default smaller than the real chip.** Two very different correct fixes follow
depending on which it is:
- If 2048 words is *not* a real hardware limit: bumping the simulator's constant (as done here) is
  the right, permanent fix.
- If 2048 words *is* a real hardware limit: the simulator is correctly modeling the constraint, and
  the actual fix belongs in `bundler.py` — cut the >80% control-transfer-bundle padding overhead so
  compiled programs fit in the real budget, not in the simulator.
**This must go back to the professor before either path is taken as official** — exactly the kind
of "who fixes this" question flagged for later, now with a precise, numeric, reproducible bug
report instead of a mystery. The constant bump here is a local verification build only, same status
as yesterday's two engine-source edits.

Stopping here — both originally-reported test_spill/test_scalar_full mysteries are now fully
explained and numerically confirmed fixed on this verification build.

---

## 2026-06-18 — All `run.sh` scripts under `cmp_wd` repointed from the stale March-6 `engine_isp` snapshot to `engine_new`; 19-test regression re-run through the corrected scripts (Latest)

### What was wrong
Every `run.sh` under `cmp_wd` (and the `write_run_script` template in `compiler.py` that generates
new ones) hardcoded `BIN_DIR=/home/mohithkota/engine_isp/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin`
— a March 6th snapshot, untouched by any of today's work. The actual engine being built and patched
all day lives at `complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin`.

**Correction to the literal instruction this was actioned from**: this did *not* invalidate today's
*reported numbers* — every regression result reported earlier today (the `Pop_From_Ras` checks, the
CALL sign-extend fix verification, the 16-test table) was produced by invoking the `engine_new`
binaries directly with explicit paths, never through a test's own stale `run.sh`. So today's prior
numbers are not "wrong engine" results and don't need to be disregarded. What *was* true: any
**future** run using a bare `./run.sh` (the normal, expected way to run these tests) would have
silently gone back to the stale, unpatched March-6 engine and silently lost every fix from today.
That's the real bug this fixes — a footgun for next time, not a correctness problem with anything
already reported.

### Fix applied
Repointed `BIN_DIR` in every `run.sh` under `cmp_wd` that had the stale absolute path (26 files —
all of `alu/`, `array/`, `branch/`, `ldst/`, `pointer/`, `new_isa_tests/`, including their top-level
per-category scripts) to `complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin`.
Left untouched: `mem_march/run.sh`, `not_used_files/**/run.sh` — these use an unrelated relative-path
scheme (`../../../assembler/bin`) and aren't part of the test suite. (`compiler.py`'s `write_run_script`
template itself was not changed in this pass — still emits the stale path for any newly-compiled test;
flagging for a future pass, not fixed now.)

### 19-test regression, run via the corrected `./run.sh` scripts (not direct binary calls this time)
| Test | Result | Expected | Status |
|---|---|---|---|
| test_alu | 0xd | 13 | pass |
| test_array | 0x96 | 150 | pass |
| test_ldst | 0x3e8 | 1000 | pass |
| test_branch | 0x1 | 1 | pass |
| test_pointer | 0xf | 15 | pass |
| test_subword | 0x1 | 1 | pass |
| test_dot | 0x5a | 90 | pass |
| test_cast | 0x78ab9bcd | 0x78ab9bcd | pass |
| test_vadd | 0x4 | 4 | pass |
| test_slice | 0xb7 | 183 | pass |
| test_pack | 0xdead | 0xdead | pass |
| test_struct | 0x0 | 0 | **pass** |
| test_spill | 0x19 | 0x1d1 (465) | fail (wrong value, separate bug) |
| test_scalar_full | 0x7ff0 | 0xc (12) | fail (wrong value, separate bug) |
| test_vreduce | pipeline crash (`mcode_align` assertion) | 76 | fail — regression vs. historical baseline |
| test_cmov | pipeline crash (parse exception + segfault) | 600 | fail — regression vs. historical baseline |
| test_logic | pipeline crash (parse exception) | — | fail (pre-existing, held) |
| test_2d | pipeline crash (`mcode_align` assertion) | — | fail (pre-existing, held) |
| test_fsqrt | pipeline crash (`mcode_align` assertion) | — | fail (pre-existing, held) |

**Every number is identical to today's already-reported results.** Running through the corrected
`run.sh` scripts instead of direct binary invocation changed nothing — confirms the prior report was
accurate. test_logic/test_2d/test_fsqrt remain blocked for their pre-existing, unrelated reasons
(documented earlier in this file). test_vreduce/test_cmov remain newly broken — still attributed to
`engine_new` being a diverged codebase from the historical `engine_isp` baseline (~15 files differ
beyond today's two intentional edits), not to either of today's fixes. test_spill/test_scalar_full
remain wrong for their own separate, unidentified reasons. test_struct remains the one confirmed fix.

Stopping here as instructed — no new bugs, no new hypotheses, no further investigation tonight.

---

## 2026-06-18 — ROOT CAUSE CONFIRMED (found manually, not by Claude Code): CALL's disassembler sign-extend used the wrong bit index. Fix verified on a local build — test_struct now passes; test_spill/test_scalar_full still wrong for separate reasons; two new pipeline regressions traced to engine_new being a diverged codebase, not to either fix (Latest)

**Root cause, found by tracing engine source directly (`McodeDisassemble.cpp`, `DisassembleToCallInstr`,
line 266):**
```cpp
int32_t relative_jump = (int32_t) Sign_Extend(25, Get_Slice (24, 0, hex_instr));
```
`Sign_Extend`'s first argument is a bit **index** (`McodeUtils.cpp`: `pad_ones = (1 << sign_index) & x`).
CALL's jump field is 25 bits wide, bits `24:0` — its real sign bit is at index 24, not 25. Calling it
with 25 checks a bit that's always 0 on a value already masked to `24:0`, so negative/backward call
offsets are never sign-extended. Every backward call (callee defined before caller — the normal C
pattern, e.g. `f` before `main`) computes `target + 2^25` instead of `target`, landing in
zero-filled garbage memory. This is exactly why all six engine-layer checks in the
[[project_call_phase_hazard|standalone incident report]] came back clean — none of that code ever
ran in the failing case; the call never reached the callee at all.

**Confirmed precisely isolated to CALL, not systemic.** `DisassembleToBranchInstr` (same file,
line 302) does `Sign_Extend(11, Get_Slice(16,5,...))` — a 12-bit field (bits `16:5`), real sign bit
at index 11, called with 11. **Correct, no off-by-one.** Consistent with for/while loops (which use
BRANCH, not CALL, for backward jumps) having worked correctly all along.

### Fix applied and rebuilt (verification build only — not a replacement for the professor's distributed binaries)
`complier_Apara/engine_new/.../McodeDisassemble.cpp:266`: `Sign_Extend(25, ...)` → `Sign_Extend(24, ...)`.
Rebuilt with `scons` (11:58 timestamp) on top of the same local copy that already had today's
earlier, separately-confirmed-inert `mc->Pop_From_Ras()` fix in `McodeExecute.cpp`.

### Check 3 — noop_call.c halt-before-`f`'s-return probe
**`f`'s body now executes, and r1 = 6 at the halt point.** Before the fix: `$call f` resolved to
`npc=0x2000018`, ran 10 ticks into garbage, never entered `f`. After the fix: disassembly shows
`$call l_24` (correct), the bundle at `pc=0x28` sets r1=6 and branches into `f_epilogue`, SP/FP
restore runs, and the inserted `$halt` fires cleanly at `pc=0x31` after 17 ticks — **r1=6, confirmed**.

### Check 4 — full regression (16 runnable tests; `test_logic`/`test_2d`/`test_fsqrt` excluded, pre-existing unrelated blockers)
| Test | Before today | After fix | Expected | Status |
|---|---|---|---|---|
| test_alu | 0xd | 0xd | 13 | unchanged ✓ |
| test_array | 0x96 | 0x96 | 150 | unchanged ✓ |
| test_ldst | 0x3e8 | 0x3e8 | 1000 | unchanged ✓ |
| test_branch | 0x1 | 0x1 | 1 | unchanged ✓ |
| test_pointer | 0xf | 0xf | 15 | unchanged ✓ |
| test_subword | 0x1 | 0x1 | 1 | unchanged ✓ |
| test_dot | 0x5a | 0x5a | 90 | unchanged ✓ |
| test_cast | 0x78ab9bcd | 0x78ab9bcd | 0x78ab9bcd | unchanged ✓ |
| test_vadd | 0x4 | 0x4 | 4 | unchanged ✓ |
| test_slice | 0xb7 | 0xb7 | 183 | unchanged ✓ |
| test_pack | 0xdead | 0xdead | 0xdead | unchanged ✓ |
| **test_struct** | 0xa | **0x0** | 0 | **NOW PASSES** |
| test_spill | 0x328 | 0x19 | 0x1d1 (465) | changed, still wrong |
| test_scalar_full | 0x7ff0 | 0x7ff0 | 0xc (12) | unchanged, still wrong |
| test_vreduce | 0x4c (pass) | **crashes** | 76 | **NEW regression** |
| test_cmov | 0x258 (pass) | **crashes** | 600 | **NEW regression** |

**Only `test_struct` is actually fixed by today's change.** `test_spill` now runs to natural
completion instead of jumping into garbage (confirmed: its final `$return` reaches `npc=0x800`
cleanly, no `0x2000xxx` jump anywhere in the trace) — real structural progress — but its computed
value (`0x19`) still doesn't match expected (`0x1d1`), so a second, separate bug remains in it.
`test_scalar_full` was already running to natural completion before today (no garbage jump either
before or after the fix) — its wrongness was never caused by the CALL bug, so the fix had no effect
on it; a separate, still-unidentified bug remains.

**`test_vreduce` and `test_cmov` are new pipeline-level failures — NOT caused by either of today's
two edits.** Confirmed by running the identical, untouched `.mcode` source through the original
pre-rebuild `engine_isp` binary: both align and run cleanly there. Only `engine_new`'s rebuilt
toolchain crashes on them (`test_vreduce`: `mcode_align` aborts with the same pre-existing
`Calculate_Pad_For_Alignment: Assertion '0' failed` that `test_2d`/`test_fsqrt` have always hit;
`test_cmov`: a parser exception — `expecting QUESTION, found '('` — followed by a segfault, a
different failure mode entirely). **Cause found, not chased further per instruction**: a direct
`diff -rq` between `engine_isp/AjitHpcAccelRepo/.../assembler/src` and
`complier_Apara/engine_new/AjitHpcAccelRepo/.../assembler/src` shows roughly 15 files differ beyond
today's two intentional one-line edits — `McodeBundle.cpp`, `McodeParser.cpp`, `McodeOperations.cpp`,
`McodeProgram.cpp`, `McodeRoot.cpp`, `McodeUtils.cpp`, `MachineRun.cpp`, `McodeBinaryCode.cpp`,
`McodeInstructions.cpp`, plus two files (`McodeAccelerator.cpp`, `McodeFpuUtils.cpp`) that don't
exist in the `engine_isp` tree at all. **`engine_new` is not "`engine_isp` plus two patches" — it's
a separately-diverged codebase snapshot**, and today is the first time its toolchain has actually
been built and exercised against `test_vreduce`/`test_cmov`. The regression is real but belongs to
that pre-existing divergence, not to the CALL sign-extend fix or the `Pop_From_Ras` fix.

### Bottom line for the professor report
The CALL disassembler sign-extend bug (`Sign_Extend(25,...)` → should be `Sign_Extend(24,...)`,
`McodeDisassemble.cpp:266`) is real, precisely isolated (BRANCH confirmed unaffected), and the fix
measurably works — `noop_call.c` now executes `f` and gets r1=6, and `test_struct` now passes
end-to-end on hardware-equivalent simulation. It is **not** a complete fix for the three originally
broken tests: `test_spill` and `test_scalar_full` each have at least one more, separate, unidentified
bug. **This verification build (`engine_new`) should not be treated as a clean baseline** — it
diverges from the `engine_isp` binaries used for the original 13-test passing baseline in ~15 files
unrelated to this fix, which is the most likely explanation for `test_vreduce`/`test_cmov` newly
failing here. Recommend re-testing this exact one-line fix against the professor's actual
distributed `engine_isp` source tree (not `engine_new`) before reporting it as the official fix.
Stopping here as instructed — no further investigation this session.

---

## 2026-06-18 — Engine rebuilt with `mc->Pop_From_Ras()` fix in `McodeExecute.cpp`; three confirmation checks run, all unchanged

Engine rebuilt (`mcode_run` timestamp Jun 18 11:18, in
`complier_Apara/engine_new/AjitHpcAccelRepo/.../assembler/bin/`) with one change: a second source
suggested adding `mc->Pop_From_Ras();` inside `___execute_return_operation___` in
`McodeExecute.cpp`, right after `Top_Of_Ras_Stack()`, before `Set_Npc`, as a possible fix for the
call-depth bug. Three checks run against the rebuilt binary, as instructed. No further
investigation performed this session per explicit instruction.

**1. `noop_call.c` with `$halt` placed right before `f`'s `$return` — r1 at that point now?**
Could not observe r1 at the intended point, for a reason orthogonal to the fix being tested: the
`$call f` instruction (`main`→`f`, a backward call — `f` is emitted at a lower address than the
call site) does not transfer control into `f` at all. The runtime's own trace shows
`npc=0x2000018` instead of `0x18` (`f`'s real address); PC then reads zero-filled memory and the
run stops after 10 ticks, having never executed `f`'s body or the inserted `$halt`. r1 simply
stays at `main`'s own pre-call prologue value, `0x328`, for the whole run. **Confirmed identical
with the old (pre-rebuild) `mcode_run` on the exact same `.obj`** — same `npc=0x2000018`, same
final r1=`0x328`. So this specific check is unaffected by today's rebuild either way — the rebuild
neither fixes nor changes it, because the call never reaches the code path the fix touches.

**2. test_struct / test_spill / test_scalar_full — did 0xa / 0x328 / 0x7ff0 change?**
**No change.** Re-ran each existing `.obj` against the rebuilt `mcode_run`: final r1 =
`0xa` (test_struct), `0x328` (test_spill), `0x7ff0` (test_scalar_full) — identical to the
pre-rebuild baseline. Cross-checked with a clean control (same three `.obj` files run against the
old pre-rebuild `mcode_run`): identical `0xa` / `0x328` / `0x7ff0`. The `Pop_From_Ras()` fix did
not change any of these three results.

**3. test_alu / test_pack sanity check — still pass on the rebuilt binary?**
**Yes, both still pass**, no regressions from the rebuild itself. test_alu: final r1=`0xd` (13),
matches expected, zero errors in the run log. test_pack: final r1=`0xdead`, matches expected, zero
errors in the run log.

### Bottom line
The rebuilt engine changes nothing observable in any of these three checks — test_struct/
test_spill/test_scalar_full remain exactly as wrong as before (still do not trust their results),
test_alu/test_pack remain correct (rebuild introduced no regression), and the `noop_call.c` halt
probe couldn't exercise the fixed code path because the backward `$call f` itself resolves to a
wrong target (`0x2000018`) before execution ever reaches `f`. Stopping here as instructed — no
further hypotheses or investigation this session.

---

## 2026-06-17 — STANDALONE INCIDENT REPORT: nested function calls are broken; six causes checked and ruled out compiler-side; needs simulator source

**This entry is written to be self-contained — readable cold, without the rest of this file or
any conversation history.** It documents one evening's investigation into why `test_struct`,
`test_spill`, and `test_scalar_full` produce wrong results, despite compiling and assembling
without any error.

### The bug, in one sentence
**Any C function called from within another function (not `main` itself) returns a garbage
value instead of its actual return value** — confirmed with the simplest possible repro:
```c
int f(void) { return 6; }
int main() { return f(); }
```
This returns garbage (specifically, whatever `main`'s own prologue happened to leave in
register `$r1` — see below), not `6`.

### Why this matters / scope
Every test in the 19-program suite that has ever passed either has zero function calls beyond
`main`, or only calls compiler intrinsics (`__pack`, `__dot_*`, `__cmov_*`, `__vadd_*`, etc. —
these compile to inline instructions and never emit a real `$call`). `test_struct`,
`test_spill`, and `test_scalar_full` are the ONLY three tests in the entire suite with a real,
user-defined nested function call — which is exactly why this was never caught until today's
full hardware regression. **Do not trust the results of these three tests.** Everything else in
the suite is unaffected (confirmed via multiple zero-regression full-suite re-runs throughout
tonight's work).

### Two real bugs found and fixed along the way (kept — both still valid, just insufficient)
While investigating, found and fixed two genuine, separate bugs in `bundler.py` (the VLIW
instruction-bundling pass). Both are confirmed correct, cause zero regressions on the rest of
the suite, and are worth keeping regardless of the unresolved issue below:

1. **Aligner reorders bundle instructions by type.** `mcode_align` does not preserve program
   order within a bundle — it relocates `$ld`/`$st` to later slots than ALU/`$set`, regardless
   of what order the compiler emitted them in. Proven by diffing unaligned vs aligned mcode for
   a function prologue: `$st [SP+0] OLD_FP` / `+FP=SP` (intent: save OLD fp, then update) became
   `+FP=SP` / `$st [SP+0] FP` (now stores the NEW fp). Fixed by adding `c_mem_reads` tracking in
   `bundler.py`'s `_pack_bundles`: a non-memory instruction writing a register that an
   already-bundled memory instruction reads now forces a bundle split.
2. **Conservative SP+`$call` bundling hazard** (added at explicit request, since the exact
   phase interaction between an SP-modifying instruction and a co-bundled `$call` was unverified
   and the pattern is rare/cheap to avoid): `$call` is now never bundled with an instruction
   that writes `$r27` (SP) — forces a split.

Neither fix resolves the bug described here. Both were verified via full 19(+2)-test regression
— zero behavioral change to any previously-passing test.

### Six things checked and ruled out, in order, each with its own falsifiable test

**1. Bundle padding.** Per the ISA's quirk that any bundle containing a control-transfer
instruction must be padded to a full 8 instructions: checked the `.aligned.mcode` for the
failing bundle directly. It IS correctly padded to 8. Not a padding bug.

**2. Jump-target resolution.** Disassembled the assembled `.obj` (`mcode_disassemble`) and
confirmed `goto f_epilogue` resolves to the exact bundle containing `f_epilogue`'s real first
instruction (the SP-restore `+ $r27 = $r0 + $r26`). Not an addressing/jump-target bug.

**3. Bundle shape / instruction ordering within the failing bundle.** The failing bundle inside
`f` is:
```
- $r27 = $r27 - $r1       (SP -= frame size)
+ $r1  = $r0 + 6            (set return value)
? $r0 == $goto f_epilogue   (unconditional jump)
```
Control experiment: forced `main` itself into this exact same 3-instruction shape via
`int main(){return 6;}` (its own prologue's SP-reduction lands in the same bundle as the
return-value-set and the jump — same shape, same instructions, different label name only).
**This works correctly** (`r1=6` at the end). Diffed the two bundles byte-for-byte — identical
except for the label name (`f_epilogue` vs `main_epilogue`, necessarily different relative
offsets). So the bundle shape itself, and whatever order the aligner puts its three instructions
in, is NOT the problem — it works when this exact shape is `main`'s own code.

**4. Caller-save / restore ordering.** There's a known fix from the 28-register-allocator work:
copy the return value (`$r1`) to its destination temp BEFORE restoring any caller-saved
registers, because restoring might otherwise clobber `$r1`. Checked whether this fails to
trigger for an edge case (callee with no arguments, no other live locals at the call site) by
tracing two minimal cases directly from the generated mcode:
   - `noop_call.c` (`f` takes no args): there is NO caller-save/restore sequence at all in the
     generated mcode — zero `$st`/`$ld` between `$call f` and the return. Nothing is live at
     the call site, so the save list is empty. The "capture" instruction is `+r1=r0+r1`, a
     self-copy, only because the register allocator happened to assign the call's result temp
     to `$r1` itself (the first register in a fresh allocator pool). The ordering fix isn't
     misfiring here — it's simply never invoked.
   - `get_x_repro.c` (struct-pointer argument forces a real caller-save): the register that gets
     saved-and-restored is `$r3` (the pointer being passed as the argument) — NOT `$r1`. The
     capture instruction (`+r4=r0+r1`, reads r1 writes r4) and the restore instruction
     (`$ld r3 [FP-104]`, writes r3) touch completely disjoint registers. No possible conflict
     between them regardless of execution order.
   - In both cases, register-probed `$r1` immediately upon `$call` returning — already wrong in
     both (`0x328`, `0x7fe8` respectively) — confirming the corruption exists before ANY of this
     capture/restore code runs at all.

**5. Is the corruption visible from inside the callee, before `$return` even executes?**
(Hypothesis: if so, that's evidence of a problem visible purely from the callee's own
instruction stream, independent of anything about RAS or the call-return mechanism externally.)
Inserted `$halt` directly inside `f`'s epilogue, immediately after the unconditional jump lands
there, before the `$ld`/`$return` bundle executes. **`$r1 = 0x328` (808) at that exact point** —
already wrong, confirmed before `$return`/RAS-pop runs at all.

Cross-checked the literal values, not just "both look wrong": in `main`, `$r1` immediately
*before* `$call f` = `0x328`. Immediately *after* `$call f` returns = `0x328`. Inside `f`'s
epilogue = `0x328`. **All three are the identical bit pattern** — `$r1` never changes from
`main`'s own prologue-time value (`$set $r1 0 808`, its frame-size constant) anywhere across the
entire call.

(Side-note, fully resolved: `test_spill`'s wrong final answer is *also* exactly `0x328`. Checked
directly — `test_spill`'s `main` independently emits the identical `$set $r1 0 808` in its own
prologue. Both land on 808 because `72 (min stack-frame floor for ~0 declared locals) + 224
(caller-save reserve) + 512 (spill reserve) = 808` is the standard frame constant for any
function with few/no stack-declared locals — true for both, since test_spill's "28 live values"
during its spilling test are register-held temps, not stack-declared locals. Not a coincidental
number; the same underlying failure leaving the same literal residue in two different programs.)

**6. Is the failing write specific to something about how the CALLEE's own epilogue code is
generated — e.g., a scratch register accidentally chosen as `$r1`, clobbering the just-written
return value before `$return` runs?** Specific, falsifiable hypothesis: that the epilogue's SP
restoration might need a `$set <reg> 0 <framesize>` + subtract two-step sequence (since
frame sizes like 808 exceed the ALU's 10-bit signed immediate range, the same reason the
*prologue's* SP reduction needs this two-step pattern) — and that this might pick `$r1` as
its scratch register, clobbering the return value that was just written.

Checked directly from generated mcode, three separate real instances (`noop_call.c`'s `f`,
`get_x_repro.c`'s `get_x`, and `test_spill`'s actual `f01`) — **all three byte-identical**:
```
<name>_epilogue:
||
    + $r27 ($i64) $r0 $r26      ← SP = FP — a plain REGISTER COPY, no constant involved
;
||
    $ld ($i64) $r26 [$r27 + 0]   ← restore old FP
    $return
;
```
No `$set` anywhere in any epilogue, in any of the three. The reason: restoring SP only needs
`SP = FP` (a register-to-register copy), because FP already holds the exact value SP needs to
become — unlike the *prologue's* SP reduction, which needs to load the frame-size *constant*
and therefore needs the two-step `$set`+subtract pattern. There is no constant to load in the
epilogue, hence no scratch register of any kind is ever borrowed there. Cross-checked against
`_gen_IRFuncEnd` in `codegen.py`: it is a fixed, unconditional 3-instruction emission with zero
register-allocation logic, so it cannot produce a different pattern for any frame size, ever.
**Hypothesis not confirmed. No fix applied.**

### What's left standing after all six checks
Padding ✓ clean. Jump-target resolution ✓ clean. Bundle shape/ordering ✓ clean (proven by a
working depth-1 control case with byte-identical code). Caller-save/restore ordering ✓ clean
(checked two different minimal cases). Visible-before-`$return` ✓ confirmed wrong, but not
attributable to anything in the callee's own code (point 6). Epilogue scratch-register clobber
✓ clean (no scratch register exists in any epilogue, of any kind).

The one fact every check above converges on: **identical, byte-for-byte generated code is
correct when executed as the program's first (`apara_start→main`) call, and incorrect when
executed as a nested (`main→anything`) call.** Nothing in the compiler's output differs between
these two cases — only the calling context does. Re-confirmed there is zero special-casing of
the string `"main"` anywhere in `codegen.py`/`ir_gen.py`/`compiler.py`.

### What to check next (needs engine/simulator source — out of reach from the Python/mcode side)
In `MachineRun.cpp` (or wherever `$call`/`$return`/RAS push-pop is actually implemented):
does anything beyond PC save/restore happen across a `$call`/`$return` pair — specifically,
does register-file write-enable, or any register snapshot/restore, get gated by RAS depth in a
way that would make a register write commit correctly at depth 1 (the outermost call) but not
commit at depth ≥2 (any nested call)? That is the precise, narrow question this evening's
investigation has earned: not "is something wrong with calls" (six checks say the Python
compiler's output is correct), but "why does identical code commit a register write differently
depending on how many calls deep it's executing."

### Status
**Unresolved. Pausing here as planned.** Two real, unrelated bugs were found and fixed in
`bundler.py` along the way (kept, zero regressions, see above) — neither is the cause of this
issue. `test_struct`/`test_spill`/`test_scalar_full` remain untrustworthy until the question
above is answered. Everything else in the 19(+2)-program suite is confirmed unaffected and
unchanged by tonight's work.

---

## 2026-06-17 — r1's write fails INSIDE the callee, before $return; but generated code is byte-identical to a working depth-1 case

Two precise checks, no engine source needed:

**Check 1 — is r1 already wrong inside `f`, before `$return` runs?** Inserted `$halt` directly
inside `f`'s epilogue, before `$ld`/`$return` execute (right after the unconditional jump from
`f`'s body lands there). `r1 = 0x328` (808) at that exact point. So `+r1=r0+6` (the return-value
write, in the same bundle as the jump) never took effect — confirmed BEFORE `$return`/RAS-pop
even runs, inside `f`'s own execution.

**Check 2 — literal hex values, not characterizations.** In `main` (`noop_call.c`):
`r1` immediately before `$call f` = `0x328`. `r1` immediately after `$call f` returns = `0x328`.
Inside `f`'s epilogue (check 1) = `0x328`. **All three are the identical bit pattern** — `r1`
never changes from `main`'s own prologue-time value, all the way through the call and back.

**Is test_spill's `0x328` the same value or coincidence?** Checked directly: `test_spill`'s
`main` also emits `$set $r1 0 808` in its own prologue. Traced why both land on 808:
`72 (frame-size floor for ~0 declared locals) + 224 (caller-save) + 512 (spill reserve) = 808` —
the standard frame constant for ANY function with few/no stack-declared locals (test_spill's
main has few real stack locals; its 28 "live values" are register-held temps, not stack slots).
Not a coincidental number — the same underlying failure (a write to r1 not committing) leaving
behind the same literal value because both mains' prologues happen to use the same constant.

**But: ruled out this being specific to `f`'s generated code.** Diffed the exact failing bundle
inside `f` against the identical-shape bundle inside `main_trivial.c`'s `main` (the depth-1
control case that works) — byte-for-byte identical except for the label name (`f_epilogue` vs
`main_epilogue`, necessarily different relative offsets). Also confirmed zero special-casing of
the string `"main"` anywhere in `codegen.py`/`ir_gen.py`/`compiler.py` — `main` and `f` go
through the exact same codegen path. So identical generated code produces a correct result at
depth 1 and an incorrect one at depth 2+, with nothing in the instruction stream itself
differing. This still points at execution context (first `$call` vs a nested one), not at
anything the compiler generated differently — laid out as evidence for review, not asserted as
final, since "identical code, different result" is unusual enough to warrant a second opinion
before fully ruling out something exotic on the generation side.

**Still not fixed. Still do not trust test_struct/test_spill/test_scalar_full.**

---

## 2026-06-17 — Ruled out caller-save/restore ordering bug too — corruption exists the instant $call returns, before ANY post-call codegen runs

Checked the specific hypothesis that the known "copy r1 to dest BEFORE restoring saved registers"
28-register-allocator fix (see [[project_28reg_allocator]]) might not be triggering for a
no-argument/no-other-live-locals callee. Traced directly from the generated mcode for both
minimal repros, no engine source needed:

1. **`noop_call.c`** (`int f(void){return 6;} int main(){return f();}`): there is NO
   caller-save/restore sequence in the generated mcode at all — zero `$st`/`$ld` between
   `$call f` and the return. `saved` is empty because nothing is live at that call site. The
   "capture" is a self-copy (`+r1=r0+r1`) only because the allocator happened to assign the
   call's result temp to `r1` itself. The ordering fix isn't misfiring — it's not even invoked.
2. **`get_x_repro.c`** (argument triggers real caller-save): the saved/restored register is `r3`
   (the pointer being passed as the argument), NOT `r1`. The capture (`+r4=r0+r1`, reads r1
   writes r4) and the restore (`$ld r3 [FP-104]`, reads FP writes r3) touch completely disjoint
   registers — no possible conflict between them regardless of bundle/instruction order.

**Then checked the more fundamental thing directly**: in both cases, register-probed `r1`
immediately upon `$call` returning, before ANY of this capture/restore code executes. **Already
wrong in both** — `0x328` for `noop_call.c`, `0x7fe8` for `get_x_repro.c` (matching prior
findings). So the corruption is present at the exact moment control returns from the call,
before the compiler's own post-call sequencing (capture, restore, or otherwise) has run at all.

**Conclusively rules out**: bundle padding (checked earlier), jump-target resolution (checked
earlier), bundle-shape/ALU-vs-branch ordering (checked earlier via the `main_trivial.c` control
case), and now caller-save/restore ordering (checked here, in both the simplest and the
argument-passing case). The bug is not anywhere in `bundler.py` or `codegen.py`'s call-handling
logic that's been inspected so far — it is specifically about what register state exists the
instant a depth-≥2 `$call` returns. This now needs `MachineRun.cpp`'s `$call`/`$return`/RAS
implementation to go further.

---

## 2026-06-17 — Ruled out compiler-side bundling/addressing bugs for nested calls — isolated to a register-state question at the $call/$return boundary

Two cheap, compiler-side checks before escalating to a hardware question (both came back clean):

1. **Bundle padding**: confirmed the minimal repro's failing bundle (`- $r27=$r27-$r1` / `+$r1=$r0+6`
   / `?goto f_epilogue`) IS correctly padded to a full 8 instructions in the `.aligned.mcode`,
   matching the control-transfer quirk. Not a padding bug.
2. **Jump target resolution**: disassembled the assembled `.obj` and confirmed `goto f_epilogue`
   resolves exactly to the bundle containing `f_epilogue`'s real content
   (`+ $r27 ($i64) $r0 $r26`, the SP-restore instruction) — no address mismatch. Not a compiler
   addressing bug.
3. **test_scalar_full**: confirmed directly — it DOES call `add3`/`max2`/`fact`, real nested
   user-function calls, same shape as test_struct/test_spill. Plausible same root cause but not
   separately proven beyond having the same call shape.

### New, sharper finding: the exact same bundle works at depth 1, fails at depth 2+
Since the two checks ruled out a compiler bug, ran a control experiment: forced `main` itself
into the IDENTICAL failing bundle shape via `int main(){return 6;}` (its own prologue's SP
reduction lands in the same bundle as the return-value-set and the unconditional jump — same
3-instruction shape as the failing case inside `f`). **This works correctly** (`r1=6`). The
*only* difference between the working and failing case is call depth: `apara_start→main` (depth
1) works; `main→f` (depth 2+) doesn't, for an otherwise byte-identical bundle pattern.

Traced register state precisely across the depth-2 call boundary (`main` calling the trivial
`f(){return 6;}`): immediately after `$call f` returns, `r26` (FP) and `r27` (SP) are BOTH
correctly restored to main's pre-call values (`0x7ff8`, `0x7cd0` respectively — verified
correct). But `r1` reads back as `0x328` (808) — exactly the constant `main`'s OWN prologue had
set in `r1` *before* making the call. It's as if `f`'s write to `r1` (its return value) never
propagates back to the caller at all, while FP/SP restoration works perfectly.

### Conclusion: not a compiler bug, narrowed to a specific hardware/simulator question
Padding ✓, jump-target resolution ✓, bundle shape ✓ at depth 1 — the only remaining variable is
nested call depth itself. This is now a precise question for `MachineRun.cpp` (or wherever
`$call`/`$return`/RAS handling lives): does anything beyond PC save/restore happen across a
`$call`/`$return` pair that could cause the return-value register specifically to revert to its
pre-call value, and why would that depend on call depth (works for apara_start→main, fails for
main→anything)? Recommend checking RAS push/pop logic and whether register file state is
snapshotted/restored alongside PC for any depth beyond the outermost call.

**Still not fixed. Still do not trust test_struct/test_spill/test_scalar_full's results.**

---

## 2026-06-17 — Conservative SP+$call bundler fix applied (as requested) — does NOT resolve struct/spill/scalar_full; root cause is deeper than first thought

### The requested fix, applied exactly as scoped
Added a new hazard case to `bundler.py`: never bundle `$call` together with an instruction that
modifies SP (`$r27`) — forces a bundle split between them. Conservative, narrow, only fires near
call sites. **Zero regressions** — re-ran the full 19-test suite (+2 isolated subword tests),
every previously-passing test (`test_alu/array/subword/ldst/branch/vadd/slice/vreduce/pointer/
cast/cmov/dot/pack/subword_i8/subword_i16`) still produces the exact same correct value.
`test_2d`/`test_fsqrt` still crash the aligner, unrelated, untouched (held per instruction).

### But it does NOT fix test_struct / test_spill / test_scalar_full — all three UNCHANGED
| Test | Before this fix | After | Expected |
|---|---|---|---|
| test_struct | 0xa | 0xa (unchanged) | 0 |
| test_spill | 0x328 | 0x328 (unchanged) | 0x1d1 |
| test_scalar_full | 0x7ff0 | 0x7ff0 (unchanged) | 0xc |

### Why: the SP+$call bundling was never the actual root cause — found something deeper
Verified the fix correctly splits the bundle (`$call f` now alone in its own bundle, confirmed in
the generated mcode) — but re-tested the simplest possible nested call
(`int f(int x){return x+1;} int main(){return f(5);}`) and it's STILL wrong after the fix.
Traced further and found an even smaller failing case:
```c
int f(void) { return 6; }
int main() { return f(); }
```
This fails too — `f` returns garbage instead of 6. Traced the corruption to INSIDE `f`, in the
bundle that sets the return value and jumps:
```
- $r27 = $r27 - $r1     (SP -= frame size)
+ $r1  = $r0 + 6          (set return value)
? $r0 == $goto f_epilogue (unconditional jump)
```
Register-traced this directly: by the time control reaches `f_epilogue` (immediately after this
bundle), r1 is ALREADY wrong — the `+r1=6` write did not take effect, even though this is a
plain ALU write with no memory instruction involved at all. This is a DIFFERENT mechanism than
the aligner's ALU-vs-MEM slot reordering (see the previous entry below) — here a register write
co-bundled with an unconditional jump appears not to commit, specifically for a function reached
via a NESTED call (depth ≥ 2: apara_start→main→f). The exact same "+result; goto epilogue"
shape is used by `main`'s own return in literally every passing test (depth 1: apara_start→main)
and works fine there — so it's specific to nested calls, not the pattern in general.

**Not yet fixed. Not yet root-caused to a specific, nameable mechanism** — only empirically
narrowed down to "write + unconditional jump, same bundle, inside a depth-≥2 call". This is now
the third open hardware-semantics question alongside `$nop`'s parse failure and (resolved)
`$pack`'s operand order — needs simulator source inspection, not further guessing from this side.
Recommend checking how `MachineRun.cpp` (or wherever bundle retirement/writeback is implemented)
handles register writes in a bundle that also contains a control-transfer instruction, especially
across a `$call` boundary.

### Bottom line on function calls
**Do not trust any test with a real (non-intrinsic) nested function call yet.** `test_struct`,
`test_spill`, `test_scalar_full` remain the only three tests in the suite with this shape, and
all three are still broken for a reason beyond the two bundler fixes applied so far today.

---

## 2026-06-17 — Major finding: aligner reorders bundle instructions by type (ALU before MEM) — partially fixed, ANY real function call is at risk

### XNOR fix applied, but test_logic blocked by something else
Applied `'~~' → '~^'` in `_APARA_OP` — confirmed correct and necessary on its own. But `test_logic`
still fails identically: `$nop` itself doesn't parse, even in total isolation (a file containing
only `$nop` fails; `$null` in the same harness works). See [[project_nop_parse_bug]]. Unrelated
to XNOR — a second, separate blocker in the same test.

### The big discovery: the EXTERNAL ALIGNER REORDERS instructions within a bundle
While tracing `test_struct` (see [[project_call_phase_hazard]] for full detail), found that
`mcode_align` does not preserve the textual order of instructions within a bundle — it relocates
`$ld`/`$st` instructions to LATER slots than ALU/`$set` instructions, REGARDLESS of which order
the compiler wrote them in. Proven by diffing a function's unaligned vs aligned mcode side by
side: `$st [SP+0] OLD_FP` / `+FP=SP` (intended: save OLD FP, THEN update FP) gets reordered to
`+FP=SP` / `$st [SP+0] FP` (now stores the NEW FP). **This silently breaks the "VLIW reads all
operands before any writes" assumption bundler.py was built on, specifically whenever a memory
instruction is meant to read a register an ALU instruction in the same bundle is about to write.**

**Fixed (partially) in `bundler.py`**: added `c_mem_reads` tracking — a non-memory instruction
writing a register that an already-bundled memory instruction reads now forces a split. Verified
this resolves the specific FP-corruption-on-return case (confirmed via register trace: FP is now
correctly restored to the caller's value after a callee returns). **No regressions** — full 19+2
test suite re-run, all previously-passing tests still pass; `test_vadd`/`test_slice`/`test_cast`/
`test_dot`/`test_vreduce`/`test_subword`/`test_pack` (today's other fixes) all still correct.
`test_scalar_full` improved (`0x3` → `0x7ff0`, an address-shaped value — still wrong, but
different, suggesting partial progress).

### Still broken: the SIMPLEST POSSIBLE function call still fails
```c
int f(int x) { return x + 1; }
int main() { return f(5); }
```
Even with the fix above, this returns `0x328` (808 — main's own frame-size constant, clearly
stale/never-overwritten) instead of `6`. Traced to a bundle where an SP-reducing ALU instruction
and `$call` are packed together: `- $r27 = $r27 - $r1` / `+ $r2 = $r0 + 5` / `$call f`. This is a
DIFFERENT interaction than the memory-phase one just fixed — `$call` itself doesn't count as a
"memory" instruction in the current model, so today's fix doesn't touch it. Whatever the exact
mechanism, the simplest possible nested call is not yet reliable.

**`test_spill` independently confirmed to have the identical bundling pattern**
(`- $r27 = $r27 - $r1` / `$call f01`, same shape) — found by checking its mcode directly, not by
assuming. Consistent with, but not yet proof of, the same root cause. `test_struct` likely the
same (it has nested calls throughout). `test_scalar_full`'s remaining failure not yet pinned to
this specific call site, but its calls (`add3`, `max2`, `fact`) are real function calls too.

### This affects EVERY test with a real (non-intrinsic) function call
Going back through the suite: every test that's passed so far either has no function calls beyond
`main`, or only calls compiler intrinsics (`__pack`, `__dot_*`, `__cmov_*`, etc. — these compile to
inline instructions, never `$call`). `test_struct`/`test_spill`/`test_scalar_full` are the ONLY
tests in the suite with real `$call`-based user function calls — which is exactly why this has
never been caught before. **Not a hypothesis: confirmed directly with the 2-line `f(x)=x+1`
repro above, which has nothing struct/pointer/spill-specific about it at all.**

### Not fixed yet — needs hardware/simulator confirmation before guessing further
This is now squarely a "what does the simulator actually do" question, the same category as the
`$pack` operand-order and `$nop` parse issues. Specifically: when a bundle contains an SP-modifying
ALU instruction together with `$call`, what value of SP does the call/jump mechanism actually use
— the old or the new? And more generally, is there a complete, authoritative description of the
bundle's execution phases (which instruction types execute in which order) anywhere in the
simulator source (e.g. `MachineRun.cpp`, already referenced for the PACK semantics question)?
Guessing further risks another round of "fixed something, still broken for a different reason."

---

## 2026-06-17 — $set-merge bug FIXED; test_subword/test_cast/test_dot/test_vreduce all resolved

### The fix (codegen.py)
`_load_const(reg, value)`: for any value needing more than one 16-bit `$set` field, build the
low 16 bits directly into `reg`, then for each additional chunk — borrow a scratch register via
`_safe_borrow()`, recursively load that chunk into it, shift it into position, OR it into `reg`,
unborrow. Recursion naturally handles arbitrary width (not just 32-bit) since each level peels
off one more 16-bit chunk via `value >> 16`. Also fixes a second latent bug: the old code masked
`hi = (value >> 16) & 0xFFFF`, silently truncating anything beyond 32 bits even if merging had
worked correctly.

`_gen_IRGlobalDecl` (startup global-initializer code) needed a separate, non-recursive version
(`_append_const_lines`, now a static helper) since it runs before any function's register
allocator exists — it can only use the two dedicated init-scratch registers (`$r30`/`$r31`), not
borrow arbitrarily. Iterates chunks with an explicit 64-bit mask instead of recursion (avoids an
infinite loop from Python's sign-extending right-shift on negative values). The DMEM byte-offset
half of that function doesn't need this at all — DMEM is at most 64KB (ISA §1), so a byte offset
always fits one `$set` field; that path now just stays single-chunk and reuses `$r31` directly.

Verified against the original minimal repro (`gi=100100; gi=gi+1; if(gi!=100001)...`) and the
exact `r30` register trace that first proved the bug — now builds correctly and returns success
on hardware.

### Effect: all 4 originally-blocked tests now resolved
| Test | Before | After | Expected |
|---|---|---|---|
| test_subword | -12 (failed check #12) | **0x1 (full pass)** | 1 |
| test_dot | 0xf | **0x5a** | 90 |
| test_vreduce | 0x17 | **0x4c** | 76 |
| test_cast | 0x78ac9ccd (close but wrong) | **0x78ab9bcd** | 0x78ab9bcd |

`test_cast` needed a SECOND fix beyond the $set bug: probing showed `big` (declared `int64_t`,
a typedef) was being stored/loaded via `$ld`/`$st ($i32)` instead of `($i64)`, truncating it.
Root cause: `_type_size`'s `IdentifierType` branch only recognized literal base-type names
("long long", etc.) — pycparser does NOT expand a typedef to its underlying structure at the use
site, so `int64_t x;` arrives as `IdentifierType(names=['int64_t'])`, an unrecognized name,
silently defaulting to 4. This is exactly the "related, not-yet-fixed" half of
[[feedback_elem_size_scalar_bug]], now confirmed as a real bug via this test and fixed: added a
module-level `_TYPEDEF_SIZE` dict in `ir_gen.py`, populated by `visit_Typedef` for every typedef
(`_TYPEDEF_SIZE[node.name] = _type_size(node.type)`), consulted by `_type_size` as a fallback
after the literal base-type dict. Confirmed fix: `big` now uses `$st`/`$ld ($i64)`, and the full
expected value `0x78ab9bcd` comes out correct.

Full regression re-run (19 programs + the 2 isolated subword tests): no regressions, all
previously-passing tests still pass; `test_struct`/`test_spill`/`test_scalar_full` unchanged
(separate causes, not yet investigated); `test_2d`/`test_fsqrt` still crash the aligner
(held, untouched, per explicit instruction).

---

## 2026-06-17 — XNOR mnemonic fixed; test_logic blocked by a SEPARATE, new issue: $nop doesn't parse

`codegen.py`'s `_APARA_OP['~^']` was `'~~'`, now `'~^'` — confirmed correct and necessary (the
literal `~~` symbol has no meaning in the ISA). But `test_logic` still fails to assemble after
this fix, with the exact same error as before: `test_logic.mcode:46:7: expecting ''u'', found
''o''` — at `$nop`. **Isolated with a minimal repro: a file containing only `$nop` (nothing
else) fails the identical way; a file containing only `$null` in the same harness parses fine.**
So this is a real, separate, confirmed assembler-grammar issue with `$nop` specifically — not
something the XNOR fix touches, and not something I should guess at (same category as the
$pack-operand-order and $set-label questions: the ISA doc says `$nop` is valid syntax, but the
parser doesn't accept it, and that gap can only be resolved by checking the parser source like
`McodeParser.cpp`, not by guessing alternate spellings). **`__nop()` is only used by test_logic** —
no other test in the suite touches it. Holding `test_logic` here pending grammar confirmation.

---

## 2026-06-17 — $pack fixed (grammar + bundler hazard); same hazard gap fixed for 6 more instructions

### $pack: two stacked bugs, both now fixed
1. **Grammar/operand order** (user confirmed via `McodeParser.cpp:570`): real order is
   `$pack <packed_nbits> <rd> <word_nbits> <rs2>`, not `<rd> <packed_nbits> <word_nbits> <rs2>`
   like the ISA doc's own example shows. `_gen_IRPack` in `codegen.py` now emits the correct order.
   This alone fixed the `mcode_align`/`mcode_assemble` crash.
2. **Bundler hazard**: `bundler.py`'s `_parse_deps` didn't recognize `$pack` *at all* — it has no
   case for it, so it fell through to "zero reads, zero writes" for hazard-tracking purposes. Since
   `$pack rs2` implicitly reads BOTH `rs2` and `rs2+1` (the consecutive pair, per `_gen_IRPack`'s
   `borrow_pair()`), this caused an undetected RAW hazard whenever an adjacent instruction in the
   same bundle wrote `rs2+1` — exactly what `_gen_IRPack` itself emits (`+ p2 = b` right next to
   `$pack ... p1`). Root-caused empirically: traced r1/r2 via truncated-mcode-plus-$halt probes,
   confirmed they held the correct values right up until the bundle boundary, and confirmed `$pack`
   alone (no co-bundled writes) computes correctly (`0xbeefdead` for `lo=0xbeef,hi=0xdead`) —
   isolating the corruption to the missing pair-read tracking specifically.

   **Fixed** by adding a `$pack` case to `_parse_deps` that returns `reads={rs2, rs2+1}`,
   `writes={rd}`.

   `test_pack` now runs end-to-end correctly: r1=`0xdead`. (The test's own comment expected
   `0xBEEF` — that assumption was backwards per the now-confirmed semantics, "first register →
   upper bits, last → lower bits"; `0xdead` is the actual correct low-16-bits of `0xbeefdead`.
   Not a bug, just a stale comment in the test file.)

### Same hazard-tracking gap found in 6 MORE instructions — also fixed
Auditing why the gap existed for `$pack` revealed `_parse_deps` has NO case at all for
`$cast`/`$fsqrt`/`$cmov`/`$slice`/`$v`/`$dot`/`$vreduce` — every one of them fell through to
"zero reads, zero writes," meaning **none of them had any hazard protection** in the bundler.
Confirmed this directly hit `$cast` too while debugging `test_pack` further (a `$ld` writing `r8`
and a `$cast` reading `r8` were bundled together, same RAW-hazard shape, r9 came out as 0 instead
of the loaded value). Added proper read/write tracking for all of them — see `bundler.py` for the
exact field semantics per instruction (notably: `$cmov`'s `rd` is both read and written;
`$dot $accumulate`'s `rd` is also read).

### Effect on the regression batch
Re-ran `test_subword, test_dot, test_struct, test_spill, test_scalar_full, test_vadd, test_vreduce,
test_slice, test_cast` (the batch from the bundler-memory-hazard fix) plus the full hardware
regression after this second fix:

| Test | Before this fix | After | Status |
|---|---|---|---|
| test_pack | crashed at align/assemble | r1=0xdead | **FIXED** (the explicit ask) |
| test_vadd | 0x0 | 0x4 | **FIXED** (expected 4) |
| test_slice | 0x0 | 0xb7 | **FIXED** (expected 183=0xb7) |
| test_cast | 0x0 | 0x78ab0000 | improved, still wrong (expected 0x78ab9bcd) |
| test_dot | 0x7ff0 | 0xf | improved, still wrong (expected 0x5a=90) |
| test_vreduce | 0x20001 | 0x17 | improved, still wrong (expected 0x4c=76) |
| test_subword | -12 | -12 (unchanged) | still fails check #12 — this is the [[project_set_no_merge_bug|$set merge bug]], untouched by this fix |
| test_struct | 0xa | 0xa (unchanged) | still unexplained |
| test_spill | 0x328 | 0x328 (unchanged) | still unexplained |
| test_scalar_full | 0x3 | 0x3 (unchanged) | still unexplained |

No regressions: test_alu, test_array, test_ldst, test_branch, test_cmov, test_pointer all still
correct. test_2d and test_fsqrt still crash the aligner with the same pre-existing assertion
failure (`Calculate_Pad_For_Alignment`) — unrelated to any of today's fixes, not yet diagnosed.
test_logic (XNOR mnemonic typo) also not yet fixed.

### Status: real progress, more remains
Two real bugs found+fixed today in `bundler.py` (memory hazard, missing-instruction hazard gap)
plus the `$pack` grammar fix in `codegen.py`. 3 of 9 originally-failing tests now fully resolved.
The remaining ones have at least one more distinct cause each: the confirmed `$set`-merge bug
(test_subword, very likely test_cast/test_dot/test_vreduce too, given the "improved but still
wrong" pattern suggests SOME of their computation is now right but multi-field constants are
still corrupted), and fully unexplained issues in test_struct/test_spill/test_scalar_full.

---

## 2026-06-17 — Bundler memory-hazard FIXED; found a second bug ($set doesn't merge)

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

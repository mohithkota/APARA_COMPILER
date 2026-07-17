# Bug Report: APARA Simulator (`engine_isp`) — Solved vs. Not Solved

Consolidated across the entire project history (compiled from `compiler/STATUS.md`'s dated entries
and this session's own verification work). Covers every confirmed defect found in the
**simulator/assembler source itself** (`engine_isp/.../assembler/src/*.cpp`) — not in the Python
compiler being developed. Each entry below was confirmed by checking that the compiler emitted the
textually-correct mcode instruction, and that the simulator's own execution/assembly of that
correct instruction is what's wrong.

**Authoritative binary**: `engine_isp/assembler/bin/mcode_run`. All "current status" claims below
refer to this binary, which is what every `run.sh` in this project uses and what all 5\,992
independently-verified test checks were run against (see the "Additional finding" section at the
end for why a second, non-authoritative binary elsewhere in the project tree must not be confused
with this one).

**Scorecard**: 6 confirmed engine-side bugs total — **3 solved, 3 not solved.**

---

## SOLVED (3)

## Bug E1 (SOLVED): `$ld` sub-word loads always read the upper half-word, ignoring the real byte offset

**File**: DMEM load logic in the simulator's execute path (fixed upstream by the hardware/VM team;
this project never had source-diff visibility into the fix itself, only behavioral confirmation).

**Symptom**: `$ld ($i32)` always returned bits `[63:32]` of the 8-byte DMEM word, regardless of the
byte offset actually specified in the instruction's address. `$st ($i32)` was correct (wrote upper
or lower 32 bits depending on offset) — only `$ld` was broken. This meant any value stored at
byte-offset 4 of a word (the "lower half" placement) could never be read back correctly by a
32-bit load.

**Why it mattered**: this is the load instruction underlying every `int`, `short`, and `char`
scalar/array access in any compiled C program — i.e. almost everything.

**Workaround used while the bug was live** (compiler-side, predates this fix): every element,
regardless of C type, was allocated its own full 8-byte-aligned DMEM slot
(`dmem_stride = max(elem_bytes, 8)`), and `codegen.py` always emitted `($i64)` loads/stores
regardless of the real element width — sidestepping the broken sub-word addressing entirely
instead of relying on it.

**Fix**: fixed in the upstream VM/hardware build; the fixed binaries were pulled into this
project's `engine_isp/assembler/bin/`.

**Verification**: full 19-program hardware regression (2026-06-17) after pulling the fix.
`test_subword_i8.c`/`test_subword_i16.c` (isolated single-width probes) both return `r1=1` (full
pass) on hardware. `test_array`/`test_cmov`/`test_branch`/`test_pointer` (which exercise `$i32` in
other shapes) all confirmed correct. **Status: confirmed working on hardware, currently in
production use** — the compiler's sub-word load/store codegen for `i8`/`i16`/`i32` is built on top
of this fix and is part of the current verified baseline.

---

## Bug E2 (SOLVED): `$call`'s disassembler sign-extended the wrong bit, breaking every backward call

**File**: `McodeDisassemble.cpp`
**Function**: `DisassembleToCallInstr`
**Line**: 266

### The code (exact)
```cpp
266: int32_t relative_jump = (int32_t) Sign_Extend(25, Get_Slice (24, 0, hex_instr));
```

### What's wrong
`Sign_Extend`'s first argument is a **bit index**, not a width
(`McodeUtils.cpp`: `pad_ones = (1 << sign_index) & x`). `$call`'s jump field is 25 bits wide —
bits `24:0` — so its real sign bit lives at index **24**, not 25. Calling `Sign_Extend(25, ...)`
checks a bit that is always 0 on a value already masked down to `24:0`, so **negative (backward)
call offsets were never sign-extended.**

### Why this mattered
Any function called from inside another function — not `main` itself — that was *defined before
its caller in source order* (the completely normal C pattern, e.g. a helper `f()` defined above
`main()`) computed a backward jump. Every such call computed `target + 2^25` instead of `target`,
landing in zero-filled garbage memory. `f`'s body never executed; the caller's return register held
whatever stale value happened to be sitting in it before the call. **Six separate compiler-side
hypotheses were checked and individually ruled out first** (bundle padding, jump-target resolution,
bundle shape/ordering, caller-save/restore ordering, callee-internal codegen, epilogue
scratch-register clobber) before the investigation moved to the simulator's disassembler source
directly and found this.

**Confirmed isolated to `$call`, not systemic**: `DisassembleToBranchInstr` (same file, line 302)
does `Sign_Extend(11, Get_Slice(16,5,...))` on its own 12-bit field (bits `16:5`, real sign bit at
index 11) — called with 11, correctly indexed. This is consistent with `while`/`for` loops (which
use `$branch`, not `$call`, for their backward jumps) having always worked correctly.

### Fix
```cpp
int32_t relative_jump = (int32_t) Sign_Extend(24, Get_Slice (24, 0, hex_instr));
```

### Verification
- Minimal repro (`int f(void) { return 6; } int main() { return f(); }`): before the fix, `$call f`
  resolved to `npc=0x2000018` and never entered `f`; after the fix, disassembly correctly shows
  `$call l_24`, `f`'s body executes, and the program halts with `r1=6` exactly as expected.
- `test_struct.c` (which calls helper functions defined above `main`) went from wrong (`0xa`) to
  exactly correct (`0x0`) with this fix alone.
- Full regression confirmed **zero regressions** among the 11 other already-passing tests.
- **Currently in production use**: `test_struct` is part of the current 393-check, 0-error baseline
  (2026-06-20), confirming this fix is present in the authoritative `engine_isp/assembler/bin/mcode_run`.

---

## Bug E3 (SOLVED): instruction memory was half the documented size, silently truncating larger programs

**File**: `McodeClasses.hpp`
**Lines**: 139, 143

### The code (before fix)
```cpp
139: uint32_t __instruction_memory[2048];   // stale comment claimed "16KB" -- 2048 words is only 8KB
...
143: static int Instr_Mem_Size_In_Words() { return 2*1024; }
```

### What's wrong
`AparaReference.pdf`, p.6, §1, Figure 1.1, states explicitly: *"The instruction memory provides
16KB of instruction space to each accelerator. Each instruction is 4-bytes."* 16KB ÷ 4 bytes =
**4096 words**. The simulator's array was sized `[2048]` — half the documented capacity, with a
comment claiming 16KB that never matched the actual 8KB the array provided.

`McodeAccelerator.cpp`'s instruction loader (`Init_Instruction_Memory`, lines 88–101) silently
**drops** — logs an `Error:`, does not write, does not abort — any instruction whose `pc >= 2048`.
A program larger than 2048 words runs off the end of its own truncated body into zero-filled
memory, with no crash and no obvious symptom — execution just continues into `$null` until the
tick budget expires, leaving the return register frozen on whatever value it last held (which can
coincidentally look like a plausible, but wrong, answer).

### Why this mattered
Two real test programs were large enough to trigger it: `test_scalar_full` (2688 words, 640
silently dropped) and `test_spill` (2496 words, 448 dropped). Both were misdiagnosed for a time as
having a remaining *logic* bug, when in fact their actual return-value computation was simply never
loaded into memory at all.

### An intermediate false lead, corrected before finalizing
A first attempt bumped the array further than necessary (to `[16384]`) as a verification-only
change and was initially second-guessed and reverted on the strength of a verbal claim that "2048
words is the real hardware limit." That claim was then checked directly against the written ISA
specification (cited above) and found to be incorrect — 4096 words, not 2048, is the documented
figure. The final fix uses the spec-correct value, not the over-sized verification value.

### Fix
```cpp
uint32_t __instruction_memory[4096];
static int Instr_Mem_Size_In_Words() { return 4*1024; }
```

### Verification
| Test | Before fix | After fix | Expected |
|---|---|---|---|
| `test_scalar_full` | `0x7ff0` (stale leftover) | `0xc` | `0xc` (12) |
| `test_spill` | `0x19` (stale leftover) | `0x1d1` | `0x1d1` (465) |

Combined with Bug E2's fix, all three originally-broken tests (`test_struct`, `test_spill`,
`test_scalar_full`) now produce exactly their expected values. Full 19-test regression: zero
regressions among the 14 other tests. **Currently in production use**: `test_spill`/
`test_scalar_full` are both part of the current 393-check, 0-error baseline.

---

## NOT SOLVED (3)

## Bug E4 (NOT SOLVED, major/correctness): `$vreduce` on unsigned vector types sign-extends instead of zero-extending

**File**: `McodeOperations.cpp`
**Function**: `__vreduce_operation__`
**Lines**: 146–197 (bug specifically at line 151, interacting with the `else` branch at 174–194)

### The code (exact)
```cpp
146:		for(I=0, fI = rs1_in_vector.size(); I < fI; I++)
147:		{
148:			uint64_t ele = rs1_in_vector[I];
149:
150:			// sign extend to 64-bits.
151:			int64_t r = (int64_t) Sign_Extend_64(src_type.Get_Nbits()-1, ele);
152:			if(signed_flag)
153:			{
154:				int64_t r = (int64_t) Sign_Extend_64(src_type.Get_Nbits()-1, ele);
155:				if(sub_opcode == __ADD)
156:					s_result  = s_result + r;
                    ...
173:			}
174:			else
175:			{
176:				if(sub_opcode == __ADD)
177:					result  = result + r;
                    ...
194:			}
195:		}
197:		ovalue = CastToU64 (signed_flag, dest_type.Get_Nbits(), (signed_flag ? ((uint64_t) s_result) : result));
```

### What's wrong
Line 151 unconditionally sign-extends `ele` into `r`, **before** the `if(signed_flag)` branch is
even reached — so it runs for every element regardless of the vector's actual signedness.

- The `if(signed_flag)` branch (152) redeclares its own local `r` at line 154, identical to line
  151 — correct, just redundant.
- The `else` (unsigned) branch (174) declares **no local `r` at all**. Every use of `r` inside it
  (e.g. line 177) falls through to the outer, already-sign-extended `r` from line 151. The unsigned
  path never zero-extends; it silently reuses the signed value.

### Why this matters
For an unsigned element with its top bit set (e.g. `$vu8` byte `0xFC`=252), the correct behavior
is to zero-extend to `252`. The bug sign-extends it to `-4` instead.

### Confirmed reproduction
```
vector (vu8, 8 elements): [1, 2, 3, 0xFC, 5, 6, 7, 8]
  $vreduce + on ($vi8)  -> 28    (correct, signed)
  $vreduce + on ($vu8)  -> 28    (simulator's actual output -- WRONG, should be 284)
```
Same pattern confirmed at `$vu16` (expected 65538, got 2) and `$vu32` (expected 4294967295, got -1)
— generic across all three unsigned vector widths.

### Suggested fix
```cpp
else
{
    uint64_t r = ele & __mmask__(src_type.Get_Nbits());   // zero-extend, not sign-extend
    if(sub_opcode == __ADD)
        result = result + r;
    ...
}
```
(`__mmask__` already exists and is used elsewhere in this same file.)

### Status
**Not fixed.** Currently causes exactly 3 documented/expected errors in the project's 5\,992-check
verification baseline (`isa_coverage_tests/test_vreduce_full.c`, landing precisely on the three
unsigned+negative-element cases). A standalone reproduction
(`vreduce_bug_demo.c`) independently confirms the same 3-error pattern. Flagged for the
professor/hardware team — out of scope for the Python compiler project to fix.

---

## Bug E5 (NOT SOLVED, major/correctness): 4-bit vector types (`vi4`/`vu4`) silently compute garbage

**File**: `McodeOperations.cpp`
**Function**: `CastToU64` (line 50)

### What's wrong
`CastToU64(int signed_flag, uint32_t nbits, uint64_t ival)` has a `switch(nbits)` with cases for
**8, 16, 32, 64 only** — there is no `case 4`. For any 4-bit-wide result (every `vi4`/`vu4`/`vf4`
vector element operation), the switch falls through with no case matching. `result` is declared
but **never assigned**, so the function returns whatever uninitialized garbage was already sitting
on the stack.

### Confirmed reproduction (hand-computed, 4/4 repeated runs)
| Type | a | b | Expected | Hardware actual | Result |
|---|---|---|---|---|---|
| `vu8` (control) | `0x0102030405060708` | `0x1010101010101010` | `0x1112131415161718` | `0x1112131415161718` | exact match |
| `vi4` | `0x1111111111111111` | `0x2222222222222222` | `0x3333333333333333` | `0x0` | **WRONG** |

Type parsing itself is correct (`isa.g`'s grammar correctly resolves `vi4_t` to `nbits=4`); the bug
is purely the missing switch case, not type recognition.

### Scope check (audited, not guessed)
Every other `switch(nbits)` block in the simulator (`McodeNumeric.cpp:493`, five blocks in
`McodeFpuUtils.cpp`) either explicitly handles 4 bits, or fails loudly with `assert(0)` instead of
silently returning garbage. `CastToU64` is the **only** one that declares its result variable
uninitialized with no `default:` case — confirmed isolated, not a systemic pattern across the
codebase.

### Suggested fix
Add a `case 4:` doing a manual 4-bit sign-extend/mask (no native C++ `int4_t` exists to reuse the
existing `___signed_cast___`/`___unsigned_cast___` macros with).

### Status
**Not fixed.** Explicitly deprioritized — `vi4`/`vu4` are not used frequently in this project's
target workloads, so this was flagged for the professor rather than chased further. `vu4`/`vf4`
are untested but share the identical code path, so should be assumed equally broken until checked.
The compiler's own ISA-coverage scope explicitly excludes `vi4`/`vu4` for exactly this reason.

---

## Bug E6 (NOT SOLVED, minor/diagnostic-only): misleading error messages in the post-condition file parser

**File**: `McodeAccelerator.cpp`
**Function**: `McodeAccelerator::Verify_Line`
**Lines**: 473, 515

This doesn't affect execution correctness, but cost real debugging time while building the
verification harness, because the error text actively points at the wrong cause.

### E6a — wrong keyword named in the "incomplete mem line" error
```cpp
468:	else if (tokens[1] == "mem")
469:	{
470:		//      mem <address>    mem-value [mem-value-mask]
471:		if (tokens.size() < 4)
472:		{
473:			McodeRoot::Error ("Incomplete reg line in Verify_Line", this);   // <-- says "reg", but this is the "mem" branch
474:			ret_val = 1;
475:		}
```
Line 473 is reached only when parsing a malformed **`mem`** line, but the message says
**"Incomplete reg line"** — copy-pasted from the `reg`-handling branch (line 427) and never updated.

### E6b — error message names the wrong token on an unrecognized line
```cpp
512:	else
513:	{
514:		ret_val = 1;
515:		McodeRoot::Warning("Unknown verify line keyword " + tokens[0], NULL);   // <-- checks tokens[1], reports tokens[0]
516:	}
```
Every branch above dispatches on **`tokens[1]`** (file format is `<thread-id> <keyword> <args...>`),
but the fallback warning prints **`tokens[0]`** (the thread-id) instead. Feeding a line in the
*wrong* format (e.g. missing the leading thread-id) lands here and reports something like
`Unknown verify line keyword reg` — which reads as though `"reg"` itself were invalid, when `"reg"`
*is* a valid keyword; the real problem is its position in the line.

### Suggested fix
```cpp
McodeRoot::Warning("Unknown verify line keyword " + tokens[1], NULL);   // report the field actually checked
McodeRoot::Error   ("Incomplete mem line in Verify_Line", this);        // for E6a
```

### Status
**Not fixed.** Diagnostic-only — does not affect any of the 5\,992 verification checks' actual
pass/fail correctness, only the clarity of error messages encountered while reverse-engineering the
file format empirically.

---

## Related findings (not code bugs, but relevant to "what's wrong with engine_isp")

### A secondary, minor robustness gap: no clean error for a zero-instruction bundle
While root-causing an unrelated **compiler-side** bug (the bundler was emitting two labels in front
of one bundle, which the assembler grammar doesn't allow), the resulting zero-instruction bundle
didn't produce a clean parse error: `McodeBundle.cpp`'s `Calculate_Capacity()` silently returns `0`
for it (logging an error but not aborting), and the crash only surfaces later, in
`Calculate_Pad_For_Alignment`'s division-by-zero guard, as a raw `Assertion '0' failed`. The
**trigger was compiler-side and has been fixed** (the bundler no longer emits duplicate labels), so
this code path is no longer hit by anything in this project — but the underlying engine-side gap
(crash instead of a clean diagnostic for this malformed input) was never itself patched. Not
re-flagged as a numbered bug above since it requires a compiler-side trigger that no longer occurs.

### `engine_isp` vs. `engine_new`: two diverged codebases, source of transient confusion
While testing Bugs E2/E3's fixes, a local rebuild (`engine_new`) was discovered to diverge from the
historical `engine_isp` baseline in roughly 15 source files unrelated to either fix
(`McodeBundle.cpp`, `McodeParser.cpp`, `McodeOperations.cpp`, `McodeProgram.cpp`, `McodeRoot.cpp`,
`McodeUtils.cpp`, `MachineRun.cpp`, `McodeBinaryCode.cpp`, `McodeInstructions.cpp`, plus two files,
`McodeAccelerator.cpp` and `McodeFpuUtils.cpp`, that don't exist in the `engine_isp` tree at all).
This caused `test_vreduce`/`test_cmov` to newly fail when run against `engine_new`'s toolchain,
even though they passed against the original `engine_isp` binary on the exact same `.obj` file —
not a regression from either fix, just evidence the two trees are genuinely different codebases,
not "the same engine plus two patches." **Resolved** in the sense that this project has since
consolidated on a single authoritative binary (`engine_isp/assembler/bin/mcode_run`); `test_vreduce`
and `test_cmov` are both part of the current 393-check, 0-error baseline against that binary.

### A hard architectural limitation, not a bug: function pointers cannot be implemented
There is no instruction that can load a function's absolute address into a register. `$call <label>`
encodes a PC-relative offset, not an absolute address. `$set`'s immediate-field grammar (confirmed
directly against the assembler's parser source) only accepts numeric literal tokens
(`UINTEGER`/`NINTEGER`/`HEXADECIMAL`) — a label name in that position is a hard parse failure, and
the assembler has no label-resolution mechanism outside of control-transfer instructions. This is
categorically different from the bugs above: it isn't broken logic to fix, it's a missing
capability that would need a new instruction or assembler feature. The compiler's own
`IRFuncAddr`/`IRIndirectCall` scaffolding is fully built and ready on the Python side; it is blocked
purely by this assembler-level gap. **Not solved — and not solvable from the compiler side at all.**

### Two disagreeing simulator binaries exist in the project tree
`engine_isp/assembler/bin/mcode_run` is **the correct, authoritative binary**, used throughout this
project and the one all 5\,992 verification checks were run against. A second, different binary,
`verification/bin/mcode_run` (confirmed different MD5 checksum), accepts a *different*
`PostCondition` file format (`<keyword> <args...>`, no leading thread-id) and belongs to a separate
RTL/testbench-oriented verification setup (the same directory holds a `tb` binary and a 243MB
`test_setup_test_bench`), not this project's compiler-targeted simulator:
```
$ ls -lh engine_isp/assembler/bin/mcode_run
-rwxrwxr-x 1 mohithkota mohithkota 1.8M Jun 18 13:05 mcode_run      <- authoritative
$ ls -lh verification/bin/mcode_run
-rwxrwxr-x 1 mohithkota mohithkota 1.7M Jun 20 12:22 mcode_run      <- NOT authoritative
```
Note the non-authoritative copy is actually *newer* by file timestamp — a reminder that file
recency alone is not evidence of correctness. Several pre-existing example result files elsewhere
in the project tree (e.g. under `verification/lastsem/`) appear to have been written against the
non-authoritative binary's format and will not verify correctly against the authoritative one.

---

## Summary table

| # | Bug | File | Status |
|---|---|---|---|
| E1 | `$ld` sub-word loads ignored real byte offset | DMEM load logic (upstream VM) | **SOLVED** |
| E2 | `$call` disassembler sign-extended the wrong bit | `McodeDisassemble.cpp:266` | **SOLVED** |
| E3 | Instruction memory half the documented size | `McodeClasses.hpp:139,143` | **SOLVED** |
| E4 | `$vreduce` unsigned sign-extends instead of zero-extends | `McodeOperations.cpp` (`__vreduce_operation__`) | **NOT SOLVED** |
| E5 | `vi4`/`vu4` vector ops return garbage | `McodeOperations.cpp` (`CastToU64`) | **NOT SOLVED** |
| E6 | Misleading `Verify_Line` error messages | `McodeAccelerator.cpp:473,515` | **NOT SOLVED** (diagnostic-only) |

## How these were found

E1/E2/E3 surfaced from a standalone incident investigation into nested function calls returning
garbage (2026-06-17/18) — six compiler-side hypotheses were checked and ruled out before the
investigation moved into the simulator's own source. E4/E5 surfaced while building the systematic
ISA-instruction-coverage sweep (`isa_coverage_tests/`) and its independent "no bias" golden
verification (compiling each test natively with `gcc` against `golden_stubs.h`, then checking the
simulator's real output against that ground truth via its own `-r <result-file>` `PostCondition`
mechanism). E6 was found while reverse-engineering the exact `PostCondition` file format
empirically, since the error messages encountered along the way described the wrong root cause.

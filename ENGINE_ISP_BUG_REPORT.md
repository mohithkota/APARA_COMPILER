# Bug Report: APARA Simulator (`engine_isp`) Source Defects

Found while building an independent verification system for the APARA C compiler (a separate
project — see `compiler/STATUS.md` and `isa_coverage_tests/README.md` for the full methodology and
5992 independently-verified test checks that surfaced these). Both bugs below are in the
**simulator/assembler source itself** (`engine_isp/assembler/src/*.cpp`), not in the compiler being
developed — confirmed by checking that the compiler emits the textually-correct mcode instruction
in each case, and that the simulator's own execution of that correct instruction is what's wrong.

---

## Bug 1 (major, correctness): `$vreduce` on unsigned vector types sign-extends instead of zero-extending

**File**: `engine_isp/assembler/src/McodeOperations.cpp`
**Function**: `__vreduce_operation__`
**Lines**: 110 (function start), 146–197 (full loop + result cast); bug is specifically lines
148–151 interacting with 174–194. All line numbers below confirmed against the current source,
re-read immediately before writing this report.

### The code (exact, lines 110–199)

```cpp
110: void __vreduce_operation__ (McodeOpcode sub_opcode, McodeType dest_type, McodeType src_type, vector<uint64_t>& rs1_in_vector, uint64_t& ovalue)
111: {
   ...                                                       (lines 112-145: setup + the float-type branch, not relevant here)
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
                    ...                                       (157-172: same pattern for MUL/OR/AND/XOR/XNOR/MAX/MIN)
173:			}
174:			else
175:			{
176:				if(sub_opcode == __ADD)
177:					result  = result + r;
                    ...                                       (178-193: same pattern for MUL/OR/AND/XOR/XNOR/MAX/MIN)
194:			}
195:
196:		}
197:		ovalue = CastToU64 (signed_flag, dest_type.Get_Nbits(), (signed_flag ? ((uint64_t) s_result) : result));
198:	}
199: }
```

### What's wrong

Line 151 computes `r` by **unconditionally sign-extending** `ele`, *before* the
`if(signed_flag)` branch at line 152 is even reached. This is outside both branches — it runs for
every element regardless of whether the vector's element type is signed or unsigned.

- The `if(signed_flag)` branch (line 152) **redeclares its own local `r`** at line 154
  (`int64_t r = (int64_t) Sign_Extend_64(...)`) — identical to line 151, so this branch is
  correct, just redundant.
- The `else` branch (line 174, the **unsigned** case) declares **no local `r` at all**. Every use
  of `r` inside it (e.g. line 177, `result = result + r;`) falls through to the **outer `r` from
  line 151** — which is already sign-extended. The unsigned path never zero-extends `ele`; it
  silently reuses the signed value.

### Why this matters

For an unsigned vector element whose top bit is set (e.g. a `$vu8` byte value of `0xFC` = 252),
the correct behavior is to zero-extend it to `252` before summing. Because of the bug, it gets
sign-extended to `-4` instead — the simulator computes the **signed** interpretation for
**unsigned** types.

### Confirmed reproduction

Two C test programs (compiled to APARA mcode and run on the real simulator binary), each with one
deliberately negative-bit-pattern element:

```
vector (vu8, 8 elements): [1, 2, 3, 0xFC, 5, 6, 7, 8]   (0xFC = 252 unsigned, -4 signed)
  $vreduce + on ($vi8)  -> 28    (1+2+3+(-4)+5+6+7+8 = 28)            -- correct for signed
  $vreduce + on ($vu8)  -> 28    (simulator's actual output)          -- WRONG, should be 284
                                  (1+2+3+252+5+6+7+8 = 284, zero-extended)
```

Same pattern confirmed at `$vu16` (expected 65538, got 2) and `$vu32` (expected 4294967295, got
-1) — the bug is generic across all three unsigned vector widths, not width-specific.

The compiler itself is confirmed innocent: it emits the textually correct instruction
(`$vreduce + rd ($vu8) rs`, with the correct `$vu8` type tag) in every case — verified directly in
the generated mcode before assuming the simulator was at fault.

### Suggested fix

Compute a properly zero-extended value inside the `else` (unsigned) branch instead of falling
through to the sign-extended outer `r`, e.g.:

```cpp
else
{
    uint64_t r = ele & __mmask__(src_type.Get_Nbits());   // zero-extend, not sign-extend
    if(sub_opcode == __ADD)
        result = result + r;
    ...
}
```

(`__mmask__(nbits)` is an existing helper already used elsewhere in this file and in
`McodeExecute.cpp`, declared in `McodeUtils.hpp`.) The outer line 152 declaration of `r` could then
be removed entirely, since both branches would have their own correctly-scoped value.

### Live test asserting this exact discrepancy

`isa_coverage_tests/test_vreduce_full.c` (and its golden file,
`isa_coverage_tests/test_vreduce_full/test_vreduce_full.result`) intentionally encodes the
*architecturally correct* expected values for the unsigned cases. Running it against the real
simulator currently produces exactly 9 matching results and 3 mismatches — landing precisely on
the three unsigned+negative-element cases described above, e.g.:

```
Error: PostCondition Mem[0x87] = 0x1c, expected 0x11c (mask=0xffffffffffffffff)
```
(`0x1c` = 28, the simulator's actual signed-equivalent output; `0x11c` = 284, the correct answer)

---

## Bug 2 (minor, diagnostic-only): misleading/wrong error messages in the post-condition file parser

**File**: `engine_isp/assembler/src/McodeAccelerator.cpp`
**Function**: `McodeAccelerator::Verify_Line`
**Lines**: 473, 515

This doesn't affect correctness of execution, but cost real debugging time while building the
verification harness, because the error text actively points at the wrong cause.

### Bug 2a — wrong keyword named in the "incomplete mem line" error

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
**"Incomplete reg line"** — copy-pasted from the `reg`-handling branch a few lines above (line 427)
and never updated.

### Bug 2b — error message names the wrong token on an unrecognized line

```cpp
383:	if(tokens[1] == "pc")
   ...
422:	else if (tokens[1] == "reg")
   ...
468:	else if (tokens[1] == "mem")
   ...
512:	else
513:	{
514:		ret_val = 1;
515:		McodeRoot::Warning("Unknown verify line keyword " + tokens[0], NULL);   // <-- checks tokens[1], reports tokens[0]
516:	}
```

Every branch above actually dispatches on **`tokens[1]`** (the second whitespace-separated field —
this binary's file format is `<thread-id> <keyword> <args...>`, e.g. `0 mem 0x80 0x1`). But the
fallback warning at line 515 prints **`tokens[0]`** (the thread-id, not the keyword) in the error
message. This is an easy format to get wrong in practice: a *different* simulator build elsewhere
in this same project (`verification/bin/mcode_run`, a separate binary, different MD5) accepts
`mem 0x2 0x0505...` with the keyword written **first** and no leading thread-id at all — confirmed
by testing the same file against both binaries directly. Feeding that format (or, symmetrically,
`reg 0 0xa 0x1`) to *this* binary lands in this `else` branch and reports:

```
Warning: Unknown verify line keyword reg
```

which looks like `"reg"` itself is an unrecognized keyword — actively misleading, since `"reg"` *is*
a valid keyword; the real problem is its position in the line.

### Suggested fix

```cpp
McodeRoot::Warning("Unknown verify line keyword " + tokens[1], NULL);   // report the field actually checked
```

and for 2a:

```cpp
McodeRoot::Error ("Incomplete mem line in Verify_Line", this);
```

---

## Additional finding (not a code bug, but relevant): two simulator binaries in this project disagree on the `PostCondition` file format

`engine_isp/assembler/bin/mcode_run` (the binary actually used by this compiler project's
`run.sh` scripts) requires `<thread-id> <keyword> <args...>` as shown above. A second,
different binary present in this same project tree, `verification/bin/mcode_run`
(confirmed different file — different MD5 checksum, so a different build), accepts
`<keyword> <args...>` with no leading thread-id at all for `mem` lines, and was the binary that
several pre-existing example result files in this project (e.g. under `verification/lastsem/`)
were apparently written for. Neither binary errors out helpfully when given the other's format —
the first prints the misleading message in Bug 2b above, and the second simply produces no
`PostCondition` output at all for a mismatched line. Worth knowing which binary a given
`run.sh`/example pairing was actually built against before debugging a "verification isn't
working" report.

---

## How these were found

Both surfaced while building `isa_coverage_tests/golden/golden_gen.py` and
`compiler.py`'s `try_golden_verify()` — an independent verification system that compiles each test
natively with `gcc` to get ground truth, then checks the APARA simulator's actual execution against
it via the simulator's own `-r <result-file>` `PostCondition` mechanism. Bug 1 was found because a
test's golden (correct) value disagreed with the simulator's real output on three specific checks,
traced down to this exact function. Bug 2 was found while reverse-engineering the exact
`PostCondition` file format empirically, since the error messages encountered along the way
described the wrong root cause.

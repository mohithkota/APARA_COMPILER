# Engine / Assembler C++ Source Changes

All modifications made to the APARA engine toolchain source
(`engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/src/`),
applied 2026-07-19 during the fuzz1000 full-ISA verification campaign.
Changes 1, 4, 5 are **new fixes** found by the campaign; changes 2, 3, 6 are
**re-applications** of the 2026-07-17/18 FP-campaign fixes that had been
deployed binary-only and were silently reverted by a `scons` rebuild (see
"Process incident" at the end).

After every change the toolchain was rebuilt (`scons` in `assembler/`) and the
full regression gate re-run: feature_sweep 16/16, universal 21/21,
pointer_bugs 15/15, fp01–09 50/50 — plus the fuzz1000 campaign
(613 PASS / 0 FAIL over 1013 gcc-verified programs, 102/102 instruction×type
coverage).

`testing/fuzz1000/check_engine_fixes.sh` greps the source for all six fixes —
run it **before and after any toolchain rebuild**.

---

## 1. `MachineRun.cpp` — `$fsqrt` executed as an unimplemented stub (NEW)

**Function:** `McodeFsqrtInstruction::Execute`
**Symptom:** every `$fsqrt` silently produced 0 (`sqrt(4.0)` → 0). Found by
directed test d08.
**Cause:** the Execute body was a stub; the constructor already sets opcode
`__FSQRT` and `__alu_operation__` already dispatches it to `fp_sqrt` — the
instruction class just never called it.

Before:
```cpp
void McodeFsqrtInstruction::Execute     (McodeMachine* mc)
{
	McodeRoot::Error("McodeFsqrtInstruction yet to be implemented", this);
	this->McodeInstruction::Set_Out_Arg (0, 0);
	return;
}
```

After:
```cpp
void McodeFsqrtInstruction::Execute     (McodeMachine* mc)
{
	// Was a "yet to be implemented" stub that silently produced 0 (found by
	// the fuzz1000 directed battery, 2026-07-18). The constructor already
	// sets opcode __FSQRT and __alu_operation__ already dispatches it to
	// fp_sqrt, so route through the standard ALU execute path (single
	// source operand, no immediate).
	uint64_t ovalue;
	___execute_alu_operation___ (this->McodeInstruction::Get_Opcode(),
			this->Get_Dest_Type(),
			this->Get_Src_Type(),
			this->McodeInstruction::Get_In_Arg(0),
			0, 0, 0,
			ovalue);
	this->McodeInstruction::Set_Out_Arg (0, ovalue);
	if(__global_verbose_flag)
		McodeRoot::Info(Int64ToString (this->McodeInstruction::Get_Address()) + ": Fsqrt!");
	return;
}
```

---

## 2. `McodeExecute.cpp` — float casts fell into an error branch (RE-APPLIED)

**Function:** `___execute_cast_operation___`
**Symptom:** any `$cast` with a float type on either side printed
"Float conversions in cast not supported as yet." and left `ovalues`
**uninitialized** (host-memory garbage landed in the destination register).
**Cause:** the main body (which routes through `___cast_operation___`, and
that function dispatches int↔float itself) was guarded by
`if (neither type is float)`.

Before:
```cpp
	if(!dest_type.Get_Float_Flag()
			&& !src_type.Get_Float_Flag())
	{
		... full implementation ...
	}
	else
	{
		McodeRoot::Error ("Float conversions in cast not supported as yet.", NULL);
	}
```

After (guard removed so every cast takes the main path; error branch deleted):
```cpp
	// Re-applied fix (originally 2026-07-17, lost in a source revert; see
	// cmp_wd/compiler/STATUS.md): float casts used to fall into an
	// unimplemented-error branch below that left ovalues UNINITIALIZED.
	// ___cast_operation___ dispatches int<->float itself, so every cast can
	// take the main path.
	if(1)
	{
		... full implementation (unchanged) ...
	}
```

---

## 3. `McodeOperations.cpp` — int↔float cast dispatch called the helpers SWAPPED (RE-APPLIED)

**Function:** `___cast_operation___`
**Symptom:** every int→float and float→int conversion produced garbage (each
helper read its operand as the wrong representation).
**Cause:** an int *source* was sent to `cast_float_to_int`, and a float source
with int dest to `cast_int_to_float` — exactly swapped.

Before:
```cpp
			if(!src_type.Get_Float_Flag())
				w = cast_float_to_int (src_type, dest_type, v);
			else if(!dest_type.Get_Float_Flag())
				w = cast_int_to_float (src_type, dest_type, v);
			else
				w = cast_float_to_float (src_type, dest_type, v);
```

After:
```cpp
			// Re-applied fix (originally 2026-07-17, lost in a source revert;
			// see cmp_wd/compiler/STATUS.md): the int<->float dispatch called
			// the two conversion helpers SWAPPED — an int SOURCE must go
			// int->float, a float source with int dest must go float->int.
			if(!src_type.Get_Float_Flag())
				w = cast_int_to_float (src_type, dest_type, v);
			else if(!dest_type.Get_Float_Flag())
				w = cast_float_to_int (src_type, dest_type, v);
			else
				w = cast_float_to_float (src_type, dest_type, v);
```

---

## 4. `McodeOperations.cpp` — scalar 32-bit casts misclassified as vector casts (NEW)

**Function:** `___cast_operation___`
**Symptom:** `(int)(-6.0f)` stored `0x00000000FFFFFFFA` instead of the
sign-extended `0xFFFFFFFFFFFFFFFA` (fp03 caught it). The already-correct
sign-extended result from `cast_float_to_int` was masked back to 32 bits.
**Cause:** `is_vector_cast` was derived from `in_vals.size() > 1` — but
`Break_Vector` splits a *scalar* 32-bit source into 2 entries, so scalar
32-bit casts were treated as vector casts and their results masked to the
destination lane width.

Before:
```cpp
	int is_vector_cast = (in_vals.size() > 1);
```

After:
```cpp
	// Use the TYPE flags, not in_vals.size(): a scalar 32-bit source is
	// Break_Vector'd into 2 entries, so size>1 misclassified scalar casts as
	// vector and masked the result to dest_nbits — stripping the sign
	// extension of e.g. (int)(-6.0f) stored into a 64-bit word (fp03,
	// re-found 2026-07-19).
	int is_vector_cast = (src_type.Get_Vector_Flag() && dest_type.Get_Vector_Flag());
```

---

## 5. `McodeOperations.cpp` — unsigned `$vreduce` sign-extended its lanes (NEW)

**Function:** `__vreduce_operation__`
**Symptom:** `__vreduce_vu8(0x01f2030405060708)` returned 0x14 instead of
0x114 — the 0xf2 lane was summed as −14. `__vreduce_max_vu8` picked the wrong
maximum for the same reason. Found by directed test d06.
**Cause:** a shared `r` was computed with `Sign_Extend_64` *before* the
signed/unsigned branch; the unsigned branch then used that sign-extended
value.

Before:
```cpp
			// sign extend to 64-bits.
			int64_t r = (int64_t) Sign_Extend_64(src_type.Get_Nbits()-1, ele);
			if(signed_flag)
			{
```

After:
```cpp
			// Unsigned lanes must be ZERO-extended: `ele` already arrives
			// masked to the lane width from Break_Vector. The old shared
			// `r` was sign-extended even on the unsigned path, so e.g.
			// __vreduce_vu8 summed 0xf2 as -14 (fuzz1000 d06, 2026-07-19).
			uint64_t r = ele;
			if(signed_flag)
			{
```
(The signed branch keeps its own inner
`int64_t r = (int64_t) Sign_Extend_64(...)`, which shadows the outer `r`,
so signed behavior is unchanged.)

---

## 6. `McodeFpuUtils.cpp` — `cast_int_to_float` ternary promotion bug (RE-APPLIED)

**Function:** `cast_int_to_float` (~line 437)
**Symptom:** `(float)(-3)` became `(double)(2^64 − 3)` ≈ 1.8e19 and then
saturated on the way back — every NEGATIVE int→float cast was wrong (all
positive casts were fine, which is how it originally slipped through).
**Cause:** in `cond ? (u & 0xffffffff) : (int) u` the signed arm's `int` is
promoted back to `uint64_t` (the unsigned arm's type) by C's conditional-
operator rules, so the negative value re-entered as a huge unsigned number.

Before:
```cpp
	double x = (src_type.Get_Unsigned_Flag() ?  (u & 0xffffffff) : (int) u);
```

After:
```cpp
	// Re-applied fix (originally 2026-07-18, lost in a source revert; see
	// cmp_wd/compiler/STATUS.md): the old ternary promoted the signed arm's
	// (int) back to uint64_t (the unsigned arm's type), so (float)(-3)
	// became (double)(2^64-3). Sign-extend explicitly from the source width.
	double x;
	if(src_type.Get_Unsigned_Flag())
	{
		x = (double) (u & __mmask__(snbits));
	}
	else
	{
		int64_t sv = ((int64_t) (u << (64 - snbits))) >> (64 - snbits);
		x = (double) sv;
	}
```
(Also fixes the old unsigned arm's hard-coded 32-bit mask: it now masks at
the actual source width.)

---

## Checked and found already correct (no change made)

- **`McodeFpu.hpp` `__float32_sub__` / `__float64_sub__`** — the Jul-17
  fp_sub bugs (bit-pattern subtract at 32-bit; `+` instead of `-` at 64-bit)
  are NOT present in this tree's macros; they convert through double and
  subtract correctly.
- **`__alu_operation__` unsigned scalar path** — `__uexec_64__` on raw
  uint64 operands + `CastToU64` is correct, including unsigned divide and
  logical `>>`. The older build's `Cast_Up_To_u64`/`__mmask__(64)` zeroing
  does not exist in this source — so the compiler now emits `($u64)` for
  unsigned `/`, `%`(its divide), and `>>` (the old "u64 >> is a sim limit"
  note is obsolete).
- **Backward `$call` sign-extend** (the Jul-5 regression) — present and
  correct in this source; verified indirectly by the full gate (every call
  to an earlier-defined function is a backward call).

## Related facts discovered (assembler behavior, not changed)

- `$pack` validity requires `packed_nbits % word_nbits == 0`
  (`McodeClasses.hpp:412`) and is checked **silently** — an invalid pack
  yields only "Error: there were invalid instructions" with no
  per-instruction message.
- `mcode_align` reports lane-placement failures (>4 ld/st or >1 div/sqrt
  per bundle, CTI not movable to lane 0) on stderr but **still emits the
  bundle** — never discard align/assemble stderr (the compiler's bundler
  now enforces these limits itself; see STATUS 2026-07-18).

## Process incident (why "re-applied" fixes exist)

The Jul-17/18 FP-campaign simulator fixes (items 2, 3, 6) were built and
deployed as binaries, but the source tree was left pristine. The 2026-07-19
`scons` rebuild (for item 1) regenerated all binaries from the unfixed
source, silently reverting them. The regression gate caught it (60/61 —
fp03 failing). All fixes now live in source with re-application comments,
and `testing/fuzz1000/check_engine_fixes.sh` verifies their presence.
**Rule: no toolchain fix is ever binary-only, and the full gate runs after
every rebuild.**

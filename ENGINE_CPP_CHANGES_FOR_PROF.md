# Engine C++ changes made on our side — for review / upstreaming

**Prepared 2026-07-31.** Everything below concerns
`engine_isp/assembler/src/`. Two separate things are reported:

* **Part A** — four fixes we applied to the simulator source that are **not in
  your tree**, so a rebuild from your latest master would silently drop them.
* **Part B** — one defect we believe is still present **in your latest master**.

Trees compared:

| | path | newest commit |
|---|---|---|
| yours | `prof_git_folder/AjitHpc_new/AjitHpcAccelRepo` | `eb49118`, 2026-07-30 |
| ours | `engine_new/AjitHpcAccelRepo` | binary built 2026-07-19 |

---

## Part A — four fixes present in our tree, absent from yours

`grep -c "Re-applied fix\|fuzz1000"` returns **0** in your
`McodeExecute.cpp` and `McodeOperations.cpp`, and non-zero in ours. At least one
of these was reported before and lost — our source comment records it as
*"originally 2026-07-17, lost in a source revert"* — so this is the second time
they have gone missing.

### A1. `$vreduce` on UNSIGNED types sign-extended each lane

**File:** `McodeOperations.cpp`, in the vreduce lane accumulation
**Symptom:** `__vreduce_vu8` summed a lane holding `0xf2` as **−14** instead of
242. Found by our `fuzz1000` campaign, seed d06, 2026-07-19.
**Cause:** the shared accumulator variable was sign-extended even on the
unsigned path. `ele` already arrives masked to the lane width from
`Break_Vector`, so the unsigned path must simply use it.

```c
// ours: unsigned lanes must be ZERO-extended
uint64_t r = ele;
```

This one is load-bearing for us: our compiler now *rejects* vectorizing unsigned
reductions specifically because our capability database records unsigned
`$vreduce` as broken. If the fix is upstreamed we can re-enable that path.

### A2. `is_vector_cast` was decided by operand COUNT, not by the type flags

**File:** `McodeOperations.cpp`, `___cast_operation___`
**Symptom:** a scalar 32-bit source is `Break_Vector`'d into 2 entries, so
`in_vals.size() > 1` misclassified a **scalar** cast as a vector cast and masked
the result to `dest_nbits`, stripping the sign extension of e.g.
`(int)(-6.0f)` stored into a 64-bit word. Our test `fp03`.

```c
// ours: use the TYPE flags, not in_vals.size()
int is_vector_cast = (src_type.Get_Vector_Flag() && dest_type.Get_Vector_Flag());
```

### A3. int↔float dispatch called the two conversion helpers SWAPPED

**File:** `McodeOperations.cpp`
**Symptom:** an int **source** was sent through float→int and vice versa.
Originally fixed 2026-07-17; lost in a revert; re-applied 2026-07-19.

### A4. Float casts fell into an unimplemented-error branch leaving `ovalues` UNINITIALIZED

**File:** `McodeExecute.cpp`, cast execution
**Symptom:** the destination register kept whatever happened to be in the output
buffer — nondeterministic results rather than an error.
**Fix:** `___cast_operation___` dispatches int↔float itself, so every cast can
take the main path.

> Note: your commits `f6c05f4`/`eb49118` (2026-07-30) re-indented exactly this
> region of `McodeExecute.cpp` while removing the `if(!float && !float)` guard.
> Our A4 fix touches the same lines, so **please merge rather than overwrite** —
> the two changes are compatible but will conflict textually.

---

## Part B — defect we believe is still in your latest master

### B1. `AddrIsAligned` does not check 32-byte transfers

**File:** `McodeUtils.cpp`, line ~564 (identical in both trees)

```c
int AddrIsAligned(uint32_t byte_addr, uint32_t nbytes)
{
    switch(nbytes)
    {
        case 1:  return(1);
        case 2:  return ((byte_addr & 0x1)  == 0);
        case 4:  return ((byte_addr & 0x3)  == 0);
        case 8:  return ((byte_addr & 0x7)  == 0);
        case 16: return ((byte_addr & 0xf)  == 0);
        case 32: return ((byte_addr & 0x1)  == 0);   // <-- expected 0x1f
        ...
```

Every other case masks `nbytes - 1`. The 32-byte case (`$u256`, a 4-register
group) masks `0x1`, so it only requires an **even** address: a `$u256` transfer
misaligned by 2, 4, 8 or 16 bytes passes the check silently. This is the
opposite failure mode to the one below — under-checking rather than
over-checking — so it would let bad code through rather than flag good code.

---

## Part C — what we would like from the ISA side (no bug, a question)

Our convolution vectorizer is currently **wrong on hardware** because it emits
8-byte loads at unaligned addresses. A shifted stencil window
(`out[i] = in[i] + in[i+1] + in[i+2]`) needs the packed words starting at
`base+0`, `base+1`, `base+2`, so two of every three 64-bit loads are unaligned
**by construction**.

We have confirmed this is a genuine architectural constraint rather than a
simulator artifact — a scalar load reads exactly one aligned dword:

```c
mc->Get_Accelerator()->Read_Data_Dword((base_byte_addr & 0xfffffff8), byte_mask, &mval);
```

so an 8-byte access spanning two words is not expressible in the datapath. We
are fixing this on the compiler side and are **not** asking for an ISA change.

Two things would help us, if they are cheap:

1. **Make the unaligned case fatal rather than advisory.** Today
   `McodeRoot::Error(...)` prints and execution continues, reading the
   *containing aligned word* — so the program produces plausible-looking wrong
   numbers instead of stopping. That is how this defect survived in our compiler
   for several milestones.
2. **Confirm whether an unaligned-capable load is planned.** If the accelerator
   ever gains one, shifted-window convolution becomes vectorizable without a
   shift-and-merge sequence.

---

## Part D — feature in your tree we have not adopted yet

Commits `f6c05f4` / `eb49118` (2026-07-30) add a `replicate_flag` to `$dot`:

```c
void __dot_operation__(int accumulate_flag, int replicate_flag,
                       McodeType dest_type, McodeType src_type, ...)
```

so that *replicate → dot → accumulate* is a single instruction. This is directly
useful for our GEMM and dot-product kernels. We have not adopted it yet because
it needs (a) an entry in our vector capability database and (b) a rebuilt
`mcode_run` — which is blocked on Part A being resolved, so that rebuilding does
not regress A1–A4.

---

## Summary / requested action

| item | file | status | ask |
|---|---|---|---|
| A1 unsigned `$vreduce` | `McodeOperations.cpp` | fixed in our tree only | please merge |
| A2 `is_vector_cast` | `McodeOperations.cpp` | fixed in our tree only | please merge |
| A3 int↔float swap | `McodeOperations.cpp` | fixed in our tree only; lost once already | please merge |
| A4 uninitialised cast output | `McodeExecute.cpp` | fixed in our tree only; **conflicts with your 07-30 re-indent** | please merge carefully |
| B1 `AddrIsAligned` case 32 | `McodeUtils.cpp` | present in your master | one-character fix (`0x1` → `0x1f`) |
| C1 unaligned load is advisory | `McodeExecute.cpp` | by design? | consider making it fatal |
| D1 `$dot $replicate` | yours | new feature | we will adopt after A is merged |

We are happy to send Part A as a patch against your master in whatever form is
easiest.

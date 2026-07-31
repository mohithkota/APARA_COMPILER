# R6.3 — Aligned Sliding Window Vector Lowering

**Outcome: STOPPED, with the blocking evidence documented. No lowering shipped.**

The milestone's own gate — *"inspect the APARA ISA … if they do not exist, stop
and document"* — is passed: the primitives **do** exist and the reconstruction is
implementable. It was implemented. It was then blocked by a **different**
discovery: the vector differential oracle that every vector kernel must pass
orders vector lanes **backwards relative to the hardware**. That discrepancy is
invisible to every kernel shipped so far and fatal to any order-dependent
lowering, including this one.

The working tree has been **reverted to `9cf4d57`**, which verifies **38/38** on
the simulator. Nothing unvalidated was left in the compiler.

---

## 1. Motivation

R6.2C established a fully correct vector compiler by declining convolution
kernels whose taps (`in[i+1]`, `in[i+2]`) require unaligned packed loads. The
rejection is correct; the lowering is the limiting factor. Cost measured in
R6.2C: 8 convolution/2-D kernels declined, `conv3` simulator ticks 5854 → 9895
(+69%).

## 2. ISA alignment constraints

Unchanged and absolute:

* `AddrIsAligned(addr, 8)` requires `(addr & 7) == 0` (`McodeUtils.cpp`);
* a scalar load performs exactly one `Read_Data_Dword(base & 0xfffffff8, …)`, so
  an 8-byte access spanning two words is not expressible in the datapath;
* on violation the simulator prints an error **and continues**, reading the
  containing word — plausible wrong numbers rather than a stop.

## 3. Do the required primitives exist?  — YES

| primitive | status |
|---|---|
| `$slice <rd> hindex lowindex <rs2>` | exists (`isa.txt` line 526) — `rd := rs2[hindex downto lindex]` |
| `$pack <total-nbits> rd <word-nbits> rs2` | exists (`isa.txt` line 501) |
| `<<`, `>>`, `|` on a 64-bit register | exist; `codegen.py` already emits them, `IRBinOp` already represents them |

A funnel shift built from `<<`/`>>`/`|` is simpler than `$slice`+`$pack` and uses
only instructions the backend already emits, so **no new IR node and no
pseudo-instruction is required**. The milestone's stop-condition does not apply.

## 4. Window reconstruction algorithm (derived, not guessed)

Two facts fix the direction of the shift, and both were read out of the
implementation rather than assumed:

* **DMEM byte order** — byte `b` of a word occupies bits `[63-8b : 56-8b]`
  (`compiler.py build_data_map`, derived from the engine's store path). Element 0
  of a packed array is therefore in the **most significant** byte.
* **Lane order** — `Break_Vector` (`McodeUtils.cpp`) takes lane `I` from
  `vec >> ((nfields-(I+1))*nbits)`, i.e. **lane 0 is the most significant** field.

The two agree, so for a window starting `s` bytes into a word:

```
    W0 = load [base + aligned_off]          both aligned by construction
    W1 = load [base + aligned_off + 8]
    window = (W0 << 8*s) | (W1 >>ᵤ (64 - 8*s))
```

`W0 << 8s` brings element `base+s` into the top lane; the top `s` bytes of `W1`
fill the bottom. The `>>` must be **unsigned** (logical) or the sign bit smears
into the window. The second load may read up to 7 bytes past the last element
the kernel needs; those bytes are shifted out and never used, and DMEM is flat
and readable, so the over-read is harmless.

## 5. Reuse of existing infrastructure

The implementation added no new client and no convolution-specific backend:

| component | use |
|---|---|
| `vector_affine` | `resolve_offset(...).const_off % 8` gives the shift; `sym_div` proves the symbolic part word-aligned (both added in R6.2C) |
| `expression_tree` | `ArrayRef` gained one field, `word_shift` |
| `vector_compact_loop` | one new shared emitter, `packed_window_load_at` |
| `vector_elementwise_lowering` | the three existing packed-load sites call it |
| `vector_legality` | the R6.2C gate relaxed for **loads** only — a misaligned load is legal iff its misalignment is a compile-time constant; a misaligned **store** is never reconstructable and still rejects |
| `vector_pipeline` | untouched |

Every shifted contiguous access the legality framework accepts flows through this
path; nothing is convolution-specific.

## 6. Result of the implementation

Recognition and planning worked. For `out[i] = in[i]+in[i+1]+in[i+2]` the plan
carried exactly the right shifts:

```
ArrayRef slot -72  word_shift 0   const_off 0  sym_div 0
ArrayRef slot -72  word_shift 1   const_off 1  sym_div 0
ArrayRef slot -72  word_shift 2   const_off 2  sym_div 0
```

The kernel then **rolled back at the pipeline's differential gate**:

```
vectorized: 0   rolled_back: 1   reason: differential:mismatch
```

## 7. Why it rolled back — the blocking discovery

`vector_validation`, the packed differential oracle that gates **every** vector
kernel, models lanes in the opposite order to the hardware:

```
VALIDATOR   vector_validation._lane(packed, i, bits, mask):
                return (packed >> (i * bits)) & mask          <- lane 0 = LEAST significant

HARDWARE    McodeUtils.cpp Break_Vector:
                v = mask & (vec >> ((nfields-(I+1))*nbits));  <- lane 0 = MOST  significant
```

**Every kernel shipped so far is order-independent**, which is why this has never
been observable: elementwise, AXPY and expression kernels apply the same
operation to every lane, and `$dot`/`$vreduce` sum across lanes. Reversing the
lane numbering changes nothing for any of them. A sliding window is the first
**order-dependent** lowering, and under the validator's model a left shift moves
lanes the wrong way — so a reconstruction that is correct on hardware is reported
as a mismatch, and the pipeline correctly refuses to commit code it cannot
validate.

The gate behaved exactly as designed. The model behind it is what is wrong.

## 8. Simulator verification

Not reached for the recovered kernels: the pipeline never committed them, so
there was nothing to verify. The reverted tree was re-verified after the revert:

```
38/38 programs verified — RESULT: PASS
```

## 9. Recovered kernels / performance

**None.** No kernel was recovered and no performance measurement is reported,
because reporting numbers for code that never shipped would be meaningless.
R6.2C's measured cost stands unchanged: 8 kernels declined, `conv3` ticks
5854 → 9895.

## 10. Remaining unsupported cases, and what R6.3 needs

To land this lowering, in order:

1. **Resolve the lane-order discrepancy in `vector_validation`.** Determine
   authoritatively whether `Break_Vector`'s MSB-first order is the hardware
   contract (the evidence says yes: it agrees with the DMEM byte order the
   compiler already relies on), then correct `_lane`/`_pack_lanes` and
   **re-validate every existing vector kernel against the simulator** — the
   oracle is the gate for all of them, so changing it is a correctness-critical
   act in its own right, not a prerequisite to be rushed.
2. Re-apply the window lowering (this milestone's diff is small and localized:
   one field on `ArrayRef`, one emitter, three call sites, one legality
   relaxation).
3. Verify the recovered kernels on the simulator, then measure.

Still out of scope even then:

* **misaligned stores** — never reconstructable (a read-modify-write of two
  words), so any kernel storing mid-word stays declined;
* **column-strided accesses** — rejected by `vector_affine` as before;
* **runtime-variable misalignment** — the shift must be a compile-time constant;
  an invariant with an unproven symbolic part stays declined;
* **2-D stencils** inherit all of the above; their row bases are already proved
  aligned (`sym_div = 32`), so only the inner tap shift matters.

---

## 11. Reproduction

```sh
cd compiler
python3 -m verification                       # 38/38 at the reverted HEAD
# the lane-order discrepancy, side by side:
sed -n '38,40p' vector_validation.py
sed -n '522,533p' <engine>/engine_isp/assembler/src/McodeUtils.cpp
```

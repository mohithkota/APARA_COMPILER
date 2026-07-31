# R6.2D — Canonical Vector Lane Ordering Validation

**Result: the hardware is MSB-first, established by experiment on the simulator.
The validation oracle was wrong and has been corrected. No lowering was
committed.**

The correction is behaviour-neutral for every kernel that exists today — 38/38
simulator verification, 124-program corpus byte-identical, zero tick changes —
because the oracle's error was self-cancelling for lane-order-independent work.
The R6.3 recovery check then showed the lane order was **not the only** blocker.

---

## 1. Background

R6.3 derived an aligned sliding-window lowering and implemented it. It rolled
back at the pipeline's differential gate, and the investigation pointed at a
disagreement between the validation oracle and the APARA implementation over
vector lane ordering. R6.3 stopped there rather than change a
correctness-critical oracle on the strength of source reading.

## 2. Observed disagreement

| source | ordering |
|---|---|
| `compiler.py build_data_map` | byte `b` at bits `[63-8b : 56-8b]` → **MSB-first** |
| `McodeUtils.cpp Break_Vector` | lane `I` = `vec >> ((nfields-(I+1))*nbits)` → **MSB-first** |
| `vector_validation._lane` | lane `i` = `packed >> (i*bits)` → **LSB-first** |
| `vector_lowering.PackedVectorInterp` packed load | `packed \|= e << (i*eb*8)` → **LSB-first** |

The oracle's two halves agree with **each other** and disagree with the
implementation. That self-consistency is exactly why it was never observable:
the packed load reverses the element order, the operation is applied per lane or
summed across lanes, and the packed store reverses it back. For elementwise,
AXPY, GEMM, dot and reduction kernels the two reversals cancel and memory comes
out byte-identical.

## 3. Experimental kernel

Lane *numbering* on its own is **not observable**: it is an internal label that
appears on both operands of every ISA vector operation and cancels. `$dot` of
two vectors is `Σ aᵢbᵢ` under any numbering; `$vreduce` is a sum; `$v` is
per-lane. What *is* observable, and what the sliding-window reconstruction
actually depends on, is the **byte position of element 0 inside the 64-bit
word**.

gcc cannot be the reference here — the DMEM word layout is an APARA-specific
property with no native counterpart, so a golden comparison of a reinterpreted
64-bit value would be meaningless. The probe therefore uses only
hardware-confirmed primitives and is checked directly against DMEM
PostConditions.

**Probe 1 (load side).** DMEM word `0x80` is preloaded with
`0x0102030405060708`, then:

```
$ld ($i8)  $r2 [$r1 + 0]     ; the byte at ADDRESS 0
$ld ($i64) $r3 [$r1 + 0]     ; the whole word
$slice     $r4 63 56 $r3     ; its most  significant byte
$slice     $r5  7  0 $r3     ; its least significant byte
```

**Probe 2 (store side).** Write `0xFF` to byte **address 3**, then read the whole
word back.

## 4. Simulator evidence

```
Probe 1
  Info: PostCondition Mem[0x81] = 0x1    <- $ld ($i8) of byte address 0
  Info: PostCondition Mem[0x82] = 0x1    <- $slice 63 56  (most  significant)
  Info: PostCondition Mem[0x83] = 0x8    <- $slice  7  0  (least significant)

Probe 2
  Info: PostCondition Mem[0x81] = 0x10203ff05060708
```

Byte address 0 holds `0x01`, and the most significant byte of the word also
holds `0x01`, while the least significant holds `0x08`. Writing byte address 3
lands at bits `[39:32]`. **Byte address 0 is the most significant byte:
MSB-first, on both the load and the store path.**

## 5. Oracle comparison

| | ordering | agrees with hardware? |
|---|---|---|
| simulator (measured above) | MSB-first | — |
| `Break_Vector` | MSB-first | **yes** |
| `build_data_map` | MSB-first | **yes** |
| `vector_validation._lane` | LSB-first | **no** |
| `PackedVectorInterp` load/store | LSB-first | **no** |

`Break_Vector` is correct, so per the milestone's rule the **oracle** is what
changes, and only the oracle.

## 6. Root cause

`PackedVectorInterp` gathers `lanes` elements from their individual byte
addresses into one register with `packed |= e << (i*eb*8)`, placing element 0 in
the **low** byte — the mirror image of the DMEM layout the compiler itself
assumes and the hardware implements. `_lane` and the `_valu` output assembly are
LSB-first to match, so the model is internally coherent and externally mirrored.

Consequences:

* **lane-order-independent lowerings** (everything shipped to date) are validated
  correctly — the mirror cancels;
* **any order-dependent lowering** is validated incorrectly. A sliding window is
  the first such lowering, and its correct-on-hardware reconstruction was
  reported as a mismatch.

## 7. Oracle modifications

Three edits, all in the oracle, all MSB-first:

```python
# vector_validation._lane
-    return (packed >> (i * bits)) & mask
+    return (packed >> (64 - (i + 1) * bits)) & mask

# vector_validation._valu  (output assembly, the inverse of _lane)
-    out |= (r & mask) << (i * bits)
+    out |= (r & mask) << (64 - (i + 1) * bits)

# vector_lowering.PackedVectorInterp  (packed load, and the symmetric store)
-    packed |= e << (i * eb * 8)
+    packed |= e << (64 - (i + 1) * eb * 8)
```

`Break_Vector` was **not** touched. No lowering, legality, scheduler, bundler or
memory-dependence code was touched.

Sanity check after the change, on the word from probe 1:

```
_lane(0x0102030405060708, 0) = 0x1      (byte address 0, the MSB)
_lane(0x0102030405060708, 7) = 0x8
```

## 8. Regression results

| check | result |
|---|---|
| verification harness (simulator, 38 programs) | **38/38 PASS**, negative controls still reject |
| ticks vs R6.2C | **NONE changed** |
| vectorization decisions vs R6.2C | **NONE changed** |
| 124-program corpus, bundled mcode hashes | **0 changed** (byte-identical) |
| `pipeline_crosscheck` | **PASS 124/124** |
| unit suites (all 15 `_r*_test.py`) | **all pass** |

This is the expected outcome and it is the proof that the old oracle's error was
self-cancelling: correcting a mirror that was applied twice changes nothing.

**A caveat on the R5 evaluation suite.** Its CSV output differs between runs
**with identical code**, so it cannot be used as an A/B regression instrument.
Verified as pre-existing: two consecutive runs at `9cf4d57` (without the oracle
fix) also differ. R5.0 recorded that "two consecutive evaluation runs produce
identical numbers"; that no longer holds, most plausibly because R6.2's
disambiguation made R4.2.5's compact-vs-unrolled size probe sensitive to
module-global counter state that is not reset between benchmarks in one process.
This is **not** caused by R6.2D and is recorded as separate outstanding work; the
deterministic instruments above carry the regression claim instead.

## 9. Temporary R6.3 validation

The reverted R6.3 lowering was re-applied temporarily, tested, and reverted
again. Nothing from it is committed.

| | before R6.2D | after R6.2D |
|---|---|---|
| conv 3-tap accepted by the pipeline | rolled back, `differential:mismatch` | **committed, `ok`** |
| unaligned loads in the emitted code | — | **0** |
| simulator result | not reached | **FAIL** — `Mem[0x81] = 0x0, expected 0xc` |

So the lane order **was** a real blocker and is now gone: the oracle accepts the
reconstruction and the emitted code contains no illegal address. But a **second,
independent blocker** remains — the reconstructed values are wrong for some
elements. `results[0]` (`out[0]`, chunk 0) is correct while `results[1]`
(`out[11]`, chunk 1) reads 0, which points at the per-chunk advance of the
reconstructed window rather than at the funnel-shift itself.

Per this milestone's instructions the investigation stopped there. This is
documented as a **new** blocker for R6.3, distinct from the lane ordering.

## 10. Remaining work

* **R6.3's second blocker** — the reconstructed window is correct for the first
  chunk and wrong for later ones. To be diagnosed when R6.3 resumes; the symptom
  above localises it to the chunk advance.
* **Evaluation-suite nondeterminism** — pre-existing, unrelated to lane order,
  but it removes an A/B instrument the project used to rely on and should be
  fixed (most likely a counter reset between benchmarks).
* **The `$dot128` lo/hi register pair** was not exercised by these probes. It is
  order-dependent *across registers* rather than across lanes, and no current
  lowering emits it, but it should be probed the same way before anything does.
* **`vf32_t` lane order** is untested — floating-point vector support remains
  under construction.

---

## 11. Reproduction

```sh
# the probes (hand-written mcode, no compiler involvement by design)
cd /tmp/laneprobe
$APARA_TOOLS/mcode_align probe.mcode  > probe.aligned.mcode
$APARA_TOOLS/mcode_assemble probe.aligned.mcode > probe.obj
$APARA_TOOLS/mcode_run -p 0x0 -i probe.obj -d data.map -r probe.result

# regression
cd compiler
python3 -m verification                 # 38/38
python3 loopopt/pipeline_crosscheck.py  # 124/124
for t in _r*_test.py; do python3 $t | tail -1; done
```

# R6.3.2 — Sliding Window Optimization

**The convolution loop body now sits exactly on the derived architectural lower
bound: 14 instructions per 8 outputs = 1.750 instructions/output**, down from
3.500 at the start of this milestone. 38/38 simulator verification is maintained
throughout.

Two of the three phases required code; the third turned out to be already
achieved, and is reported as such rather than credited.

---

## 1. Guarded measurement

Twice in the previous milestone a change raised `NameError`, `vector_pipeline`
swallowed it into a scalar fallback, and the reproducer "passed" with the scalar
tick count. Every number below therefore comes from a harness that refuses to
report unless **all** of the following hold:

* the kernel actually vectorized (`stats.vectorized == 1`);
* the emitted mcode contains a `$v` operation **and** funnel-shift instructions;
* simulator verification passes against the gcc golden reference;
* the tick count **differs from the scalar baseline** (3077).

A scalar fallback now fails the measurement instead of flattering it.

## 2. Results, one phase at a time

| | instr/output | bundles/output | ticks | dyn IPB | occupancy |
|---|---|---|---|---|---|
| R6.3.2 baseline | 3.500 | 1.375 | 1830 | 0.830 | 21.5% |
| **Phase 1** — share the aligned pair at the compact site | **2.375** | **1.250** | 1846 | 0.742 | 19.8% |
| Phase 2 — load the IV once | *(already achieved, no change)* | | | | |
| **Phase 3** — emit the final affine address directly | **2.250** | **1.125** | **1798** | 0.753 | 19.9% |
| scalar reference | 13.000 | 7.000 | 3077 | — | — |
| derived lower bound | 1.750 | — | — | — | — |

Measured on the loop **body alone**, the kernel is now at **1.750
instructions/output — the bound exactly** (see §6 for why the table above reads
2.250 for the same kernel).

### Phase 1 — complete the W0/W1 sharing

R6.3 shared the aligned pair at one of the three packed-load sites. This
completes it for the **compact** path, which is the site the convolution kernels
actually use. The pair and its address computation are emitted once per
`(array, aligned word)` per chunk instead of once per tap, keyed on the
`word_index` derived from the same affine constant that already yields the shift.

`instructions/output 3.500 → 2.375 (−32%)`, dynamic instructions 1518 → 1370.

**Reported as measured, not as hoped:** whole-program ticks rose 1830 → 1846
(+0.9%) even though the body shrank. The body accounts for −16 bundles across
sixteen chunks, so the increase came from outside the steady state — most likely
the peeled remainder or the prologue, where the shared pair lengthens two live
ranges. Codegen reported no spill. Phase 3 subsequently recovered it.

### Phase 2 — load the induction variable once: already achieved

**No code was written, because the body already contained zero IV reloads.**
Verified by inspecting the emitted body rather than assumed. Before R6.3.2 there
were four `$ld ($i32)` reloads, one per cloned offset expression. Phase 1's
sharing collapsed the three clones to one, and the scalar optimizer's
loop-register promotion keeps the induction variable in `$r3` across the back
edge, so no reload survives. Claiming a phase that the previous phase had already
delivered would have been double-counting.

### Phase 3 — emit the final affine address directly

With the pair keyed on a tap that already starts at a word boundary, the
correction `off − shift` degenerates to `off − 0` — the last remnant of the
add-then-subtract pattern, and a wasted instruction on every iteration. It is now
skipped and the load addresses the offset directly.

`instructions/output 2.375 → 2.250`, `bundles/output 1.250 → 1.125`,
**ticks 1846 → 1798 (−2.6%)**, which also recovers Phase 1's regression.

## 3. Mcode diff — the steady-state body

**Before R6.3.2** (28 instructions / 8 outputs): four `$ld ($i32)` IV reloads,
four `$ld ($u64)` window loads, a duplicated tap-0 load, and an add-then-subtract
per tap.

**After** (14 instructions / 8 outputs):

```
+ $r6 ($i64) $r3 8            ; aligned_off + 8
$ld ($u64) $r8 [$r4 + $r3]    ; W0   -- shared by all three taps
$ld ($u64) $r7 [$r4 + $r6]    ; W1   -- shared by all three taps
<< $r9  ($i64) $r8 8          ; tap 1 hi
<< $r13 ($i64) $r8 16         ; tap 2 hi
>> $r10 ($u64) $r7 56         ; tap 1 lo
>> $r14 ($u64) $r7 48         ; tap 2 lo
| $r11 ($i64) $r9  $r10       ; tap 1 window
| $r15 ($i64) $r13 $r14       ; tap 2 window
$v + $r12 ($vi8) $r8  $r11    ; tap 0 IS W0 -- no load, no copy
$v + $r16 ($vi8) $r12 $r15
$st ($i64) [$r5 + $r3] $r16
+ $r3 ($i64) $r3 8            ; IV, held in a register
? ($i64) $r0 == $goto vcl_1_cond
```

This is the predicted sequence item for item: one address computation, two
aligned loads shared across all taps, six funnel-shift operations, two vector
adds, one store, and two instructions of loop overhead (the compare lives in the
condition block).

## 4. Remaining redundancy

**In the loop body: none identified.** The body matches the derived bound
instruction for instruction.

Outside the body, and not addressed here:

* **the peeled remainder and prologue** — the measured whole-kernel figure is
  2.250 rather than 1.750 because the harness kernel also initialises 136
  elements and reads results, and because the peeled tail is not shared with the
  steady state. That is where the difference between 14 and 18 instructions per
  chunk in the two measurements comes from;
* **dynamic IPB fell** 0.830 → 0.753 across the milestone. Fewer instructions in
  the same number of bundles necessarily lowers density; ticks fell, which is the
  figure that matters, but the vector body is now *sparser* and is a good
  candidate for the unrolling R6.1 identified;
* **occupancy is ~20%**, so roughly four of five issue slots in the vector body
  are still idle — a scheduling-supply problem, explicitly out of scope here.

## 5. Estimate against the 1.75 bound

| | instr/output |
|---|---|
| R6.3.2 baseline | 3.500 |
| after Phase 1 | 2.375 |
| after Phase 3 | 2.250 *(whole kernel)* / **1.750** *(loop body)* |
| derived bound | 1.750 |

**The body is at the bound.** Further gains must come from the remainder and
prologue, or from a different optimization class (unrolling, software
pipelining), not from removing redundancy inside this lowering.

## 6. Threats to validity

* The two instruction/output figures measure different things and both are
  reported. **1.750** is the steady-state loop body of the convolution kernel —
  the quantity the 1.75 bound was derived for. **2.250** is the same kernel
  measured through the guarded harness, whose program also contains a 136-element
  initialisation loop, sparse result reads and a peeled remainder. Quoting only
  the first would overstate the result.
* One kernel shape (3-tap `vi8`). `vi16` and `vi32` amortise the same overhead
  over 4 and 2 outputs, so their per-output advantage is proportionally smaller;
  the bound derivation is width-specific and was not repeated for them.
* Ticks are the simulator's, not hardware cycles.
* Phase 1's +0.9% tick regression was explained by elimination (no spill; the
  body shrank) rather than by direct attribution to a specific instruction, and
  was subsequently recovered by Phase 3.

---

## 7. Regression

| check | result |
|---|---|
| simulator verification | **38/38 PASS**, negative controls reject |
| `pipeline_crosscheck` | **PASS 124/124** |
| unit suites (all 15) | **all pass** |
| guarded assertions (vectorized / vector ops present / ticks ≠ scalar) | enforced on every measurement |

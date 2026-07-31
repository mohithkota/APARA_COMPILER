# R6.3 — Is an aligned sliding window worth pursuing?

**Answer: YES, decisively. Continue R6.3.**

And I have to withdraw my own previous warning. In R6.2E I reported that the
window form "ran 1888 ticks against 1517 for the scalar form" and recommended
settling profitability before further work. **That comparison was wrong.** It
compared whole programs at a small trip count, where a large fixed setup cost
dominates, instead of the steady-state loop. Measured properly, the vector form
is already several times faster per output element.

---

## 1. Method

Comparing whole programs is invalid here: both spend most of their time in a
136-iteration scalar initialisation loop, and the vector build carries a larger
one-off setup. The meaningful quantity is the **steady-state cost per output
element**, measured on the emitted loop body of the same kernel compiled both
ways.

Kernel: `out[i] = in[i] + in[i+1] + in[i+2]`, `vi8_t`, 128 outputs (16 chunks,
no remainder, so no peeling in either arm).

## 2. Measured steady state

| | instructions / output | bundles / output |
|---|---|---|
| scalar loop body (`fb_6`, 13 instrs, 7 bundles, 1 output) | **13.00** | **7.00** |
| sliding window body (`vcl_2_body`, 28 instrs, 11 bundles, 8 outputs) | **3.50** | **1.38** |
| **ratio** | **3.7× fewer** | **5.1× fewer** |

Corroborating whole-program measurement (scalar, four trip counts — perfectly
linear): **19.0 ticks and 26.0 instructions per output**, confirming the scalar
loop is expensive and that the slope method is sound.

## 3. Upper bound from the ISA

Best possible 3-tap `vi8` sliding window, per chunk of 8 outputs. Every operation
below is an existing APARA instruction:

| category | count | why |
|---|---|---|
| aligned loads | **2** | `W0` = elements 8c..8c+7, `W1` = 8c+8..8c+15. **Both are shared by all three taps** — no tap needs any other word |
| shift / or | **6** | tap 0 is `W0` itself (free); taps 1 and 2 each need `shl`, `shr`, `or` |
| vector ALU | **2** | two `$v +` to sum three windows |
| stores | **1** | one packed store of 8 results |
| loop overhead | **3** | IV increment, compare, branch |
| **total** | **14 per 8 outputs** | = **1.75 instructions / output** |

Bundle lower bound on an 8-wide machine with 4 memory lanes: the dependence
chain is `loads → shl/shr → or → vadd → vadd → store`, six levels, with the
branch sharing the last bundle → **≈ 6 bundles per 8 outputs = 0.75 bundles /
output**.

## 4. The comparison

| | instr / output | bundles / output | vs scalar |
|---|---|---|---|
| scalar (measured) | 13.00 | 7.00 | — |
| sliding window **as implemented today** | 3.50 | 1.38 | **3.7× / 5.1× faster** |
| sliding window **theoretical best** | 1.75 | 0.75 | **7.4× / 9.3× faster** |

The termination condition in the milestone — "if the theoretical best vector
implementation is still slower than scalar" — is not met, and not remotely. Even
the current unoptimised implementation beats scalar by 5× per output in bundles.

## 5. Where the current implementation loses against the bound

The emitted body is 28 instructions where 14 suffice. The instruction mix names
the waste exactly:

```
steady-state body, 28 instructions / 8 outputs
   +              5        $ld ($u64)     4      <- window loads: should be 2
   $ld ($i32)     4   <-   $ld ($i64)     2      <- tap-0 load: should be 1
   IV reloads:               -            2      <- add-then-subtract
   should be 1               << 2  >> 2  | 2     <- correct (3 per shifted tap)
                             $v 2                <- correct
                             $st ($i64)   2
```

Four concrete losses, all redundancy rather than anything structural:

1. **`W0`/`W1` are not shared between taps.** Each shifted tap emits its own pair
   of aligned loads, so 4 window loads appear where 2 suffice — both taps read
   exactly the same two words.
2. **The induction variable is reloaded 4 times**, once per cloned offset
   expression. `clone_offset` re-emits the loop's address computation per access
   and nothing removes the duplicates afterwards.
3. **Tap 0 is loaded twice.**
4. **Add-then-subtract:** the cloned offset computes `iv + k` and the window
   emitter immediately computes `(iv + k) - k`. Two wasted ALU operations per
   shifted tap.

Every one of these is a redundancy that CSE within the vector body would remove;
none requires redesigning the lowering. Closing them takes 3.50 → ~1.75
instructions per output, i.e. the theoretical bound.

## 6. Conclusion and what comes next

**Continue R6.3.** The profitability question is settled in its favour by a wide
margin, and the remaining engineering is (a) the correctness defect isolated in
R6.2E — sparse reads after the loop return zero — and (b) the four redundancies
above, which are pure upside and not on the correctness path.

Priority order: fix the correctness defect first (nothing else matters until the
kernel is right), then share `W0`/`W1` across taps, which alone removes a third
of the body.

## 7. Threats to validity

* The vector figures come from a lowering that currently **computes wrong values**
  for some kernels. The counts are still representative of the intended work —
  the body performs all the loads, shifts, adds and the store the correct version
  needs — but they are not a measurement of correct code, and the final numbers
  must be re-taken once the defect is fixed.
* Bundles are not ticks. The simulator's whole-program tick measurements are used
  only as corroboration; the per-output comparison is in bundles, counted on the
  same steady-state block in both arms.
* One kernel shape (3-tap `vi8`). Wider element types have fewer lanes per chunk
  and so a smaller advantage: `vi16` amortises the same overhead over 4 outputs
  and `vi32` over 2, which should be measured before claiming the result
  generalises. The ratio for `vi32` would be roughly a quarter of the `vi8`
  advantage — still favourable, but by much less.

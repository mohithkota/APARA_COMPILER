# R14.9 — Result-base loop-invariant code motion: **STOPPED**

Compiler at R14.8 (`a8fede2`). **ANALYSIS ONLY — 0 production `.py` files
changed.** Frozen tags untouched, nothing pushed.

## Answer to the final question

> "Can the result base be proven invariant across J-tiles, materialized once per
> row, and reused by R14.8's constant-offset stores without spills or
> correctness regressions?"

**No — the premise is false. STOP CONDITION 1 fires: the result base is not
loop-invariant with respect to the J-tile loop.**

## The evidence

The result address is `&results[i*N + j + t]`, so the base offset expression is

```
_t130 = load(slot -520)        i
_t132 = _t130 * 16             i*N
_t134 = load(slot -528)        j          <-- the J-tile loop's OWN induction variable
_t136 = _t132 + _t134          i*N + j
_t139 = _t136 * 8              byte offset
IRGlobalAddrOf _gsb1 = &DMEM[0x400 + _t139]
```

Measured from the IR, not assumed:

| fact | value |
|---|---|
| loop enclosing the base | `fc_9`, `is_innermost = False` |
| its `primary_iv` | **slot −528 (`j`)** |
| stack slots **written** inside that loop | −568, −560, −552, −544, −536, **−528** |
| base offset **reads** slot −528? | **yes** |
| base offset reads slot −520 (`i`)? | yes — and −520 is **not** written in the J-tile loop |

So the base depends on the J-tile loop's own induction variable. It changes on
every tile iteration by design. Phase 2's condition 1 ("the result base
expression does not depend on J") is **false**.

## Why the milestone's target structure cannot work as written

The proposal was:

```
row scope:  result_base = C[row][first_output]
J-tile 0:   stores using result_base + delta
J-tile 1:   stores using result_base + delta        <-- same delta
```

But successive tiles are `J_TILE * 8` bytes apart, and that displacement grows
with the tile index. It is an **induction variable**, not a constant, so it
cannot be folded into the store immediates the way R14.8's within-tile deltas
(0/8/16/24) are. Reusing one base with a *constant* delta across tiles would
write every tile's results to the same addresses — a wrong-answer bug.

This is a different situation from R14.3, and the distinction matters: R14.3's
premise failed because there was no inner K loop at all. Here the loop exists
and the analysis is well-formed; the expression simply is not invariant in it.

## What IS invariant, and what it is worth

Only the **row part** `i*N` is invariant in the J-tile loop (slot −520 is not
written there). Hoisting `GBASE + i*N*8` would shorten the per-tile chain from
three instructions to two.

What-if measured with the project's own bundler (`bundler.bundle_mcode`), not
estimated:

| epilogue form | instructions | **bundles** |
|---|---|---|
| current — base rebuilt per tile | 12 | **5** |
| row part hoisted | 11 | **4** |

**One bundle.** Block 12 → 11; at 64 executions that is 768 → 704 weighted
ticks, i.e. **≈1.4% of whole-program ticks** (4575 → ~4511).

Two reasons that was not pursued:

1. **It is a different transformation from the one specified.** Hoisting a
   sub-expression of the base is not "materialize the result base once per row
   and reuse it across tiles". The milestone's closing rule is explicit —
   *"do NOT invent another optimization"* when a stop condition fires.
2. **The gain is marginal** and would extend a live range across the entire
   J-tile loop, which Phase 7 requires to be spill-free. At ~1.4% that trade is
   not obviously worth taking, and it is the owner's call, not an analysis's.

## R14.8 baseline (re-measured, not reused)

16×16 vu8 J_TILE=4, verified fresh on a clean tree at `a8fede2`:

| | value |
|---|---|
| ticks | **4575** |
| ticks/output | 17.87 |
| total block bundles | **12** |
| vector bundles | 7 (31 instrs) |
| scalar epilogue bundles | **5** (13 instrs) |
| address instructions in block | 23 |
| spills | **0** |
| correctness | 256/256, 0 errors |

## Verification — nothing changed

| check | result |
|---|---|
| production `.py` changed | **0** |
| 38-program suite | **38/38 PASS** |
| metrics vs R14.8 | **0 programs differ** |
| negative controls | **3/3** |
| `pipeline_crosscheck` | **124/124**, 0 IR / 0 code / 0 tier mismatches |
| `_r14_9_test.py` | **7/7** |

`_r14_9_test.py` pins the finding structurally — that the J-tile loop writes its
own IV slot and the base's offset reads that same slot — so the premise cannot be
silently re-adopted. It also guards R14.8's three immediate-displaced result
stores against regression.

## Comparison with the hand-written kernel

The hand-written kernel keeps **one result pointer live across the whole row and
advances it by a fixed stride per row** (`+ $r29, $r29, 128` at the row latch).
That is **strength reduction on the store pointer** — an induction variable — not
loop-invariant code motion. The compiler currently rebuilds the address per tile
from `i` and `j`.

So the residual structural gap is real, but it is an **induction-variable /
strength-reduction** opportunity on the result pointer, *not* the LICM this
milestone specified. Naming it correctly matters, because the two transformations
have different legality conditions.

**Not implemented. No production file changed. R15 not started.**

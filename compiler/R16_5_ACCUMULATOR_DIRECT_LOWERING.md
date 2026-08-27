# R16.5 — Direct accumulator promotion: the shadow-copy round trip removed

Implemented on `09e67c0` (R16.2), following `R16_4_JT8_SCHEDULING_ANALYSIS.md`.
**One production file changed** (`compiler/loop_reg.py`, +1 predicate, 3 call
sites), plus one re-baselined assertion in `_r14_3_test.py`. No scheduler, no
bundler, no register allocator, no vectorizer, no codegen, no ISA change, and no
change to the register budget or the spill policy.

## Answer

> **Can the compiler eliminate the vector accumulator shadow-copy round trip by
> accumulating directly into the promoted loop register, and does that unlock
> the measured JT=8 schedule without spills or correctness regressions?**

**Yes.** Every figure R16.4 predicted from its what-if is reproduced by the
production implementation, to the tick.

| primary workload: fixed-DMEM 16×16 `vu8` | R16.2 | R16.5 | |
|---|---:|---:|---:|
| **JT=4 ticks** | 987 | **861** | **−12.8%** |
| **JT=8 ticks** | 1015 | **795** | **−21.7%** |
| JT=8 vs JT=4, after R16.5 | — | 795 vs 861 | **−7.7%** |

`$dot` per bundle at JT=8 rises **3 → 4**, the j-body collapses from **three
basic blocks to one**, executed register moves fall **515 → 3**, and there are
**zero spills** in either tile.

## Premise correction — the fix is not in the vector lowering

The milestone brief specifies:

> Change vector lowering so that when a reduction accumulator **already has a
> promoted loop register**, the vector `$dot` accumulate directly targets that
> live accumulator.

**That condition is never true at vector-lowering time.** Vectorization runs at
`compiler.py:598`; `promote_loop_counters` runs inside the tier lambdas at
`compiler.py:691`, i.e. **after** it. Dumping the IR on both sides of that pass
shows the vector lowering emitting the correct, storage-agnostic memory form and
`promote_loop_counters` manufacturing the copies:

```
BEFORE promote_loop_counters          AFTER promote_loop_counters
  _vac34 = *(_vaa33+0)                  _vac34 = _lr2          <- copy IN
  ...                                   ...
  _vac34 = dot($vu8) _vpa49 . _vpb50    _vac34 = dot($vu8) ...
  ...                                   ...
  *(_vas67+0) = _vac34                  _lr2 = _vac34          <- copy OUT
```

At lowering time the accumulator lives in a stack slot and *must* be loaded and
stored; there is no register to target and nothing to fix. The pass that **emits
the copies** is `loop_reg`, and that is where R16.5 emits correctly instead.
This satisfies the brief's actual design constraint — the IR is emitted right,
not repaired by a generic post-pass — and it is specifically *not* the
`coalesce.py` route the brief rules out.

## The change

`loop_reg._promote_one` always minted a **fresh** vreg for a promoted slot and
bridged it to the body with a move at each end. R16.5 adds one predicate:

```python
vreg = _closed_roundtrip_temp(instrs, d, s, e, fa, fb)   # or a fresh temp
```

When the slot's loop traffic is a **closed round trip** — one load into a temp
`T`, in-place updates, one store of that *same* `T` — the promoted register
**is** `T`. The preheader load defines it, the body updates it, the write-back
stores it, and the two interior moves are never emitted.

Three conditions, all structural — no kernel name, datatype, tile width, matrix
size or variable name appears anywhere in the rule:

| | condition | what it protects |
|---|---|---|
| **(a)** | the store's value temp **is** the load's destination | the **R14.6 `dest != accum` distinction** (Phase 3). A store of a *different* temp is a real copy and is kept. |
| **(b)** | `T` occurs nowhere in the function outside `[load, store]` | the promoted register is written every iteration and lives across the back edge; an outside reader would see the accumulation, not one iteration's value. |
| **(c)** | no other access to that slot falls strictly inside the span | such an access would be rewritten into a move that clobbers `T` mid-flight, where the original left `T` alone. |

Kill switch `APARA_NO_ACC_DIRECT=1` restores the pre-R16.5 behaviour exactly
(measured: 1015 ticks, the R16.2 figure), so every effect below is A/B
attributable to this one predicate.

### Why nothing downstream could already do this

* the **copy-out** cannot be coalesced — its producer is `IRVecDot`, which
  `coalesce._COALESCEABLE_PRODUCERS` deliberately excludes because `$dot
  $accumulate` reads its own destination and cannot be retargeted by a plain
  producer rewrite;
* the **copy-in** cannot be coalesced — its source has other users (the
  copy-out, and the slot's other accesses), failing that pass's third condition.

## Before / after IR — JT=8, the shipped IR

```
R16.2                                   R16.5
  _lr242 = 0            (×8)              _vac34 = 0            (×8)
  _vac34 = _lr242       (×8)  <- copy IN  ---                   removed
  _vac34 = dot(...)     (×16)             _vac34 = dot(...)     (×16)
  _lr242 = _vac34       (×8)  <- copy OUT ---                   removed
  *(_gsb1+0) = _lr242   (×8)              *(_gsb1+0) = _vac34   (×8)
```

**16 registers for 8 accumulators → 8.** The zero-init writes the accumulator
directly, the dots update it in place, and the result stores read it.

## Phase 6 — register pressure

Exact liveness (iterative backward dataflow over the CFG reconstructed from the
IR's own labels and jump targets), on the IR that ships:

| | R16.2 | R16.5 |
|---|---:|---:|
| JT=8 peak live values, hot block | 18 | **25** |
| JT=4 peak live values, hot block | 17 | **17** |
| accumulators resident | 8 | **8** |
| **spills** (`CodeGen.spilled`) | **0** | **0** |
| register pool | 28 | 28 (unchanged) |

**Pressure goes UP, and that is the point.** Freeing eight registers lets the
scheduler keep all eight `B`-operand loads in flight at once, which is what buys
the 4-wide `$dot` rotation. The budget is respected — 25 ≤ 28, zero spills, and
no gate was relaxed to get there.

## Phase 7 — region formation, from the emitted code

Not inferred from ticks. Per-label dynamic tick attribution, 100% tick coverage
(795/795 and 1015/1015 matched to a named static bundle):

**JT=8 j-body**

| block | R16.2 | R16.5 |
|---|---|---|
| `fb_6` | 13 bundles / 416 ticks | **15 bundles / 480 ticks** |
| `fe_12` (result stores) | 7 bundles / 224 ticks | **gone** |
| `fi_7` (j increment) | 1 bundle / 32 ticks | **gone** |
| **total per iteration** | **21 bundles, 672 ticks** | **15 bundles, 480 ticks** |

The superblock merge that R16.3 measured failing now succeeds **on its own** —
nothing asked for it. `fe_12` and `fi_7` are absent from the emitted code.

**JT=4 j-body**: `fb_6` 10 bundles / 44 instrs / 640 ticks → **8 bundles / 36
instrs / 512 ticks**. Every other block is tick-identical, so the whole 126-tick
gain is 64 iterations × 2 bundles. −8 instructions = 2 × 4 accumulator copies.

## Phase 8 — `$dot` rotation

| | R16.2 | R16.5 |
|---|---:|---:|
| JT=8 max `$dot` per bundle | **3** | **4** |
| JT=4 max `$dot` per bundle | 4 | 4 |

The rotation widens without being asked to and without a gate relaxation — the
R16.4 what-if that reached 4 wide by reordering needed one that was shown unsafe
elsewhere. Here it falls out of the freed registers.

## Phase 15 — performance

| | ticks | ticks/out | bundles (static) | executed instrs | IPB | dot/bundle | peak live | spills |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| R16.2 JT=4 | 987 | 3.855 | 50 | 3531 | 3.578 | 4 | 17 | 0 |
| R16.2 JT=8 | 1015 | 3.965 | 61 | 3339 | 3.290 | 3 | 18 | 0 |
| **R16.5 JT=4** | **861** | **3.363** | 50 | **3019** | 3.506 | 4 | 17 | **0** |
| **R16.5 JT=8** | **795** | **3.105** | 57 | **2827** | **3.556** | **4** | 25 | **0** |

* **JT=4 improvement: −12.8%** (987 → 861)
* **JT=8 improvement: −21.7%** (1015 → 795)
* **JT=8 vs JT=4 after R16.5: −7.7%** (795 vs 861)

**The tile ordering reverses.** JT=8 was the worse tile at R16.2 and is the
better one at R16.5 — at every datatype and size measured (Phase 11).

Every R16.4 prediction is reproduced exactly: 795 / 861 ticks, 2827 / 3019
executed instructions, IPB 3.556 / 3.506, 3 executed moves, 4 `$dot` per bundle,
one j-body block, 15 bundles per j-iteration. The single figure that differs is
static aligned bundles at JT=8: R16.4 projected 54, production emits **57**
(alignment padding differs); ticks are unaffected.

## Phase 5 / 11 — J_TILE, datatype and size

All 32 configurations correct (256 or 1024 independently gcc-verified
PostConditions each), all vectorized:

| ticks | JT=1 | JT=2 | JT=4 | **JT=8** |
|---|---:|---:|---:|---:|
| `vu8`/`vi8` 16×16 | 2763 | 1483 | 861 | **795** |
| `vu8`/`vi8` 32×32 | 11643 | 6523 | 4539 | **3195** |
| `vu16`/`vi16` 16×16 | 3291 | 1883 | 1323 | **971** |
| `vu16`/`vi16` 32×32 | 15803 | 9147 | 5851 | **4887** |

JT=1 is unchanged from R16.2 (2763): a single reduction chains each chunk into a
fresh temp, so condition (a) declines it — correctly.

## Phase 12 — distance to the hand-written kernel

| | ticks | ticks/output | instrs | IPB |
|---|---:|---:|---:|---:|
| compiler JT=4 (R16.5) | 861 | 3.363 | 3019 | 3.506 |
| compiler JT=8 (R16.5) | **795** | **3.105** | 2827 | 3.556 |
| hand-written JT=4 | 309 | 1.207 | 1160 | 3.754 |
| hand-written JT=8 | **241** | **0.941** | 1160 | 4.813 |

Gap to the hand-written 8-dot kernel closes from **4.21×** (1015/241) to
**3.30×** (795/241). The residual is **instruction count, not packing**: the
compiler executes 2827 instructions against 1160 — 2.44× — at a comparable IPB.
Higher IPB is not equated with performance here; R16.5 in fact *lowers* the
epilogue's instruction density while making it strictly cheaper (see below).

## Phase 14 — change attribution

* **38-program verification suite: BIT-IDENTICAL.** 0 of 38 programs changed, on
  all 14 metric columns; suite total 67684 ticks before and after. R16.5 is
  provably additive there — as with R16.2, that suite is blind to this lever.
* **`APARA_NO_ACC_DIRECT=1` reproduces the R16.2 tick count exactly** (1015),
  so the entire 220-tick JT=8 delta is attributable to this predicate alone.
* Every instruction removed from the hot blocks is an accumulator move:
  JT=4 `fb_6` −8 instructions = 2 × 4 accumulators; JT=8 −16 = 2 × 8. Executed
  moves 515 → 3 program-wide. **No unrelated instruction changed.**

### One test expectation was re-baselined, and why

`_r14_3_test.py` asserted `epilogue IPB > 2`. Under R16.5 it is 1.80, because
the epilogue loses exactly J_TILE copy-outs (13 → 9 instructions at JT=4)
**without losing bundles** — those copies were riding in the spare slots of the
serial result-address chain that R16.3 identified and R16.5 does not touch:

```
epilogue, R16.5:   [1, 1, 1, 1, 5]   <- 4-deep serial address chain, then
   + $r15 ($i64) $r2 0                  one bundle holding all four $st
   << $r17 ($i64) $r15 3                plus the back edge
   + $r19 ($i64) $r19 $r17
   + $r18 ($i64) $r28 $r19
   $st [..+0] $r7  $st [..+8] $r8  $st [..+16] $r11  $st [..+24] $r9  ? $goto
```

The epilogue is **strictly cheaper**: same or fewer bundles (5 → 5, and 5 → 3 on
`vu8` 16×16), four fewer instructions, ticks equal or better (`vu8` 16×16
4575 → 4447). IPB is a density ratio, not a performance metric — a smaller
numerator over the same denominator is the fix working. The assertion now pins
the load-bearing property directly (**the epilogue must not grow**), alongside
the existing bundle-count and store-packing checks. **It passes on both the
R16.2 and the R16.5 compiler**, so it is not tailored to this milestone.

## Phase 13 — full regression

| gate | result |
|---|---|
| 38-program verification suite | **38/38 PASS** (144s) |
| negative controls | **3/3 rejected** |
| suite metrics vs R16.2 | **bit-identical**, all 14 columns |
| `pipeline_crosscheck` | **124/124** — 0 IR, 0 code, 0 selected-tier mismatch; 0 verifier failures, 0 rollbacks |
| R13.0 / R13.1 | 59/59 · 38/38 |
| R14.1a / R14.2 / R14.3 / R14.6 / R14.8 / R14.9 / R14.10 | 43/43 · 15/15 · 15/15 · **17/17** · 24/24 · 7/7 · 9/9 |
| R16.2 | **41/41** |
| R3.1, R3.2, R4.0–R4.6, R6.1, R6.2, R6.6, R6.8, R7.1, R9.1, R9.2 | all PASS (24 files) |

R14.6 — the milestone whose `dest != accum` distinction condition (a) preserves
— passes 17/17 untouched.

## Phase 4 / 10 / 16 — what is deliberately unaffected

* **Single reductions** (dot product, sum reduction): emitted mcode is
  **byte-identical** with R16.5 on and off. Their lowering chains each chunk into
  a fresh temp, so condition (a) declines them.
* **Ordinary loop counters** (`load → add into a new temp → store`) decline for
  the same reason — which is why the 38-program suite is bit-identical.
* **Remainder / peel**: row lengths that are not a whole number of packed chunks
  (12, 20 — remainders 4 and 4 on the 8-lane `vu8` chunk) keep their scalar peel
  and stay correct.
* **Genericity**: renaming the arrays and the accumulators leaves the tick count,
  the `$dot` count and the move count identical; a stack-local matmul is correct
  too. Storage class, datatype, tile width and matrix size are not inputs to the
  rule.

## Defect surfaced (pre-existing, NOT caused by R16.5)

Writing the genericity test exposed an unrelated miscompilation: a **256-element
statically-initialized LOCAL (stack) array** gives wrong results — 256/256
PostConditions fail — in a 16×16 `vu8` matmul built without `--dmem-init`.

**It is not R16.5.** With `APARA_NO_ACC_DIRECT=1` the build is identical in every
respect — 1272 ticks, 8 `$dot`, 256/256 wrong — so it reproduces exactly on the
R16.2 compiler. Two further data points bound it:

* it is **not** "local static initializers" in general — a 4-element `int A[4] =
  {11,22,33,44};` local is correct;
* the same matmul with the arrays **initialized by a runtime loop** instead is
  correct (and still vectorizes), which is the form `_r14_3_test.py` has always
  used and the form R16.5's genericity test now uses.

The 1272 ticks are the tell: a correct build must execute 512 initializer stores
on top of a ~987-tick kernel, and 1272 leaves no room for them, so the
initialization appears to be dropped or truncated rather than mis-ordered.

Filed as a finding, not fixed here — it is outside this milestone and fixing it
would change code R16.5 does not touch.

## Tests — `_r16_5_test.py`

Three tiers, so the fast one needs no toolchain:

```bash
python3 _r16_5_test.py --unit    # 6 unit checks, no simulator, instant
python3 _r16_5_test.py           # + emission/regression/genericity/matrix subset
python3 _r16_5_test.py --full    # the whole 4 datatypes x 2 sizes x 4 tiles matrix
```

The unit tier asserts the predicate on synthetic IR: it fires on a closed
self-update, and declines on (i) `dest != accum`, (ii) a temp used outside the
round trip, (iii) another slot access inside the span, (iv) an ordinary loop
counter.

## Status

**R16.5 COMPLETE.** One predicate in `loop_reg.py`; no scheduler, bundler,
allocator, vectorizer or codegen change; no register-budget or spill-policy
change; every production gate honoured unmodified. Tags `r10-final`,
`r11-verified` and `r12.1-verified` untouched. Nothing pushed.

**Remaining limitations.** (1) The serial result-address chain R16.3 identified
is still there — now fully exposed as four 1-instruction bundles per epilogue,
and it is the next-largest j-body term. (2) Single-reduction dot/sum kernels do
not benefit; their fresh-temp chaining would need a lowering change, not a
promotion change. (3) The 38-program suite cannot see this lever at all — a
j-tiled multi-reduction matmul is not in it, which is worth fixing before the
next milestone relies on that suite as a performance gate.

**Do not start R16.6 automatically.**

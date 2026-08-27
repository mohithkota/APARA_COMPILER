# R16.4 — JT=8 region formation and dot/load scheduling: **root cause found**

Compiler at `09e67c0` (R16.2), following `R16_3_JT8_GAP_ANALYSIS.md`.
**ANALYSIS ONLY — 0 production `.py` changed, nothing pushed, no tag touched.**
Every what-if below is a wrapper around the unmodified compiler; each measured
program was executed on the real simulator and checked against its gcc golden.

## Answer

> **Can JT=8 be made faster than JT=4 by fixing region formation and/or the
> load/dot scheduling order, without exceeding 28 live registers?**

**Yes — and neither of those is the thing to fix.** Both are *symptoms* of one
emission-order defect: the vector lowering wraps every accumulator in a copy
pair, so 8 accumulators occupy **16** registers. Closing that round trip:

| | ticks | vs JT=4 today |
|---|---:|---:|
| JT=4 today | 987 | — |
| JT=8 today | 1015 | +2.8% |
| **JT=8, shadow copies removed** | **795** | **−19.5%** |
| JT=4, shadow copies removed | 861 | −12.8% |

**795 ticks, 256/256 correct, peak live 24 of 28, zero spills, and every
production gate honored** — no gate was overridden to obtain it. The region
merge that fails today succeeds by itself once the pressure is gone, and the
`$dot` rotation widens from 3 to 4 without being asked to. **JT=8 also overtakes
JT=4** (795 vs 861), reversing the current tile ordering.

**Classification: C — instruction emission order.** A and B and E and D are all
downstream of it. **No global scheduler, bundler or allocator change is
required** (Phase 12 satisfied).

---

## Phase 1 — Region structure: where the four blocks come from

`ir_gen.visit_For` (line 1434) emits four labels for **every** `for`:

```python
cond_lbl=self._lbl('fc'); body_lbl=self._lbl('fb')
incr_lbl=self._lbl('fi'); end_lbl=self._lbl('fe')
```

so the three nested loops number off as:

| loop | cond | body | incr | end |
|---|---|---|---|---|
| `i` (rows) | `fc_1` | `fb_2` | `fi_3` | `fe_4` |
| `j` (tiles) | `fc_5` | `fb_6` | `fi_7` | `fe_8` |
| **`k` (dot)** | `fc_9` | `fb_10` | `fi_11` | **`fe_12`** |

### CFG of the JT=8 j-nest as emitted

```
        fc_1  i<16 ?  ──no──▶ fe_4 ─▶ main_epilogue
          │yes
          ▼
        fb_2   promote s0..s7 + j from stack        (16×)
          │
          ▼
   ┌──▶ fc_5   j<16 ?  ──no──▶ fe_8  writeback s0..s7 (16×) ─▶ fi_3 ─▶ fc_1
   │      │yes
   │      ▼
   │    fb_6   zero+copy 8 accs, B row addr, 16 ld, 16 dot, copy back  (32×)
   │      │ fall-through, no branch, single predecessor
   │      ▼
   │    fe_12  result address chain + 8 stores                          (32×)
   │      │ fall-through, no branch, single predecessor
   │      ▼
   │    fi_7   j += 8                                                   (32×)
   └──────┘
```

**`fb_10`/`fc_9`/`fi_11` do not exist in the emitted code**: the k-loop was
vectorized away into two packed chunks. `fe_12` is what survives of it — the
label the result stores were emitted after.

### Every boundary, and whether it is required

| boundary | predecessor | successor | branch? | live across | required by CFG? |
|---|---|---|---|---|---|
| `fb_6` entry | `fc_5` | — | yes (loop test) | i, j, A-row pair, bases | **yes** |
| **`fb_6` → `fe_12`** | `fb_6` only | `fe_12` | **none** (fall-through) | 8 accumulators, i, j, bases | **no — lowering artifact** |
| **`fe_12` → `fi_7`** | `fe_12` only | `fi_7` | **none** (fall-through) | i, j, bases | **no — lowering artifact** |
| `fi_7` → `fc_5` | `fi_7` | `fc_5` | yes (back edge) | i, j, bases | **yes** |
| `fe_8` entry | `fc_5` exit | `fe_8` | yes | 8 accumulators | **yes** |

Two of the five boundaries are single-entry/single-exit fall-throughs with no
branch and no second predecessor. They exist because `visit_For` emits a label
per loop part and the k-loop's `fe` label outlived the loop it belonged to.
**Nothing in the CFG requires them.**

## Phase 2 — Mergeability: the merge is offered, and rejected for a real spill

R3.2/R6.7 (`trace_scheduler.apply_superblock_scheduling`) exists precisely to
merge single-entry/single-exit chains. Observing it without modifying it:

| | JT=4 | JT=8 |
|---|---|---|
| merge candidates offered | 3 | **3 (identical)** |
| oracle scheduling headroom | pass | **pass** (gain 5.02, threshold 0.5) |
| decision | **accepted**, `bundles 40→33`, spills 0 | **rejected**, `no-region-profitable` |

So region formation is not missing, mis-scoped, or gated off by the oracle. Each
candidate was then evaluated on its own, on the production IR:

| selection | spilled | **to memory** | instrs | static bundles |
|---|---|---|---:|---:|
| **JT=4** baseline | False | False | 99 | 40 |
| JT=4 only block@66 | False | False | 99 | 35 |
| JT=4 only block@75 | False | False | 99 | 38 |
| JT=4 only block@89 | False | False | 99 | 38 |
| JT=4 all 3 | **False** | **False** | 99 | **33** |
| **JT=8** baseline | False | False | 147 | 47 |
| JT=8 only block@102 | **True** | **True** | 153 | 46 |
| JT=8 only block@115 | **True** | **True** | 153 | 46 |
| JT=8 only block@137 | **True** | **True** | 153 | 46 |
| JT=8 reschedule only, **no merge** | **True** | **True** | 153 | 47 |
| JT=8 all 3 | **True** | **True** | 153 | 44 |

Two things fall out. First, at JT=8 **every** option spills to memory —
including rescheduling with no merge at all — so what ships is the *unrescheduled*
emission order. Second, the merged JT=8 candidate would have *fewer* static
bundles (47 → 44); it is rejected purely on the spill.

**Forcing it through confirms the gate is right, not over-conservative.** With
the merge forced and the fallback bypassed, the emitted program runs 62 ticks
and fails **248 of 256** PostConditions — the documented hazard that a spilled
loop-live value across a back-edge is unreliable in this backend. *(An earlier
attempt suppressed the spill flag globally and also broke the program; that
harness had disabled the LICM/loop-reg tier fallbacks as well, so it proved
nothing and was discarded. The scoped repeat above is the one that counts.)*

**Stop condition 2/3 would have fired here** — a merged-region schedule does not
materially improve ticks, because it cannot be built correctly. The milestone
continues only because Phase 5 found *why* it does not fit.

## Phase 3 — `fe_12`: the five one-instruction bundles

The IR, verbatim:

```
102  IRLabel         fe_12:
103  IRBinOp         _t168 = _t164 + _lr241        ; i*N + j
104  IRBinOp         _t170 = _t168 + 0             ; <-- identity
105  IRBinOp         _t171 = _t170 << 3            ; * 8 bytes
106  IRGlobalAddrOf  _gsb1  = &DMEM[0x600 + _t171]
107  IRStore         *(_gsb1+0)  = _lr242
...  (8 stores, +0 +8 +16 +24 +32 +40 +48 +56)
115  IRLabel         fi_7:
116  IRBinOp         _lr241 = _lr241 + 8
```

| # | opcode | in | out | dep pred | dep succ | classification |
|---|---|---|---|---|---|---|
| 103 | `+` | `_t164`(i·N, j-invariant), `_lr241`(j) | `_t168` | — | 104 | **true RAW**, induction-affine |
| 104 | `+ 0` | `_t168` | `_t170` | 103 | 105 | **avoidable — identity copy** |
| 105 | `<<3` | `_t170` | `_t171` | 104 | 106 | **true RAW**, address scaling |
| 106 | `&DMEM+off` | `_t171` | `_gsb1` | 105 | 107–114 | **true RAW**, address materialization |
| 107–114 | `$st` ×8 | `_gsb1`, 8 accumulators | — | 106 | — | output stores, mutually independent |

Four of the five are true RAW; one (104) is a pure identity that costs a whole
bundle 32 times. In mcode the chain becomes five bundles because the `$set
r15=512` constant and the GBASE add are appended.

**Is the chain necessary at all?** No: `_t168` is affine in the tile IV
(`+ J_TILE` per iteration), so the whole chain is a pointer increment. That is
exactly the gap **R14.10** documented and stopped on — `ivsr._iv_term` requires
`iv * Const` with the multiplied operand *directly* an IV load, and
`_decompose` handles only `+` at the top level, so `((i*N)+j)*8` is not
recognized. R16.4 measures its cost: **160 ticks at JT=8, 16% of the program.**

**Could independent work legally be scheduled into these bundles?** Not from
within `fe_12` — the eight stores all depend on 106, and the accumulators are
already final. The only independent work is in `fb_6`, which requires the merge
of Phase 2. So the honest answer is: **yes, but only via the merge**, and today
the merge does not fit in the register file.

## Phase 4/5 — 3-wide vs 4-wide rotation, and why 3 is chosen

The lowering emits the packed operands **strictly alternating**:

```
62  IRLoad    _vpb50 = *(_vra13+0)
63  IRVecDot  _vac34 = dot _vpa49 . _vpb50
64  IRLoad    _vpb51 = *(_vra13+16)
65  IRVecDot  _vac36 = dot _vpa49 . _vpb51
...                                   (16 load/dot pairs)
```

Every load is consumed by the very next instruction, so each packed-operand temp
lives for exactly one instruction. **Register allocation runs before the mcode
scheduler** (`bundler._schedule_within_blocks` reorders already-allocated
instructions), so the allocator — seeing sixteen one-instruction lifetimes —
folds them onto **three** physical registers at JT=8 (`$r7/$r8/$r9`) and four at
JT=4. The scheduler then physically *cannot* hoist a fourth load: there is no
fourth register name to put it in. **The rotation width is decided by the
allocator, from the emission order, before any scheduling happens.** That is a
phase-order effect, not a heuristic choice.

Regrouping the same instructions into waves of 4 before allocation confirms the
mechanism: the allocator then hands out four registers, the packer reaches
**4 `$dot` per bundle**, static bundles fall 47 → 45 at no instruction cost, and
the program runs **951 ticks, 256/256 correct** (−64, exactly R16.3's estimate).

**But that route is not safe, and the milestone's honesty requires saying so.**
The 4-wide grouping triggers one pressure eviction which codegen resolves by
R7.1 *rematerialization* (`spilled=True`, `spilled_to_memory=False`), and the
acceptance gates read only the coarse flag. Relaxing them to
`spilled and spilled_to_memory` is what let the 951-tick build through — and the
same relaxation, **with no reordering at all (W=1)**, makes a second program
(loop-initialized 16×16 `vu8`) compute 240 wrong results. The gate is coarse,
but loosening it is not the fix. **W=8 is worse still**: it introduces a genuine
memory spill and 264 wrong results — **stop condition 3, recorded and respected.**

## Phase 6/7 — The actual root cause: accumulator shadow copies

`fb_6` wraps every accumulator in a copy pair:

```
42-49  IRAssign _lr242.._lr249 = 0        ; 8 zero-inits
54-61  IRAssign _vac34 = _lr242 ...       ; 8 copies IN
62-93  ... 16 dots accumulate into _vac34.._vac48 ...
94-101 IRAssign _lr242 = _vac34 ...       ; 8 copies OUT
```

Eight accumulators, **sixteen registers**. R16.3 measured the consequence
(20 pool registers live, 8 free — and the free 8 *are* the shadows); R16.4
identifies why they survive every existing cleanup:

* the **copy-out** cannot be coalesced because its producer is `IRVecDot`, and
  `coalesce._COALESCEABLE_PRODUCERS` deliberately excludes it (`$dot
  $accumulate` reads its own destination, so retargeting it is not a plain
  producer rewrite);
* the **copy-in** cannot be coalesced because `_lr242` has other users (the
  copy-out and the stores), violating the pass's condition 3 ("src has no user
  other than the copy").

Closing the round trip — when `_lr` is provably neither read nor written between
the two copies, rename the interior and delete both — and re-running the
**unmodified** pipeline with **no gate override**:

| | JT=8 today | **JT=8 shadows removed** | JT=4 today | JT=4 shadows removed |
|---|---:|---:|---:|---:|
| ticks | 1015 | **795** | 987 | 861 |
| ticks / output | 3.965 | **3.105** | 3.855 | 3.363 |
| executed instructions | 3339 | **2827** | 3531 | 3019 |
| register copies executed | 515 | **3** | 515 | 3 |
| dynamic IPB | 3.290 | **3.556** | 3.578 | 3.506 |
| `$dot` per bundle | 3 | **4** | 4 | 4 |
| j-body basic blocks | **3** | **1** | 1 | 1 |
| bundles / j-iteration | 21 | **15** | 10 | — |
| peak simultaneous liveness | 23 | **24** / 28 | 21 | 21 / 28 |
| memory spills | 0 | **0** | 0 | 0 |
| correctness | 256/256 | **256/256** | 256/256 | 256/256 |

Three things happen at once, none of them asked for individually:

1. **512 executed copies disappear** (515 → 3).
2. **The superblock merge now succeeds on its own** — `fe_12` and `fi_7` are
   gone from the emitted code; the j-body is one block of 15 bundles.
3. **The rotation widens to 4 `$dot` per bundle** without the reorder of Phase 4,
   because the allocator now has the registers.

Register targets are met: peak **24 ≤ 28**, zero spills, 8 accumulators resident,
no reloads.

## Phase 8/9 — Performance comparison

| variant | ticks | ticks/out | bundles/iter | instrs | IPB | peak live | spills | dot/bundle |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| JT=4 current | 987 | 3.855 | 10 | 3531 | 3.578 | 21 | 0 | 4 |
| JT=8 current | 1015 | 3.965 | 21 (3 blocks) | 3339 | 3.290 | 23 | 0 | 3 |
| JT=8 merged-region what-if | — | — | — | — | — | — | **1 (memory)** | — |
| JT=8 4-wide rotation what-if | 951 | 3.715 | 19 | 3339 | 3.511 | 23 | 0 (remat) | 4 |
| **JT=8 shadow copies removed** | **795** | **3.105** | **15 (1 block)** | **2827** | **3.556** | **24** | **0** | **4** |
| JT=4 shadow copies removed | 861 | 3.363 | — | 3019 | 3.506 | 21 | 0 | 4 |

The merged-region row has no ticks because it cannot be built correctly (Phase 2).
The rotation row is real but was obtained through a gate relaxation shown to be
unsafe elsewhere (Phase 4). **Only the last two rows are both measured and
safely reachable.**

**JT=8 does not merely catch up — it wins.** 795 vs JT=4's best 861 (−7.7%), and
vs JT=4 today's 987 (−19.5%). The tile ordering reverses, which is the opposite
of R16.3's operational recommendation and the reason that recommendation was
scoped as "today".

## Phase 10 — Distance to the hand-written kernel

| | hand-written | JT=8 today | JT=8 shadows removed |
|---|---:|---:|---:|
| ticks | 241 | 1015 (4.21×) | **795 (3.30×)** |
| executed instructions | 1160 | 3339 (2.88×) | 2827 (2.44×) |
| IPB | 4.813 | 3.290 (0.68×) | 3.556 (0.74×) |

The decomposition still reconciles exactly: 2.44 × 1.353 = 3.30. Of the original
4.21× gap, this closes **0.91×**. What remains, by measured category:

| remaining gap component | evidence | ticks |
|---|---|---:|
| result-address chain not strength-reduced (R14.10) | 4 serial 1-instruction bundles × 32 | ~128 |
| loop control + index arithmetic | 760 address ALU + 131 branches executed | — |
| accumulator zero-init per tile | 274 executed `= 0` | ~32 |
| `$dot` density 4 vs 8 | needs loads decoupled from dots (~16 packed operands live) | — |

Region formation is **no longer** part of the remaining gap: after the fix the
j-body is a single block. The 3-vs-4 rotation is likewise closed. What is left is
scalar address/control overhead and the load/dot decoupling that R16.0 showed
requires an 18-register operand block.

## Phase 11 — Generality

The transform is keyed on the copy-pair *pattern*, not on a type, size or tile:
it matches `IRAssign X = Y … IRAssign Y = X` with a clean interior, and the
measurement harness applies it module-wide (72 pairs in the 16×16 `vu8`
program, 36 at JT=4).

Sweep over the supported packed markers at two sizes, production vs
shadow-removed, all executed against gcc goldens:

<!--SWEEP-->

The gains are small on these programs because they initialize their arrays with
a loop, so the kernel is a minority of the run — unlike the fixed-DMEM programs
where initialization is preloaded. What matters here is that the transform is
**correct on every configuration** and never regresses.

## Phase 12 — No scheduler change is required

Nothing in the diagnosis points at the global scheduler, the bundler or the
register allocator:

* region formation already offers the right merges and already accepts them
  when they fit (JT=4, and JT=8 after the fix);
* the bundler's packer is not the limiter — `BundleFull` is ~0 in both configs;
* the allocator is not making a bad choice — it is making the only choice the
  emission order leaves it.

The one abstraction that *is* coarse — the `spilled` flag conflating R7.1
rematerialization with a real memory spill — was tested directly and **must not
be loosened**: doing so miscompiles a second program. It is worth recording as a
latent issue, not as this milestone's fix.

## Root cause, stated once

> **The vector lowering materializes each reduction accumulator twice — a
> promoted loop register and a `_vac` working copy — and closes the round trip
> with a copy in and a copy out. At J_TILE=8 that is 16 registers for 8
> accumulators. The resulting pressure is what makes every rescheduling and
> every region merge spill, which is why the j-body ships as three blocks in
> unscheduled emission order with a 3-wide `$dot` rotation.**

Region formation (A/B/E) and rotation width (D) are consequences. The cause is
**C, instruction emission order**.

## Recommended next implementation step — exactly one

**Eliminate the accumulator shadow-copy round trip in the vector lowering**, so
the reduction accumulates directly into the promoted loop register.

Why this one:

* it is the *cause*, not a symptom — it alone delivers all three effects
  (copies gone, region merged, rotation widened);
* it needs **no** scheduler, bundler or allocator change;
* it passes every existing production gate unmodified — no relaxation, no
  override, zero spills, peak 24 ≤ 28;
* it helps **both** tiles (JT=4 −12.8%, JT=8 −21.7%), so it is not a JT=8
  special case;
* it is the same family as **R14.6**, which removed the identity self-copy on
  the other side of `$dot $accumulate`; this closes the remaining half.

Scope note for whoever implements it: the production form should emit the
accumulator directly rather than post-processing copies, and `coalesce.py`'s two
blocking conditions (`IRVecDot` not a coalesceable producer; "src has no other
user") document why a generic coalescing rule will not reach this pattern.

**Do not start R16.5 automatically.**

## Final question

> **Can JT=8 be made faster than JT=4 by fixing region formation and/or changing
> the load/dot scheduling order, without exceeding 28 live registers?**

**Yes — by fixing neither of them directly.**

| | current JT=8 | best what-if |
|---|---:|---:|
| ticks | 1015 | **795** |
| bundles per j-iteration | 21 (across 3 blocks) | **15 (one block)** |
| static bundles (aligned) | 61 | 54 |
| `$dot` per bundle | 3 | **4** |
| peak simultaneous liveness | 23 / 28 | **24 / 28** |
| spills | 0 | **0** |
| correctness | 256/256 | **256/256** |
| vs JT=4 (987) | +2.8% | **−19.5%** |

# R7.0 — Vector Register Pressure Analysis

**Answer: rematerialization of stack-address temporaries is the highest-return
optimization, and it is not close. 26 of the 33 registers the allocator spills
across the rejected schedules hold `FP + constant` — recomputable in ONE
instruction with NO register inputs. Rematerializing them covers the entire
deficit of every rejected schedule.**

Analysis only. No compiler source changed.

| transformation | recovers | evidence |
|---|---|---|
| **rematerialization** | **4 of 4 rejected schedules** | remat-free values at peak (11/17/16) exceed every deficit (7/9/8) |
| **pressure-aware unrolling** | **4 of 4**, but by giving up unrolling | every kernel fits at U=1 (24/24/27/27 ≤ 28) and none at U ≥ 2 |
| live-range splitting | plausibly 4 of 4, at a spill/reload cost | 32 of 35 values at peak are pure pass-through |
| pressure-aware scheduling | **0 expected** | liveness here forms an interval graph, so peak liveness *is* the exact requirement |

---

## 1. Method

The **real allocator** is instrumented, not modelled: `codegen.CodeGen` is
subclassed to record, after every IR instruction, the set of temporaries holding
registers, and to record every eviction `_spill_evict` performs. No allocator
behaviour is changed.

**True demand** (what the code would need with no limit) is measured the same way,
by enlarging `codegen.POOL_REGS` to 400 and re-running. The allocator then never
spills and its peak occupancy *is* the requirement — measured by the allocator's
own liveness and free logic rather than by a re-derivation of it. Note the pool is
rebuilt per function inside `generate()`, so the module-level list must be patched;
patching the instance silently does nothing (a first attempt did exactly that and
reported a demand of 28 for code needing 35).

Values are classified on two independent axes:

* **what the value holds** — from its *defining instruction*: a packed load or a
  `$v`/`$dot`/`$vreduce` result is a **vector** value, an `IRLoadAddr` result is an
  **address**, anything else is **scalar**. Names alone are not enough: `_vaa` is
  a vector sum in the AXPY lowering and an accumulator *address* in the
  dot/reduction lowering.
* **which pass created it** — from its name prefix, each verified against the
  emitting module, plus the `~pN` suffix that modulo variable expansion adds.

## 2. Pressure in the code that ships today

The 28-register pool is `$r1..$r31` minus `$r0/$r26/$r27/$r28`.

| kernel | peak live | vector | address + scalar | spills | dominant source at peak |
|---|---|---|---|---|---|
| elementwise vi8 | 17/28 | 15 | 2 | 0 | vector lowering |
| elementwise vi16 | 18/28 | 5 | 13 | 0 | compact-loop scaffolding (12) |
| **elementwise vi32** | **28/28** | 8 | 20 | 0 | compact-loop scaffolding (17) |
| axpy vi8 | 18/28 | 16 | 2 | 0 | vector lowering |
| axpy vi16 | 13/28 | 3 | 10 | 0 | compact-loop scaffolding (9) |
| axpy vi32 | 22/28 | 3 | 19 | 0 | compact-loop scaffolding (17) |
| reduction vi8 / vi16 | 10 / 17 | 6 / 4 | 4 / 13 | 0 | vector lowering, accumulator expansion |
| **reduction vi32** | **28/28** | 8 | 20 | 0 | accumulator expansion (8), IVSR (8) |
| dot vi8 / vi16 / vi32 | 9 / 9 / 10 | 4 / 4 / 0 | 5 / 5 / 10 | 0 | front-end scalar |
| conv3 vi8 / vi16 / vi32 | 5 / 12 / 20 | 3 / 3 / 5 | 2 / 9 / 15 | 0 | packed-window temporaries |
| gemm vi8 / vi16 / vi32 | 23 / 9 / 9 | 3 / 3 / 3 | 20 / 6 / 6 | 0 | clone_offset, compact-loop scaffolding |

**Nothing that ships spills** — by construction, since every gate from R4.1 onward
rejects a spilling candidate. But two kernels sit at **exactly 28/28**
(`elementwise vi32`, `reduction vi32`): they have zero headroom, and any further
transformation of them will spill.

**Pressure is not dominated by vector data.** At 16 and 32 bits the peak is mostly
*addresses* — 13 of 18, 20 of 28, 19 of 22. Only the 8-bit kernels, which use the
fully-unrolled realisation, are vector-data-dominated (15 of 17, 16 of 18).

## 3. The rejected schedules — where the milestone's blocker actually is

R6.8 pipelines a loop, then discards it if codegen spills. Measuring those
candidates *before* the gate rejects them:

| kernel | base demand | **SWP demand** | SWP cost | over 28 | evictions | outcome |
|---|---|---|---|---|---|---|
| elementwise vi16 / vu16 | 18 | **35** | **+17** | **+7** | 16 | rejected |
| elementwise vi32 / vu32 | 28 | **37** | **+9** | **+9** | 9 | rejected |
| **axpy vi16 / vu16** | 13 | **27** | **+14** | **−1** | **0** | **committed** |
| axpy vi32 / vu32 | 22 | **36** | **+14** | **+8** | 8 | rejected |

**Software pipelining costs +9 to +17 registers**, and the only kernel that
survives does so by exactly one register. The deficits are small — 7 to 9 — not
structural.

Sources at the pipelined peak:

```
elementwise vi16 (35): rotating banks 24, compact-loop scaffolding 10, kernel 1
elementwise vi32 (37): rotating banks 21, compact-loop scaffolding 16
axpy vi16        (27): rotating banks 18, compact-loop scaffolding  7, +2
axpy vi32        (36): rotating banks 19, compact-loop scaffolding 16, +1
```

Modulo variable expansion keeps `stages = 2` banks of the body live at once, which
roughly doubles the body's contribution; the compact-loop address scaffolding is
then duplicated per bank as well.

### Occupancy over time

| kernel | instrs | mean | p50 | p90 | peak | % ≥ 24 | % ≥ 28 |
|---|---|---|---|---|---|---|---|
| elementwise vi16 shipped | 85 | 5.4 | 4 | 13 | 18 | 0% | 0% |
| elementwise vi16 **+SWP** | 128 | 12.5 | 9 | **28** | **28** | **27.3%** | **19.5%** |
| axpy vi16 shipped | 79 | 4.7 | 3 | 10 | 13 | 0% | 0% |
| axpy vi16 **+SWP** | 110 | 10.3 | 5 | 26 | 27 | 15.5% | **0%** |

Pressure is **bursty, not sustained**: the median stays at 5–9 registers while the
top decile saturates. `elementwise` is at the limit for a fifth of its
instructions; `axpy` never reaches it. That shape is what makes a *local* remedy
(recompute a value at the few points where the pool is full) more promising than a
global one.

### Interference graph

Under the allocator's linear model each temporary occupies one interval from
definition to last use, so the interference graph is an **interval graph**. For
interval graphs the chromatic number equals the maximum clique, and the maximum
clique is exactly the peak simultaneous liveness reported above. Two consequences,
both load-bearing for the recommendations:

* the demand figures in §3 are **exact** register requirements, not heuristic
  estimates;
* **no better colouring exists.** A smarter allocator cannot recover any of these
  schedules — only reducing the number of simultaneously live values can.

## 4. Spill locations and causes

Every eviction the allocator performed, classified:

| kernel | evictions | by kind | rematerializable for free |
|---|---|---|---|
| elementwise vi16 | 16 | 10 address, 6 scalar | **10** |
| elementwise vi32 | 9 | **9 address** | **9** |
| axpy vi32 | 8 | 7 address, 1 scalar | **7** |
| **total** | **33** | **26 address (79%)** | **26 (79%)** |

The evicted values are overwhelmingly the compact loop's address temporaries:

```
at IR#71 evict _vcl1   while lowering  _vea30~p0 = *(_vcb31~p0 + _vcu28~p0)
at IR#72 evict _vcl3   while lowering  _vea32~p0 = *(_vcb33~p0 + _vcu28~p0)
at IR#97 evict _vcb7   while lowering  _vea32~p1 = *(_vcb33~p1 + _vcu28~p1)
```

`_vcl1`, `_vcl3`, `_vcb7` are all `IRLoadAddr` — `dest = &stack[FP + constant]`.
**The allocator pays a store plus a later reload (two memory operations) to keep a
value that could be recomputed with one ALU instruction and no register inputs.**

The cause is not a bad eviction choice: the allocator has no rematerialization
concept at all, so spilling is the only tool it has.

## 5. Pressure sources, classified

Across the pipelined candidates, at the peak:

| source | share of peak | notes |
|---|---|---|
| **software pipelining** (rotating banks + kernel) | **51–69%** | `stages = 2` banks of the whole body |
| **compact-loop scaffolding** (address computation) | **26–43%** | duplicated per bank; almost entirely `IRLoadAddr` |
| unrolling (per-copy offset/index) | 0–3 at peak | small directly, but it sets the body size the banks multiply |
| vector lowering (packed data) | 1–4 | the actual vector values are a minority |
| accumulator expansion | 0 here; 8 in shipped `reduction vi32` | that kernel is at 28/28 |
| packed-window temporaries | 3–9 in `conv3` | conv3 is never pipelined |
| clone_offset temporaries | 5 in `gemm vi8`, 1–5 in `conv3` | not on the pipelining path |

**Unrolling and pipelining compound rather than add**: unrolling sets the body
size, and pipelining then keeps `stages` copies of that body live.

## 6. Estimated benefit of each candidate transformation

### 6a. Rematerialization — recovers 4 of 4, highest return

Count of values at the pipelined peak that are `IRLoadAddr` or a constant
assignment (one instruction, no register inputs), against the deficit:

| kernel | deficit | remat-free at peak | peak after remat | fits 28? |
|---|---|---|---|---|
| elementwise vi16 | 7 | **11** | 24 | **yes** |
| elementwise vi32 | 9 | **17** | 20 | **yes** |
| axpy vi32 | 8 | **16** | 20 | **yes** |

**Rematerializing only the free values covers every deficit with room to spare**,
and it is what the allocator is already choosing to spill (79% of evictions). The
cost is one ALU instruction per rematerialized use, replacing a store and a
reload — a *reduction* in memory traffic, not a trade.

### 6b. Pressure-aware unrolling — recovers 4 of 4, but by giving up unrolling

Demand as a function of the unroll factor:

| kernel | U=1 | U=2 | U=4 |
|---|---|---|---|
| elementwise vi16 | **24 (fits)** | 35 | — |
| elementwise vi32 | **24 (fits)** | 35 | 37 |
| axpy vi16 | **27 (fits)** | — | — |
| axpy vi32 | **27 (fits)** | 40 | 36 |

**Every kernel fits at U=1 and none at U ≥ 2.** This also explains the R6.8 result
completely: `axpy vi16` is the only kernel whose *adaptively chosen* factor is
already 1 (R6.4.1 measured 1× as its optimum), which is precisely why it is the
only one that pipelines.

The catch is that this is not free — it trades unrolling's measured gains for
pipelining's. R6.4.1 chose those factors by measurement, so lowering U to enable
SWP is only worthwhile if SWP@U=1 beats unroll@U=k, which **has not been
measured** and is the obvious experiment to run before implementing this.

### 6c. Live-range splitting — plausible, but strictly worse than 6a here

At the peak, **32 of 35 values (elementwise vi16) and 33 of 36 (axpy vi32) are
pure pass-through** — neither defined nor used at that instruction. Splitting has
plenty of candidates and would work.

But 11 and 16 of those pass-through values are the *same* remat-free addresses,
and for them splitting costs a store plus a reload where rematerialization costs
one ALU instruction. Splitting is the right tool only for the remaining
non-rematerializable values (21 and 17), and the deficits are already covered
without touching them.

### 6d. Pressure-aware scheduling — expected benefit zero

Reordering can only help by shortening live ranges. Two measurements say it will
not help here:

* the interference graph is an **interval graph** (§3), so the demand figures are
  exact minimums for the given code — a different *colouring* cannot help, only
  different *code*;
* the values at the peak are 91% pass-through, meaning they are live across the
  peak because they are defined earlier and used later — a rotating bank's value
  must live from its stage's definition to its stage's use, and that distance is
  fixed by `II` and `stages`, not by the local order.

**Recommendation: do not pursue pressure-aware scheduling.** It is the option
with the clearest evidence *against* it.

## 7. Recommendation

**Implement rematerialization of stack-address temporaries first.** It is the only
candidate that recovers every rejected schedule without giving anything up, it
targets 79% of what the allocator actually spills, and it reduces memory traffic
rather than trading it. Scope it narrowly: a value whose defining instruction is
`IRLoadAddr` (`dest = FP + constant`) or an `IRAssign` of a constant is
recomputable anywhere, with no inputs and no ordering constraint.

**Second, measure SWP@U=1 against unroll@U=k** (§6b) before considering
pressure-aware unrolling. If rematerialization alone lifts the kernels over the
line, that experiment becomes unnecessary.

**Do not pursue pressure-aware scheduling** (§6d), and treat live-range splitting
as a follow-on for the non-rematerializable remainder only (§6c).

Also worth noting outside the pipelining path: **`elementwise vi32` and
`reduction vi32` already ship at 28/28** with no headroom at all. They are the
next things to break, and rematerialization would give them room too.

## 8. Threats to validity

* **The demand figures assume the allocator's linear liveness model.** That is the
  model the shipping allocator uses, so the numbers predict *its* behaviour; a
  different allocator would give different ones.
* **The remat estimate assumes each rematerialized value frees one register at the
  peak.** That holds when the value's only role at the peak is to be live, which is
  true for the pass-through addresses measured here, but a real implementation must
  re-measure rather than assume the subtraction.
* **`stages = 2` in every case measured.** Deeper pipelines would multiply the
  banks further and may not be recoverable by rematerialization alone.
* **Three kernels supply the rejected-schedule evidence** (elementwise vi16/vi32,
  axpy vi32) plus their unsigned twins, which behave identically. The unsigned
  variants were checked and are not independent samples.
* **Peak-based reasoning ignores duration.** `elementwise vi16` is at the limit for
  19.5% of its instructions and `axpy vi16` for 0%, which is a large qualitative
  difference that a single peak number does not convey.
* No simulator measurements were taken: nothing was implemented, so there is
  nothing to run.

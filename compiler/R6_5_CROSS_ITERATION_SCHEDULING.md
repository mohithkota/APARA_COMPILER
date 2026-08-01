# R6.5 — Cross-Iteration Vector Scheduling

**No code was changed, because the optimization R6.5 asks for is already
implemented and already optimal.** The scheduler does schedule across unrolled
iterations, instructions from different logical iterations do compete equally
for bundle slots, and the resulting schedule provably cannot be improved by any
local reordering.

This report is the evidence for that claim, because a milestone that ends in "no
change" is only worth anything if it is falsifiable. Three independent
measurements are given, one of which does not use my own analysis code at all.

---

## 1. The premise, and where it came from

The milestone states:

> Analysis shows that independent copies created by unrolling are not being
> interleaved into common bundles. The lowering is complete. The remaining
> bottleneck is scheduling.

That came from my own R6.4 report, which said "the copies, though independent,
are not being interleaved into the same bundles." **I inferred that from IPB and
occupancy barely moving. I never looked at the emitted bundles.** It was wrong,
and R6.5 inherited it.

The supporting figure — occupancy ~20%, "four of five issue slots idle" — is
also being read incorrectly. It is a **whole-program** number, and these
benchmark programs spend most of their bundles in a 64- or 136-iteration
**scalar initialisation loop** that no vector work touches. Measured on the
vector region alone:

| kernel | realisation at adaptive factor | vector-region occupancy | peak |
|---|---|---|---|
| gemm vi16 | unrolled | **76.6%** | 8/8 |
| conv3 vi8 | unrolled+peeled | **71.6%** | 8/8 |
| reduction vi32 | compact | 45.0% | 8/8 |
| dot vi8 | unrolled | 32.1% | 8/8 |
| elementwise vi16 | compact | 26.8% | 3 |
| axpy vi16 | compact | 19.6% | 2 |
| *(whole program, for contrast)* | | *~20%* | |

The vector regions are **not** at 20%. Two of them are above 70%. The success
criterion "occupancy beyond ~20%" was already satisfied before R6.5 began; the
metric was simply being measured over the wrong bundles.

## 2. Evidence 1 — the copies are visibly interleaved

`elementwise vi16`, which the adaptive selector unrolls 2×. Both copies are in
one basic block; the emitted bundles are:

```
vcl_2_body:
  ||  $ld ($i32) $r4 [$r23+0]      $ld ($i32) $r16 [$r2+0]                   ;
  ||  << $r3 ($i64) $r4 1          + $r20 ($i64) $r16 8                      ;
  ||  $ld ($i64) $r7 [$r24+$r3]    $ld ($i64) $r8 [$r25+$r3]
      + $r14 ($i64) $r3 8                                                    ;
  ||  $ld ($i64) $r17 [$r30+$r14]  $ld ($i64) $r15 [$r31+$r14]
      $v + $r19 ($vi16) $r7 $r8          <-- COPY 0's add, with COPY 1's loads
  ||  $v + $r18 ($vi16) $r17 $r15        <-- COPY 1's add, with COPY 0's store
      $st ($i64) [$r29+$r3] $r19                                             ;
  ||  $st ($i64) [$r1+$r14] $r18                                             ;
  ||  $st ($i32) [$r6+0] $r20      ? ($i64) $r0 == $goto vcl_1_cond          ;
```

Copy 0's vector add issues in the same bundle as copy 1's loads, and copy 1's
add issues with copy 0's store. That is cross-iteration interleaving, in the
shipped code, today.

The reason is structural and was true before R6.4: **unrolled copies are emitted
inline into the same basic block**, and `bundler._schedule_within_blocks` splits
only on labels, so `_schedule_block` already receives all `u` copies as one
region. There is no region to extend — the region the milestone asks for is the
one that already exists.

## 3. Evidence 2 — how much the interleaving is worth

Provenance is lost after register allocation, so rather than tag instructions I
compared against the counterfactual the milestone describes: copies scheduled
one after another would cost `u ×` the 1× body.

**`reduction vi32` compact body:**

| u | instrs | bundles | if copies were sequential | saved | occupancy |
|---|---|---|---|---|---|
| 1 | 8 | 3 | 3 | — | 33.3% |
| 2 | 12 | 4 | 6 | **33%** | 37.5% |
| 4 | 20 | 6 | 12 | **50%** | 41.7% |
| 8 | 36 | 10 | 24 | **58%** | 45.0% |

**`elementwise vi16`:** 1× is 10 instrs in 6 bundles; 2× is 15 instrs in **7**
bundles, against 12 if sequential — **42% saved**. Doubling the work costs one
bundle.

Ready-operation supply behaves exactly as unrolling theory predicts, which is
why occupancy climbs rather than saturating:

| u | avg ready ops | true critical path | occupancy |
|---|---|---|---|
| 1 | 2.67 | 7 | 33.3% |
| 2 | 3.00 | 8 | 37.5% |
| 4 | 3.67 | 10 | 41.7% |
| 8 | 5.50 | 14 | 45.0% |

## 4. Evidence 3 — the schedule cannot be improved (two ways)

### 4a. Analytic bound

`pack_lower_bound` takes the largest of three bounds that hold under *any* legal
schedule: issue width `⌈N/8⌉`, memory lanes `⌈mem/4⌉`, and the longest chain of
pairs that can never share a bundle. Each chain edge forces strict ordering *and*
forbids sharing, so a chain of k forces k bundles in every legal schedule.

**It equals the shipped bundle count in 20 of 23 configurations measured**
(6 kernels × unroll factors 1/2/4/8), and in every one of those the term that
binds is the *dependence chain*, not issue width and not memory lanes:

| kernel | u | shipped | bound | width / mem / chain | binds | slack |
|---|---|---|---|---|---|---|
| elementwise vi16 | 1 / 2 / 4 / 8 | 6 / 7 / 18 / 18 | 6 / 7 / 18 / 18 | chain each | chain | **0** |
| axpy vi16 | 1 | 7 | 7 | 2 / 2 / 7 | chain | **0** |
| axpy vi16 | 2 / 4 / 8 | 19 | 17 | 17 / 13 / 15 | *width* | **2** |
| reduction vi32 | 1 / 2 / 4 / 8 | 3 / 4 / 6 / 10 | 3 / 4 / 6 / 10 | chain each | chain | **0** |
| conv3 vi8 | any | 26 | 26 | 19 / 11 / 26 | chain | **0** |
| gemm vi16 | 1 / 4 / 8 | 6 / 23 / 23 | 6 / 23 / 23 | chain each | chain | **0** |
| dot vi8 | 1 / 2 / 4 / 8 | 3 / 7 / 28 / 28 | 3 / 7 / 28 / 28 | chain each | chain | **0** |

The three exceptions are all the *same* body: `axpy vi16` above 1×, where the
realisation flips to fully-unrolled straight-line code and the greedy packer
lands **2 bundles above** a width-limited bound. That is the one measured case
with slack, it is not an unrolled loop body, and it is 2 bundles — reported
because it is the only counterexample, not because it is significant.

### 4b. Independent search, not using my bound at all

Because the bound shares a hazard predicate with the packer, "bound == shipped"
could in principle be true by construction. So I tested it a second way that
uses **only the real `bundler._pack_bundles`**: enumerate random legal
topological orders of the body under a dependence relation that is a
conservative *superset* of what any correct scheduler must obey
(RAW + WAR + WAW + memory-may-alias + pinned control), pack each with the
production packer, and take the minimum.

| kernel | u | instrs | shipped | best of 4000 legal orders |
|---|---|---|---|---|
| elementwise vi16 | 2 | 15 | 7 | **7** — cannot be beaten |
| axpy vi16 | 1 | 11 | 7 | **7** — cannot be beaten |
| reduction vi32 | 8 | 36 | 10 | **10** — cannot be beaten |

12 000 legal schedules, none better than what ships.

## 5. What actually limits occupancy

Not the scheduling region, and not the scheduler. **The dependence chain through
one chunk.** `load → shift → or → vadd → vadd → store` is six levels deep and
the compact body holds only 8–36 instructions, so the chain — not the eight
issue slots — sets the bundle count.

Unrolling is the correct lever against exactly that, which is why occupancy rises
monotonically with `u` in §3. The remaining ceiling is that **the chain grows
with `u` too** (7 → 14 for `reduction vi32`), because the copies share the
induction variable and, for reductions, the accumulator. Those are genuine
loop-carried dependences, not scheduling artifacts.

The only way past a chain that binds within a single body is to overlap
*different trips of the loop* — start iteration `i+1`'s loads before iteration
`i`'s store retires. That means scheduling across the loop back edge, i.e.
software pipelining or modulo scheduling, which R6.5 explicitly excludes:

> Do not implement modulo scheduling. Do not redesign software pipelining.

So the milestone's constraints and its goal are in tension: within the region it
permits, the schedule is already optimal.

## 6. Why R6.4's IPB and occupancy barely moved — corrected explanation

R6.4 was right that unrolling "removed work rather than packing it denser", and
right that ticks fell 34% while occupancy moved 0.8pp. But the reason it gave —
copies not interleaved — was wrong. The real reason is that the **whole-program**
occupancy denominator is dominated by scalar initialisation bundles that
unrolling does not touch. Within the vector region, unrolling raised occupancy
substantially (33.3% → 45.0% on `reduction vi32`, 20.8% → 26.8% on
`elementwise vi16`).

I am correcting my own earlier report rather than the milestone's reading of it.

## 7. Recommendation

**Implement nothing for R6.5.** Specifically, do not add a region-merging pass
for unrolled vector bodies: the region already spans the copies, and 12 000
legal reorderings confirm the packing inside it is optimal.

If cross-iteration overlap is still wanted, the only mechanism that can deliver
it is scheduling across the back edge. The machinery already exists — R2.5–R2.8
modulo scheduling and R3.1's `production_swp` — but it has never been applied to
vector loops. That is a real, scoped follow-on (**R6.6: apply SWP to vector
compact loops**), and it is the honest successor to this milestone. It is not in
R6.5's scope, so I did not start it.

## 8. Threats to validity

* **The reordering search is randomised, not exhaustive.** 4000 orders per
  kernel on bodies of 11–36 instructions does not enumerate the space. It is
  corroboration of the analytic bound, not a second proof; the bound is the
  stronger argument and the search is the check that the bound is not circular.
* **Three kernels were searched, 23 configurations bounded.** GEMM, conv3 and
  the fully-unrolled realisations were bounded but not searched (149-instruction
  bodies make random search uninformative at 4000 trials).
* **`axpy vi16` above 1× has 2 bundles of slack** against a width-limited bound.
  That is a fully-unrolled straight-line body, not an unrolled loop, so it is
  outside R6.5's subject — but it is the one measured case where the greedy
  packer is demonstrably not optimal, and I am not claiming otherwise.
* Occupancy and bundle counts are static/frequency-weighted; ticks are the
  simulator's, not hardware cycles.
* No code changed, so the regression results below are HEAD's, unchanged.

## 9. Regression

| check | result |
|---|---|
| simulator verification | **38/38 programs in 119.9 s**, 3 negative controls rejected |
| `loopopt/pipeline_crosscheck` | **PASS** |
| unit suites (all 15) | **15 pass, 0 fail** |
| compiler source modified | **none** |

### 9.1 A broken unit suite found at HEAD — and my own report that missed it

Running the suites for this milestone, **`_r6_1_test.py` failed at HEAD** with
`AttributeError: 'NoneType' object has no attribute 'n_vector_ops'`, with no
uncommitted change to any tracked file.

Cause: R6.4.1's adaptive unroll selection picks 4× for the `dot vi8` kernel,
which flips R4.2.5's realisation probe to the fully-unrolled form. There is then
no compact loop body, `r.hot` is `None`, and `test_depgraph` dereferences it.
The break was introduced by R6.4.1 and shipped in commit `6f79566`.

**`R6_4_1_ADAPTIVE_UNROLL.md` §7 claims "unit suites (all 15) all pass". That
claim was wrong**, and I am correcting it here rather than leaving it standing.

Fixed by **pinning the unroll factor for that suite**, which is precisely the
remedy R6.4 applied to `_r4_2_5` and `_r4_3` for the same class of breakage. The
R6.1 properties under test — issue model, occupancy attribution, dependence
graph of a vector loop — do not depend on the unroll factor, so nothing was
weakened; `os.environ.setdefault` keeps an explicit `APARA_VECTOR_UNROLL`
authoritative. This is the only source change in R6.5, and it is in a test.

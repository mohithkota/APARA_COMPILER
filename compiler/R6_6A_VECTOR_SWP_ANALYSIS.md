# R6.6A — Vector Software Pipelining Feasibility

**Verdict: worthwhile for exactly two of six kernels, and for one of the others
there is a cheaper optimization that beats software pipelining outright.**

Analysis only. No compiler source changed.

Summary of the recommendation, up front:

| kernel | class | why |
|---|---|---|
| elementwise | **3 — high** | 7 → 3 bundles/iteration (−57%), MII already at the resource bound |
| axpy | **3 — high** | 7 → 4 bundles/iteration (−43%) |
| reduction | **2 — limited** | 10 → 8 (−20%) via SWP; but **accumulator expansion gets 10 → 5 (−50%)** and is simpler |
| GEMM | **1 — none** | fully unrolled: no loop exists in the shipped IR |
| convolution | **1 — none** | fully unrolled: no loop exists |
| dot | **1 — none** | fully unrolled: no loop exists |

---

## 1. Method, and why the first attempt was invalid

The existing scalar SWP framework is reused verbatim: `loopopt.modulo`'s
`build_kernel`, `res_mii`, `rec_mii`, `min_ii`, with `discovery`,
`analysis_iv`, `analysis_mem`, `DependenceGraph` and `MemoryDisambiguator`
underneath. The **only** thing changed is the `_UNSUPPORTED_KERNEL` blocklist,
relaxed in the analysis process's memory. Nothing on disk was touched.

**The first run analysed the wrong IR.** Running on the raw vectorized IR gave
RecMII 8/10/24/40 — implausibly large for kernels whose only true recurrence is
an induction variable. That IR still contains the redundant IV loads and stores
the scalar optimizer later removes. Re-running on the IR that `production_codegen`
actually ships changed the answers materially (reduction's projected gain went
from 20% to 43% in cycle units, and its RecMII from 40 to 8).

The shipped-IR analysis is self-validating: its operation counts equal the mcode
loop bodies R6.5 measured, exactly — elementwise **15**, axpy **11**, reduction
**36** ops, matching 15 / 11 / 36 instructions. The two measurements are of the
same code.

**Units.** The milestone asks to project *dynamic bundles*, so MII is computed in
**bundle units** (an edge that forbids two operations sharing a bundle costs 1),
which is the bundler's actual hazard semantics. Cycle-unit figures using the
R2.4/R6.1 latency model are reported alongside and are *not* mixed with bundle
counts — doing so would have inflated every projection.

## 2. Two blockers in the existing framework

Every vector loop is currently rejected, for two independent reasons:

**(a) A blocklist entry.** `modulo._UNSUPPORTED_KERNEL` contains `IRVecArith`,
`IRVecDot`, `IRVecDot128`, `IRVecReduce`, so every vector loop returns
`unsupported-op:IRVecArith`. **The cost model already supports them** —
`schedule._latency` returns 2 / 4 / 4 / 3 for these and `_iclass` classifies them
ALU. This is a scope guard from R2.5, not missing modelling.

**(b) GEMM additionally fails `no-counted-iv`.** Its vector loop is innermost and
top-tested, but after optimization its trip count is `UNKNOWN`, so `_eligible`
rejects it before the blocklist is even reached. Lifting (a) would not enable
GEMM.

## 3. Where there is no loop at all

At the adaptive unroll factor R6.4.1 selects, three kernels have **no vector loop
in the shipped IR** — the vector operations are straight-line code:

| kernel | realisation | vector ops in shipped IR | loops containing them |
|---|---|---|---|
| GEMM vi16 | unrolled | 8 | vector loop exists but `no-counted-iv`; outer row loop is not innermost |
| conv3 vi8 | unrolled+peeled | 14 | **none** — all straight-line |
| dot vi8 | unrolled | 8 | **none** — all straight-line |

Software pipelining transforms a loop. Where the selector has fully unrolled the
kernel there is nothing to transform, so these are **class 1, no opportunity** —
not because the dependences forbid it but because the loop no longer exists.

## 4. Dependence graphs and recurrence analysis

### 4a. elementwise vi16 — the recurrence is the induction variable, in memory

Shipped loop body, 15 operations, with earliest start times (bundle units):

```
 n0  est=0  IRLoad      _vcv2  = *(_vcl1+0)          <-- IV load  (copy 1)
 n1  est=0  IRLoad      _vcv4  = *(_vcl3+0)          <-- IV load  (copy 2)
 n2  est=1  IRBinOp     _vco5  = _vcv4 << 1
 n3  est=2  IRLoad      _vea6  = *(_vcb7+_vco5)      \
 n4  est=2  IRLoad      _vea8  = *(_vcb9+_vco5)       |  COPY 0 -- no carried dep
 n5  est=2  IRBinOp     _vcu12 = _vco5 + 8            |
 n6  est=3  IRLoad      _vea14 = *(_vcb15+_vcu12)     |  COPY 1 -- no carried dep
 n7  est=3  IRLoad      _vea16 = *(_vcb17+_vcu12)     |
 n8  est=0  IRLoad      _vcv21 = *(_vcl20+0)         <-- IV load  (copy 3)
 n9  est=3  IRVecArith  _ver10 = $v + ($vi16) _vea6 _vea8
 n10 est=4  IRVecArith  _ver18 = $v + ($vi16) _vea14 _vea16
 n11 est=4  IRStore     *(_vcs11+_vco5)  = _ver10
 n12 est=1  IRBinOp     _vcn22 = _vcv21 + 8          <-- IV increment
 n13 est=5  IRStore     *(_vcs19+_vcu12) = _ver18
 n14 est=2  IRStore     *(_vct23+0) = _vcn22         <-- IV STORE
```

20 loop-carried edges, but **only 4 carry latency** (the rest are anti/output
edges with weight 0, which can never constrain a modulo schedule):

```
   n14 --> n0    lat=1  distance=1     IV store  -> IV load
   n14 --> n1    lat=1  distance=1     IV store  -> IV load
   n14 --> n8    lat=1  distance=1     IV store  -> IV load
   n13 --> n11   lat=1  distance=1     store     -> store (memory ordering)

   binding recurrence:  n8 --(1)--> n12 --(1)--> n14 --(carried,1)--> n8
                        RecMII = 3
```

**The vector dataflow has no loop-carried dependence whatsoever.** `n3…n11` —
loads, vector adds, stores — is a pure feed-forward chain. The only recurrence is
the induction variable, and it is recurrent *because it lives in a memory slot*:
loaded three times (`_vcl1`, `_vcl3`, `_vcl20`), incremented, stored back.

That is the same pattern R2.6 addressed for scalar loops with loop register
promotion; here the compact vector loop's shared IV slot has not been promoted.

**Operations from iteration i+1 can therefore begin before iteration i
completes** — nothing but the IV update stands between them.

### 4b. reduction vi32 — the recurrence is an accumulator chain unrolling created

Shipped loop body, 36 operations. The 8 unrolled copies each do
`load → $vreduce → add into _lr103`, and **all eight add into the same
register, serially**:

```
 n10 est=2  _lr103 = _lr103 + _vred10     \
 n13 est=3  _lr103 = _lr103 + _vred19      |
 n16 est=4  _lr103 = _lr103 + _vred28      |  ONE accumulator,
 n18 est=5  _lr103 = _lr103 + _vred37      |  eight serialized adds:
 n21 est=6  _lr103 = _lr103 + _vred46      |  est climbs 2,3,4,5,6,7,8,9
 n23 est=7  _lr103 = _lr103 + _vred55      |
 n24 est=8  _lr103 = _lr103 + _vred64      |
 n27 est=9  _lr103 = _lr103 + _vred73     /
```

38 latency-carrying carried edges, every one of them between two of those adds.
**RecMII = 8 — exactly the number of serialized accumulate operations.** The
loads and `$vreduce`s are all at est 0–1 and are fully parallel; the entire
critical path is the accumulator.

This recurrence was *created by unrolling*. At 1× the chain is one add.

### 4c. Recurrence distances

`build_kernel` assigns **distance 1 to every carried edge, conservatively**
(`# distance 1 (conservative)`). Real distances may be larger, which would lower
RecMII. **Every projection below is therefore a lower bound on the achievable
gain**, not an optimistic one.

## 5. Overlap opportunity and projected gains

Bundle units. "shipped" is the measured mcode loop body, which R6.5 proved is
already optimally packed — so this compares against a genuinely optimal
non-pipelined schedule, not a strawman.

| kernel | ops | mem | shipped B/iter | ResMII | RecMII | **MII** | **gain/iter** | class |
|---|---|---|---|---|---|---|---|---|
| elementwise vi16 | 15 | 10 | 7 | 3 | 3 | **3** | **−57%** | 3 high |
| axpy vi16 | 11 | 7 | 7 | 2 | 4 | **4** | **−43%** | 3 high |
| reduction vi32 | 36 | 11 | 10 | 5 | 8 | **8** | **−20%** | 2 limited |
| GEMM / conv3 / dot | — | — | — | — | — | — | — | 1 none |

Cycle-unit figures with the full latency model, for reference only: elementwise
crit-path 11, MII 6; axpy 12 / 8; reduction 14 / 8.

For elementwise, **RecMII == ResMII == 3**: once pipelined the loop is at its
resource bound, so 3 bundles/iteration is the floor for *any* transformation, and
SWP reaches it.

## 6. Whole-program projection — and why the benchmark numbers mislead

Applying the per-iteration gains with prologue/epilogue cost
(`stages−1` extra kernel copies) at the trip counts these benchmarks actually run:

| kernel | trip | loop now | loop pipelined | + pro/epi | net saved | program dyn bundles | **program gain** |
|---|---|---|---|---|---|---|---|
| elementwise | 8 | 56 | 24 | 3 | 29 | 1084 | **2.7%** |
| axpy | 16 | 112 | 64 | 4 | 44 | 1108 | **4.0%** |
| reduction | 4 | 40 | 32 | 8 | **0** | 470 | **0.0%** |

**Reduction's entire gain is consumed by the prologue and epilogue** at a trip
count of 4.

This is the same denominator trap R6.5 documented: these programs spend most of
their bundles in *scalar initialisation loops*, and the vector loop is only
5–10% of dynamic bundles. **No loop optimization can exceed that share.** It is a
property of the verification harness, not of software pipelining.

Scaling the vector work confirms it — the program gain converges on the loop gain
as the loop's share grows:

| N | repeats | loop share of dynamic bundles | loop gain | **program gain** |
|---|---|---|---|---|
| 64 | 1 | 5.1% | 57% | 2.9% |
| 256 | 8 | 26.4% | 57% | **15.1%** |
| 256 | 64 | 54.4% | 62% | **33.4%** |

(64×8 and 64×64 are omitted: the vector loop stops being eligible when nested
under a repeat loop at that size — worth understanding before relying on it.)

**So the honest projection is a range, not a number: ~3% on the current
benchmarks, 15–33% on workloads where the vector loop actually dominates.**

## 7. The finding that matters most: unrolling has already spent this opportunity

R6.4.1's adaptive unrolling and vector SWP are in direct competition:

* it **fully unrolls 3 of 6 kernels**, deleting the loop SWP needs;
* where a loop survives it cuts the trip count to **4–16**, so prologue/epilogue
  overhead is barely amortised — and for reduction, not at all;
* for reduction it **creates** the recurrence that then limits SWP (§4b).

Unrolling and pipelining are alternative ways to exploit the same cross-iteration
parallelism, and unrolling got there first. Any SWP implementation must be
evaluated *jointly* with the unroll factor, not on top of the current selection.

## 8. A cheaper win than SWP for reductions

The reduction recurrence is a chain of eight adds into one register (§4b).
Integer addition is associative — including on wrap-around two's-complement — so
using **partial accumulators** (accumulator expansion) is exact for every packed
integer marker:

This was **modelled, not assumed**: the same `min_ii` was re-run on the same
kernel with the carried edges *between distinct accumulate operations* removed
(each operation keeps its own self-recurrence), which is exactly what giving each
unrolled copy its own accumulator does.

| reduction vi32 | RecMII | ResMII | MII | bundles/iter | vs shipped |
|---|---|---|---|---|---|
| today | 8 | 5 | 8 | 10 | — |
| software pipelining | 8 | 5 | 8 | 8 | −20% |
| **partial accumulators** | **3** | 5 | **5** | **5** | **−50%** |

The recurrence stops binding entirely — ResMII (5) takes over, so 5
bundles/iteration is the floor and accumulator expansion reaches it.

**Accumulator expansion is 2.5× the gain of SWP, needs no prologue or epilogue,
and no modulo scheduler.** It is not exact for `vf32_t`, which must be excluded.

## 9. Recommendation

1. **Do accumulator expansion first** (reduction, dot). Bigger measured
   opportunity than SWP, far less machinery, and it removes a recurrence
   unrolling introduced. Restrict to integer markers.
2. **Then implement vector SWP for elementwise and axpy only** — the two class-3
   kernels, projected −57% and −43% bundles per iteration. Lifting the blocklist
   in `_UNSUPPORTED_KERNEL` is the entry point; the cost model needs no change.
3. **Re-tune the unroll factor jointly with SWP** (§7). Do not layer SWP on top
   of the current adaptive selection — for the kernels that matter the selector
   currently unrolls the loop out of existence.
4. **Do not pursue GEMM, convolution or dot via SWP.** No loop exists; GEMM
   additionally needs a counted IV.
5. **Gate on trip count.** At trip ≤ 4 the prologue/epilogue cancels the entire
   gain (reduction, §6). Any implementation needs a profitability test on trip
   count, and the existing R3.1 `production_swp` gates are the place for it.

**Is vector SWP worthwhile? Yes, but narrowly** — for two kernels, worth 43–57%
of their loop bundles, contingent on retuning unrolling, and it should not be the
next thing built.

## 10. Threats to validity

* **Projections are bounds, not measurements.** MII is a lower bound on the
  achievable II; no modulo schedule was constructed or verified, so a real
  implementation may not reach it. Register pressure in particular is not
  modelled, and SWP is known to raise it (R2.6/R2.7 hit exactly this).
* **Carried distances are conservatively 1**, so RecMII may be overstated and the
  gains understated (§4c).
* **Prologue/epilogue cost is estimated** as `(stages−1) × MII` with
  `stages = ⌈critpath/MII⌉`; a real implementation may differ.
* **Bundle units assume the bundler's hazard semantics**, where a dependence
  costs one bundle rather than a full latency. Cycle-unit figures are given
  alongside; ticks were not measured for any projected variant, because none was
  built.
* **One marker per family.** vi16/vi32/vi8 were analysed; the other packed
  markers were not, and lane count changes ResMII.
* The scaling experiment in §6 uses a synthetic repeat loop, not a real workload.

## 11. Regression

No compiler source changed, so behaviour is HEAD's. Confirmed unchanged:

| check | result |
|---|---|
| compiler source modified | **none** (analysis ran with an in-memory blocklist relaxation only) |
| simulator verification | 38/38 PASS at `92c9026` |
| unit suites (all 15) | all pass |
| `loopopt/pipeline_crosscheck` | PASS |

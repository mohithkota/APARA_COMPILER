# R2.3 Delivery Report — Dependence-Aware IR Scheduler

**Milestone:** R2.3 (the first optimisation that consumes the DependenceGraph;
reorders IR within basic blocks, semantics-preserving).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-25

> Reorders IR *within basic blocks only*. Never schedules across blocks, never
> redesigns the DependenceGraph or the bundler, implements no software-pipelining
> / modulo / trace / superblock scheduling, does not touch `LoopUnroll`. Built as a
> standalone, fully-validated pass (like M6–M9 before their M10 integration) and
> **not** wired into the production `compiler.py`, so shipped compiler output stays
> frozen — the pipeline cross-check remains 124/124 identical, 0 rollbacks.

---

## 1. Files added
| File | Purpose |
|---|---|
| `loopopt/schedule.py` | The scheduler. `schedule_module(instrs)` reorders each function's basic blocks via critical-path list scheduling over the R2.2 DependenceGraph, guarded by a topological self-verifier + the `ir_interp` differential oracle with per-function rollback. Also `schedule_function` / `schedule_function_order`, `ScheduleStats`. |
| `loopopt/_r2_3_test.py` | Unit suite (34 checks): independent-chain interleaving, chain preservation, memory ordering, loop-carried reduction, SCC region, determinism, differential semantics batch, module-structure regression. |
| `loopopt/schedule_corpus.py` | Corpus evaluation + R2.2-vs-R2.3 measurement (both bundler-scheduler modes). |
| `loopopt/R2_3_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `loopopt/__init__.py` | Additive R2.3 exports only. |

No other file is touched. `depgraph.py`, `depgraph_disambig.py`, the bundler,
codegen, `compiler.py`, the M5 framework, and every pass are unchanged.

## 3. Existing infrastructure reused (nothing re-derived)
| Reused | For |
|---|---|
| **R2.1** `DependenceGraph` (nodes, edges, `cfg.blocks`, carried flag) | the block partition and every ordering constraint |
| **R2.2** `MemoryDisambiguator` | memory-precise edges → more scheduling freedom (fewer false memory constraints) |
| `ir_interp.differential` (R1.x oracle) | the per-function correctness gate (re-executes before/after, compares result + memory) |
| `ir_utils.func_slices` | per-function scoping |

## 4. Scheduler algorithm
For each function, for each CFG basic block:
1. **Pin** the block's leading markers (`IRLabel` / `IRFuncBegin`) to the front and its trailing control/marker (`IRJump` / `IRCondJump` / `IRReturn` / `IRHalt` / `IRFuncEnd`) to the end; the instructions between them are *schedulable*.
2. **Constraints** = the non-carried DependenceGraph edges (`RAW`/`WAR`/`WAW`/`MEM_*`/`CONTROL`) whose both endpoints are schedulable. Every such edge runs low→high program index, so the constraint graph is a DAG and the original order is one valid topological order.
3. **Dependency height** (critical path) for each node: `height(n) = 1 + max(height(succ))`, unit latency, computed in decreasing index order.
4. **List schedule**: repeatedly place the ready node (all predecessors placed) of greatest height; ties → smallest original index.
5. Reassemble the block as `pinned-front · scheduled-middle · pinned-tail`; block boundaries and inter-block order are fixed.

**Legality (why it is semantics-preserving).** A basic block executes straight-line on every entry, so any topological order of its single-execution dependence DAG is behaviourally equivalent. Cross-block edges are preserved because block membership/order is fixed. **Loop-carried edges are respected by construction**: an intra-block reorder can never move an instruction across the back-edge, so whole-iteration-before-whole-iteration ordering holds automatically — and they must *not* be added as intra-block constraints, since a carried edge `j→i` (j>i) would cycle with its intra-iteration partner `i→j` and wrongly forbid all schedules. (Using carried edges to move work across iterations is software pipelining — out of scope.)

## 5. Priority heuristic
**Deterministic critical-path / dependency-height list scheduling.** Scheduling the tallest dependence chains first starts long-latency producers early and *interleaves independent chains*, so mutually-independent instructions land adjacently — exactly what the greedy VLIW bundler needs to fill a bundle. The smallest-original-index tie-break makes the output a pure function of the input (verified: identical across repeated runs). Unit latency keeps it register-model-free; a latency-weighted height is future work.

## 6. Validation methodology
- **Structural proof** — the schedule is a topological order of the conservative dependence DAG; an internal `_verify_order` asserts every non-carried intra-block edge keeps src before dst (0 failures across the corpus).
- **Differential oracle** — `ir_interp.differential` re-executes each function before/after from identical memory and compares return value + memory; any `mismatch` rolls that function back to its original order. `unsupported` functions (calls/floats the interpreter can't run) keep their schedule — it is legal by construction.
- **End-to-end** — every scheduled program is compiled + bundled through the production CodeGen + bundler; spills and bundle counts are measured.
- **Regression** — instruction multiset and function-slice layout are preserved; the production pipeline cross-check is unaffected (the pass is not wired in).

## 7. Test summary
```
_r2_3_test.py ......................... ALL R2.3 UNIT TESTS PASS   (34/34 checks)
_r2_1_test.py / _r2_2_test.py ......... PASS (unchanged)
pipeline_crosscheck.py ................ 124/124 identical, 0 rollbacks (unchanged)
```

## 8. Corpus validation
```
R2.3 DEPENDENCE-AWARE IR SCHEDULER -- CORPUS EVALUATION
  programs analysed          : 124
  functions / changed        : 194 / 128
  basic blocks scheduled     : 1064
  blocks reordered           : 349
  instructions reordered     : 2591
  verifier failures (struct) : 0
  rollbacks (differential)   : 0
  compilation failures       : 0
  behaviour mismatches       : 0
  differential verified      : 119   (unsupported/legal-by-construction: 75)
  RESULT: PASS
```

## 9. Performance comparison (R2.2 baseline vs R2.3 scheduled)
```
  static instructions        : 11885 -> 11909   (+24; codegen reg-alloc, spills 0->0)
  -- bundler scheduler ON  (production default) --
     bundles                 : 6498  -> 6218    (-280,  -4.3%)
     IPB                     : 1.829 -> 1.915    (+0.086, +4.7%)
  -- bundler scheduler OFF (isolates the IR scheduler) --
     bundles                 : 7858  -> 7102    (-756,  -9.6%)
     IPB                     : 1.512 -> 1.677    (+0.165, +10.9%)
  -- structural context (scheduler-invariant) --
     avg dependency height   : 4.42 steps        avg schedule length : 8.82 instrs/block
```
**Discussion.** The bundler already list-schedules at the assembly level, so the
"OFF" mode isolates the IR scheduler's own contribution: **−9.6% bundles / +10.9%
IPB** — the critical-path order packs markedly denser than raw codegen order.
Even in the production "ON" configuration the IR scheduler is a **net win**
(**−280 bundles, IPB 1.829→1.915**): a better instruction order improves codegen
register allocation and gives the bundler's greedy packer a better starting
point. Static instruction count rises a negligible +24 (a few reg-alloc moves)
with **no new spills** (0→0). No program regressed into a compile failure or a
behaviour mismatch. This is the first R2 milestone to move IPB (the R1.x unroll
work was IPB-flat), because scheduling directly targets the ILP-exposure
bottleneck the M11 evaluation identified.

Example (`int f(int a,int b,int c,int d){int x=a*b,y=c*d; return x+y;}`): the two
independent `a*b` / `c*d` chains are interleaved — the four address computes are
hoisted together, then the four loads, then the two multiplies — so the bundler
can pack the independent operations into shared bundles. `differential == match`.

## 10. Remaining work before R2.4
- **R2.3 is done and frozen.** The scheduler is a standalone, validated pass; it is deliberately **not** wired into the production pipeline (shipped output unchanged). Production integration (mirroring M10's approach for the migrated loop passes) is a candidate follow-up.
- Heuristic refinements (all safe, precision/quality only): latency-weighted height (the ISA has multi-cycle ops), a register-pressure-aware tie-break to shrink live ranges, and consuming the schedule the bundler is *given* rather than letting it re-derive one.
- Not done (by design): no cross-block / global scheduling, no software pipelining, no modulo scheduling, no trace/superblock scheduling, no `LoopUnroll` change, no bundler/graph redesign. R2.4 not started.

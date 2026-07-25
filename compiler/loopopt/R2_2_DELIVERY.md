# R2.2 Delivery Report — Memory Dependence Disambiguation

**Milestone:** R2.2 (analysis-only; improve the *precision* of the R2.1
DependenceGraph's memory edges — nothing else).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-25

> Improves precision ONLY. No IR change, no scheduling change, no generated
> assembly change, no bundling change, no optimisation-choice change, 0 rollbacks.
> The DependenceGraph, `LoopTransform`, the bundler and `LoopUnroll` are frozen and
> untouched in substance; R2.2 plugs into the graph through the optional
> `disambiguator=` hook and is consumed by nothing in the production pipeline.
> **A dependence is removed only when provably safe** — everything else stays
> conservative.

---

## 1. Files added
| File | Purpose |
|---|---|
| `loopopt/depgraph_disambig.py` | `MemoryDisambiguator` (+ `Verdict`). Per-function memory disambiguation reusing M1/M2/DefUse. `classify(i, j, carried)` returns disjoint / proven / conservative for a memory pair in the intra (same-iteration) or carried (cross-iteration) relation. Helpers `build_disambiguated_function_graphs()`, `disambiguate_function()`. |
| `loopopt/_r2_2_test.py` | R2.2 unit suite (39 checks): affine self-index, constant offsets, IV+clean-slot, disjoint stack slots, aliasing pointers, conservative fallbacks, preserved loop-carried, repeated identical access, and structural soundness/regression. |
| `loopopt/depgraph_r22_corpus.py` | Corpus validator + R2.1-vs-R2.2 measurement: per-function soundness (subset-only), edge-reduction stats, reason breakdown, proven/conservative split. |
| `loopopt/R2_2_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change (all additive / backward-compatible) |
|---|---|
| `loopopt/depgraph.py` | `DepEdge` gains optional `proven` / `reason` slots (default False/None; repr unchanged when unset). `DependenceGraph.__init__` gains optional `disambiguator=` + `eliminated_memory_edges` / `eliminated` counters. `_build_memory_edges` now emits the intra and carried memory edges inline so a disambiguator can veto or tag each — **the no-disambiguator path is byte-identical to R2.1** (verified: corpus memory-edge count still exactly 13019). New query helpers `memory_edges()` / `proven_memory_edges()` / `conservative_memory_edges()` / `register_edges()`. No existing behaviour or symbol changed. |
| `loopopt/__init__.py` | Additive R2.2 exports only. |

The R2.1 graph's default output, its API, and all R2.1 tests are unchanged.

## 3. Existing analyses reused (nothing re-derived)
| Reused | From | Used for |
|---|---|---|
| `basic_ivs` (slot→step), `iv_terms` (temp→(iv_slot, scale)) | **M1** `analysis_iv` | affine index recognition + stride/step comparison |
| `aliasing_summary.clean_slots` (escape analysis), `written_keys` | **M2** `analysis_mem` | clean-slot disjointness + loop-invariant base-pointer test |
| `_access_key` + `AliasSummary.may_alias` | **M2** (already used by R2.1) | the base oracle that produces the candidate pairs |
| `DefUse.single_defs` | DefUse | base-pointer / offset origin resolution |
| `discover` / `discover_function` | M0 | per-function loop descriptors carrying the M1/M2 facts |
| `func_slices` | `ir_utils` | per-function scoping |

## 4. Disambiguation algorithms (each provably safe under the compiler's existing memory model)
1. **Clean-slot.** A clean local stack slot (address never escaped — M2 `clean_slots`) cannot be aliased by any computed pointer, global access, or call → disjoint. This is M2's documented model.
2. **Distinct local objects.** Two computed accesses based at the addresses of two *different* local slots (`&a` vs `&b`) address non-overlapping frame regions → disjoint (same non-overlapping-slot assumption M2 makes).
3. **Same-base SIV.** Two accesses through the *same* base value (same stack object address, same global object, or the same loop-invariant pointer slot) whose byte offsets are affine in the *same* induction variable with *equal* scale:
   - *intra* (same IV value): disjoint iff the constant parts differ; else must-alias.
   - *carried* (distinct iterations, IV values differing by a nonzero multiple of `step`): a cross-iteration conflict exists iff the constant difference is a nonzero multiple of the true byte stride `scale·step`; otherwise disjoint. In particular **`a[i]` vs `a[i]` has no loop-carried conflict** (only intra). Requires a known nonzero IV step (else conservative — a step-0 IV would make `a[i]` the same element every iteration).
   - constant offsets are the `scale == 0` special case (iteration-independent): disjoint iff the constants differ, else must-alias.

Every pair not covered stays **conservative**. Kept edges proved to be genuine (must-alias) are tagged `proven`.

## 5. Validation methodology
1. **Unit** (`_r2_2_test.py`) — asserts each rule fires on a matching fixture, that genuine dependences (aliasing pointers, global accumulator recurrence) are *kept*, and — in every test — the structural **soundness invariant**: R2.2's memory edges are a subset of R2.1's (same src/dst/kind/carried) and its register/control edges are identical (R2.2 only ever *drops* a memory edge).
2. **Corpus** (`depgraph_r22_corpus.py`) — the same subset/identical-register-control soundness check on **every** function of the full corpus, plus `validate()==[]`, 0 IR mutations, and 124/124 compile+bundle through the production CodeGen+bundler.
3. **Invariance** — R2.1's default graph is byte-identical (memory-edge count unchanged at 13019); `pipeline_crosscheck.py` remains 124/124 identical IR/code/tier with 0 rollbacks. Because neither graph mutates IR and nothing in the pipeline consumes them, generated assembly/bundles are unchanged by construction.

## 6. Test summary
```
_r2_1_test.py ......................... ALL R2.1 UNIT TESTS PASS   (40/40, unchanged)
_r2_2_test.py ......................... ALL R2.2 UNIT TESTS PASS   (39/39 checks)
```

## 7. Corpus validation
```
R2.2 MEMORY DISAMBIGUATION -- CORPUS VALIDATION + R2.1 vs R2.2
  programs / functions        : 124 / 194
  subset violations           : 0        register/control edge diffs : 0
  validate() failures         : 0        IR mutations                : 0
  programs compiled+bundled   : 124 (fail 0)
  RESULT: PASS (sound: subset-only, register/control identical, 0 validate/mutation)
```

## 8. Edge reduction statistics (R2.1 → R2.2)
```
  total edges                 : 23782 -> 19034
  register edges              : 6156  -> 6156   (identical, untouched)
  memory edges                : 13019 -> 8271   (4748 eliminated, 36.5%)
  loop-carried edges (all)    : 2544  -> 1977   (567 false carried edges pruned)
  surviving memory: proven (must-alias) 2088 | conservative (may-alias) 6183

  eliminated by reason                proven memory edges by reason
    clean-slot-vs-global    : 2691      same-stack-slot     : 2014
    clean-slot-vs-computed  : 1375      same-const-address  :   70
    clean-slot-vs-call      :  449      same-affine-address :    4
    distinct-const-offset   :  162
    distinct-local-objects  :   69
    siv-self-index-no-carry :    2
```
The dominant win is the clean-slot family: local scalars/arrays whose address
never escapes were being conservatively tied to every global access, computed
pointer, and call. Removing those is exactly M2's documented model. `distinct-
const-offset` and `distinct-local-objects` add array-level precision; the SIV
self-index rule removes provably-absent loop-carried self-array dependences.

## 9. Representative before/after (`sumn`: `for(i…) s += p[i];`)
Node 17 is the `p[i]` computed load; `s` lives in clean slot −24, `i` in clean
slot −32. R2.2 eliminates exactly the 6 edges tying the computed load to those
clean locals — **including two false loop-carried edges** — because a parameter
pointer `p` provably cannot point at the function's own non-escaping locals:
```
  ELIMINATED (6):
     2->17  MEM_RAW           [clean-slot-vs-computed]   # s-init  vs p[i]
     4->17  MEM_RAW           [clean-slot-vs-computed]   # i-init  vs p[i]
    17->22  MEM_WAR           [clean-slot-vs-computed]   # p[i]    vs s-store
    22->17  MEM_RAW carried   [clean-slot-vs-computed]   # s-store -> next p[i]   (false carry)
    17->29  MEM_WAR           [clean-slot-vs-computed]   # p[i]    vs i-store
    29->17  MEM_RAW carried   [clean-slot-vs-computed]   # i-store -> next p[i]   (false carry)
```
All genuine recurrences survive and are now `proven`:
```
    29->7,  29->15, 29->25   MEM_RAW carried [proven:same-stack-slot]   # i recurrence
    22->19                   MEM_RAW carried [proven:same-stack-slot]   # s (accumulator) recurrence
```
Memory edges on `sumn`: **22 → 16**. For two distinct local arrays
(`a[i]=i; s+=b[i];`) R2.2 additionally removes the `a[i]`↔`b[i]` edges as
`distinct-local-objects`; for `a[i]=a[i]+1` it removes the loop-carried
`a[i]`↔`a[i]` edge as `siv-self-index-no-carry` while keeping the intra access
`proven`.

## 10. Remaining work before R2.3
- **R2.2 is done and frozen.** Disambiguation is precision-only; no pass consumes the refined graph yet (by design).
- Precision left on the table (all *safe* — kept conservative): affine offsets with a `±const` inside the index (`a[i+1]`) are only partially recognised (M1's `iv_terms` captures `iv·scale`, not `(iv±c)·scale`); MIV / different-scale pairs, and two *different* base pointers that may alias, stay conservative. Sharpening these (a fuller GCD/Banerjee SIV/MIV test, symbolic base-pointer disambiguation) is future work, and would only *prune* more edges — never mutate IR.
- Not done (by design): no scheduling, no critical-path analysis, no software pipelining, no modulo scheduling, no change to `LoopUnroll`, the bundler, or any compiler output. R2.3 not started.

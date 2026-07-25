# R2.1 Delivery Report — Reusable IR Dependence Graph Infrastructure

**Milestone:** R2.1 (analysis-only infrastructure; the standard dependency
representation for every future scheduling / restructuring pass).
**Status:** ✅ COMPLETE & VERIFIED · **Date:** 2026-07-25

> Adds reusable analysis ONLY. Generated IR, generated assembly, bundling, tier
> selection and every other compiler behaviour are unchanged. The bundler is not
> touched, redesigned, or replaced; no list scheduling / software pipelining /
> modulo scheduling is implemented; `LoopUnroll` is untouched.

---

## 1. Files added
| File | Purpose |
|---|---|
| `loopopt/depgraph.py` | The `DependenceGraph` (+ `DepNode`, `DepEdge`). Reusable dependence graph over IR instructions: register RAW/WAR/WAW, memory RAW/WAR/WAW (via the M2 alias classifier + oracle), minimal control ordering, and loop-carried recurrence edges (via LoopInfo) held **separately** from intra-iteration edges. Full query surface: nodes/edges, predecessor/successor queries, edge iteration, Tarjan SCCs, recurrence extraction, topological order over the acyclic region, `validate()`, `dump()`, `to_dot()`. Module helpers `build_dependence_graph()` / `build_function_graphs()`. |
| `loopopt/_r2_1_test.py` | Unit suite (40 checks): RAW/WAR/WAW, memory edges, alias disjointness (stack slots + global objects), call barriers, independent instructions, SCC detection, loop-carried edges, topological order, graph self-consistency, no-mutation regression, whole-module helper. |
| `loopopt/depgraph_corpus.py` | Corpus validator: builds a graph over **every function** of the corpus, asserts zero IR mutation, runs `validate()` everywhere, and compiles+bundles every program through the production `CodeGen`+`bundler`. |
| `loopopt/R2_1_DELIVERY.md` | This report. |

## 2. Files modified
| File | Change |
|---|---|
| `loopopt/__init__.py` | **Additive export only.** Adds one `from .depgraph import …` line and the corresponding `__all__` entries. No existing symbol changed. |

Nothing else is touched. `bundler.py`, `codegen.py`, `compiler.py`, the M5
framework, legality, all `LoopUnroll` tiers, and every optimization pass are
frozen and unchanged.

## 3. Existing analyses reused (nothing duplicated)
The module builds **no** analysis of its own; it composes existing ones:

| Reused | From | Used for |
|---|---|---|
| `dest_names` / `src_names` / `jump_targets` / `func_slices` | `ir_utils` | the shared def/use/branch/slice primitives |
| `DefUse` (`def_sites`, `use_sites`, `single_defs`) | `analysis.defuse` | register dependence endpoints; the single-def `IRLoadAddr` slot map feeding memory keys |
| `build_cfg` / `CFG` (blocks, succs/preds) | `analysis.cfg` | basic blocks, the instruction→block map, and block reachability (the may-precede relation) |
| `compute_dominators` / `Dominators` | `analysis.dominators` | dominance (loop-header attribution / clean integration) |
| `build_loop_info` / `LoopInfo` (bodies, headers, back-edges) | `analysis.loopinfo` | the **sole** source of loop-carried classification and recurrence-loop attribution |
| `analysis_mem._access_key` | `loopopt.analysis_mem` (M2) | the alias-key **classifier** (stack slot / global object / computed pointer) — reused verbatim so memory keys match the loop framework exactly |
| `AliasSummary.may_alias` | `loopopt.analysis_mem` (M2) | the conservative alias **oracle** (mirrors the bundler's `_mem_may_alias`: two keys may alias unless provably disjoint) |

The only new logic is edge construction and the graph data structure / query API.

## 4. Graph architecture

**Scope.** One graph per **function slice** (temp names and block ids restart per
function — `ir_utils`). Nodes are IR instruction indices; edges are directed
"must be ordered" constraints from an earlier producer to a later consumer.

**may-precede backbone.** For instructions *i*, *j*:
- same block → *i* precedes *j* iff `index(i) < index(j)`; the reverse is possible
  **only** around a back edge, i.e. when the block lies on a cycle
  (`block ∈ reach[block]`). *(Getting this wrong silently drops every loop-carried
  dependence internal to a single-block loop body — e.g. an accumulator's store
  feeding the next iteration's load. It is handled explicitly.)*
- different blocks → *i* may precede *j* iff `block(j)` is reachable from
  `block(i)` in the CFG (transitive closure over successor edges, which already
  include loop back-edges). Two mutually-unreachable blocks (if/else arms) yield
  **no** edge — nothing forces an ordering between instructions that never both
  execute on one path.

**Edge kinds.**
| Kind | Meaning |
|---|---|
| `RAW` / `WAR` / `WAW` | register flow / anti / output on a shared temp name |
| `MEM_RAW` / `MEM_WAR` / `MEM_WAW` | memory flow / anti / output on aliasing accesses |
| `CONTROL` | ordering constraint (a block's terminator is pinned last) |

**Memory model.** Each memory op is classified with the M2 `_access_key`
(stack-slot / global-object / computed-pointer) and paired via
`AliasSummary.may_alias` (conservative: edge unless provably disjoint). A call /
indirect call is a conservative **barrier** — modelled with `key=None` (r+w) so
the oracle makes it conflict with every other memory op. (This is *more*
conservative than M2's clean-slot refinement, which is exactly what a dependence
graph wants: extra ordering edges are always safe.)

**Control.** Minimal by design: only the "terminator scheduled last" constraint
is represented (data + memory edges carry the rest; the frozen block-local
bundler owns final placement).

**Intra-iteration vs loop-carried (held separately).** For every conflicting
pair the forward constraint is an **intra** edge *E→L* (`carried=False`,
`src<dst`). When the CFG additionally lets the later instruction reach the
earlier one (a cycle) inside a common natural loop, a **carried** recurrence edge
*L→E* is added (`carried=True`, `src>dst`, tagged with the loop header from
`LoopInfo`). The two are never conflated: they carry a distinct flag and header,
and `carried_edges()` / `topo_order()` / `recurrences()` treat them accordingly.

**Invariant enforced by `validate()`.** Non-carried edge ⇒ `src < dst`; carried
edge ⇒ `src > dst` and a non-None loop header; endpoints in range; no self-loops;
succ/pred indices mirror the edge list exactly.

**Query surface (the standard API future passes consume).**
`node(i)`, `indices()`, `all_edges(kind, carried)`, `out_edges`/`in_edges`,
`successors`/`predecessors` (filterable by kind and carried), `num_nodes`/
`num_edges`, `carried_edges`, `sccs()` (iterative Tarjan), `recurrences()`
(non-trivial SCCs), `topo_order()` + `is_acyclic()` (over the non-carried
subgraph), `validate()`, `dump()`, `to_dot()`.

**Integration.** A future pass simply calls `DependenceGraph(instrs, lo, hi)` (or
`build_function_graphs(instrs)` for a whole module) and — if it already holds the
analyses — passes `cfg=/dom=/li=/du=` to reuse them instead of rebuilding. It
integrates cleanly with DefUse, LoopInfo, Dominators and the M2 memory analysis
by construction (they are its inputs).

## 5. Validation methodology
1. **Unit** (`_r2_1_test.py`, 40 checks) — every edge kind, alias disjointness,
   barriers, SCC/recurrence extraction, carried-edge separation, topological
   order, self-consistency, and a no-mutation regression check.
2. **Corpus robustness** (`depgraph_corpus.py`) — build a graph over every
   function of the full corpus; require 0 build errors, 0 `validate()` failures,
   and **0 IR mutations** (repr-equal snapshot before/after); compile+bundle
   every program through the production `CodeGen`+`bundler`.
3. **Pipeline invariance** (`pipeline_crosscheck.py`, run with the extended
   package) — the optimization pipeline still produces instruction-identical
   per-tier IR, identical generated code, identical selected tier, and 0
   rollbacks / 0 verifier failures, under **both** LICM gate states. Because the
   graph never mutates the IR and nothing in the pipeline consumes it, downstream
   assembly and bundles are unchanged by construction; the crosscheck confirms the
   pipeline is unperturbed by adding the module to the package.

## 6. Test summary
```
_r2_1_test.py ......................... ALL R2.1 UNIT TESTS PASS  (40/40 checks)
```

## 7. Corpus validation
```
R2.1 DEPENDENCEGRAPH -- CORPUS VALIDATION
  programs analysed            : 124
  functions (graphs attempted) : 194
  graphs built successfully    : 194
  graph build errors           : 0
  validate() failures          : 0
  IR mutations (must be 0)     : 0
  programs compiled+bundled    : 124   (compile failures: 0)
  total nodes                  : 7008
  total edges                  : 23782  (reg 6156 / mem 13019 / ctl 4607)
  loop-carried edges           : 2544
  recurrence SCCs              : 81
  RESULT: PASS

M10 PIPELINE CROSS-CHECK  (LICM gate OFF default AND ON):
  programs 124 · per-tier IR 124/124 · generated code 124/124 ·
  selected tier 124/124 · verifier failures 0 · rollbacks 0 · RESULT: PASS  (both)
```
"Identical IR / assembly / bundles / verifier results / optimisation choices /
zero rollbacks" holds: the pipeline crosscheck shows identical per-tier IR,
generated code and tier selection with 0 rollbacks; the corpus validator shows
0 IR mutations and 124/124 clean compile+bundle.

## 8. Example graph (`sumn` — a pointer-sum loop `for(i…) s += p[i];`)

Loop-carried recurrence edges the graph exposes (header block `B1`):
```
  31->9  MEM_RAW ('stack',-32) carried@B1   # i store (latch) -> next iter i load (header guard)
  31->17 MEM_RAW ('stack',-32) carried@B1   # i store -> next iter i load (address calc)
  24->21 MEM_RAW ('stack',-24) carried@B1   # s store  -> next iter s load  (the accumulator recurrence)
  24->19 MEM_RAW              carried@B1     # s store  -> next iter p[i] load (conservative alias)
  9->8   WAR _t3 carried@B1  ...             # reused address temps: next iter's def waits on this iter's use
  recurrence SCC: {8..12, 14..24, 26..31}    # whole loop body (pre-SSA temps are reused every iteration)
```
Intra-iteration flow within the body (same header/body, `carried=False`):
```
  17->18 RAW _t10   18->19 RAW _t11   19->22 RAW _t12   22->24 RAW _t15   ...
```
The accumulator recurrence `24→21` and the IV recurrences `31→9 / 31→17` are the
cross-iteration dependences a future software-pipelining pass must respect — they
are recorded distinctly from the intra-iteration edges, which is the whole point
of this milestone.

## 9. Remaining work before R2.2
- **R2.1 is done and frozen.** The graph is the reusable substrate only; no pass
  consumes it yet (by design).
- The current alias precision reuses M2's oracle conservatively (computed
  pointers alias everything; calls are full barriers). R2.2's **dependence
  disambiguation** is the place to sharpen this (subscript/stride tests on
  affine array accesses, reusing M1 induction-variable facts), which will *prune*
  memory edges — never add IR mutation.
- The pre-SSA temp reuse makes loop bodies collapse into one large recurrence SCC
  (register anti-dependences on reused temps). That is faithful to the current IR;
  a later scheduling pass will pair this graph with renaming/expansion to break
  the artificial anti-recurrences before pipelining.
- No changes to the bundler, scheduling, `LoopUnroll`, or any compiler output are
  planned or made here.

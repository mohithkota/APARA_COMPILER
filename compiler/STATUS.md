# APARA C Compiler — Project Status

---

## C FEATURE COVERAGE (reference table — updated 2026-07-17)

Backed by tests verified against gcc (`testing/feature_sweep/`, `testing/universal/`).
✅ supported · ⚠️ partial · ❌ not supported.

| Feature | Status | Notes |
|---|:---:|---|
| Integer types (char/short/int/long long, unsigned) | ✅ | sign/zero-extend per type |
| Arithmetic/logic/shift/comparison ops (all 12 ALU) | ✅ | incl. nand/nor/xnor |
| Compound assignment (+=,-=,*=,/=,<<=,|=,…) | ✅ | |
| Ternary ?:, comma operator, chained a=b=c | ✅ | |
| if/else, while, for, do-while | ✅ | |
| switch/case incl. fall-through, break/continue | ✅ | |
| Short-circuit && / \|\| (incl. loop conditions) | ✅ | u5 LICM fix |
| Functions, recursion, mutual recursion | ✅ | ≤4 args |
| 1D/2D/3D arrays (local+global, initialized) | ✅ | |
| struct (incl. nested), arrays of structs | ✅ | pts[i].field, p->field, (p+1)->field |
| union | ✅ | |
| bit-fields | ✅ | |
| Pointers (deref/arith/&x/p[i]/*p/cmp/q-p/stores/p++) | ✅ | uniform local & global |
| const | ✅ | |
| enum (incl. explicit values =5) | ✅ | |
| typedef (basic + anonymous-struct typedefs) | ✅ | |
| sizeof | ✅ | compile-time constant |
| static local variables | ✅ | persist across calls |
| goto / labels | ✅ | |
| designated initializers ({[2]=30}) | ✅ | |
| Vector intrinsics (__vadd/__dot/__vreduce_max, vu8_t) | ✅ | |
| Floating point (f32/f64: arith, cmp, casts, vars, arrays, params) | ✅ | fp01–07 vs gcc; gaps: struct float fields, float truthiness |
| Function pointers (assign, call, callbacks, tables, &f, ==, returns) | ✅ | compile-time linker pass; fn01–04 vs gcc |
| Variadic functions (va_start/va_arg/va_end, int/i64 args) | ✅ | stack-passed extras; va01–03 vs gcc; float va_args untested |
| >4 named args (5+/6/8-param fns, recursion, variadic combo) | ✅ | stack-passed args 5+; ma01–03 vs gcc |
| real strings | — | not needed for the APARA accelerator (address-of only, by design) |

Integer subset is functionally complete + verified universal (13/13 novel-algorithm
battery). FP and the ❌ items are the remaining work.

---

## 2026-07-25 — LOOP-OPT FRAMEWORK M12: next-phase research design (Latest)

DESIGN ONLY — nothing implemented; no existing code touched. Produced the
evidence-driven research plan for the next project, grounded entirely in M11.
Added `loopopt/m12_roadmap.html` (published Artifact); no .py/compiler change.

CENTRAL THESIS: the limiter is lack of INDEPENDENT WORK in each scheduling window,
not hardware/scheduler (ILP-saturation ~0%, reg peak 5/28). Win by EXPOSING
parallelism at the IR level and letting the existing greedy bundler pack it —
exactly what LoopTransform was built for. Framework split: Class A (IR
restructuring: unroll, SWP-as-IR, if-convert, unswitch, fusion/fission,
vectorize, reg-promote — framework-native, reuse M0–M4/M7 + MutationTransaction)
vs Class B (scheduler-level: global/trace/superblock, true modulo reservation —
needs new scheduler/bundler surgery, higher risk). Key move: emit an
already-interleaved SWP kernel as IR (Class A) using the MII M3 ALREADY computes,
avoiding a Class-B scheduler up front.

BOTTLENECK→TECHNIQUE: 58% true deps → software pipelining + unrolling; 21% memory
→ disambiguation + dependence graph; 20% control → if-conversion + global sched.
Better LOCAL scheduling ranked LAST (0% ILP-sat = no local headroom; only
work-creating or window-enlarging transforms help).

ROADMAP R1 loop unrolling (LOW effort, large, enabler) · R2 dependence graph +
memory disambiguation (foundational analysis) · R3 if-conversion/predication
($cmov) · R4 software pipelining / swing modulo scheduling (flagship, HIGH) · R5
global/superblock scheduling (Class B) · R6 vectorization ($v/$dot ISA). Each
milestone has objective/deps/deliverables/validation/success in the Artifact.

FIRST/SECOND/THIRD: unrolling → dependence-graph+disambiguation → software
pipelining. TOP RISK: new opts have NO legacy byte-diff oracle and the M5
verifier checks structure not data-flow — a differential-correctness harness
(gcc golden + simulation) is a prerequisite. Full M0–M12 test suite green (12
files, unchanged). M12 done — STOP; the framework project is complete.

## 2026-07-25 — LOOP-OPT FRAMEWORK M11: quantitative evaluation

EVALUATION ONLY — measures the frozen compiler, changes nothing (no pass/order/
heuristic/schedule/bundling/codegen touched). Added `loopopt/m11_eval.py`
(measurement harness), `loopopt/m11_report.py` (thesis-figure HTML generator),
`loopopt/m11_results.json`, `loopopt/m11_report.html`. No existing file modified.

Corpus: 124 C programs (44 loop-bearing, 79 loops). Metrics on the emitted machine
code via the bundler's own scheduler/packer + built-in split-reason instrumentation.

HEADLINE: mean IPB 1.81 (median 1.75, max 2.73) on the 8-lane VLIW ≈ 22% lane
occupancy; 57% of bundles use ONE lane, 29% use two. The limiter is DEPENDENCY
STRUCTURE, not hardware/bundler: of all bundle-split events 58.3% true-data/
recurrence deps, 21.0% memory deps (MemAlias/MemPhase), 20.2% control-flow
boundaries; hardware-lane 0.6% + ILP-saturation 0.0% ≈ nil. Max register-pressure
peak 5 of 28 → pressure is NOT binding. Dominant per-program bottleneck = true-data-
deps for all 124.

PASS CONTRIBUTION (ablation through identical cleanup; avg over affected progs):
IVSR −38% static / −40% bundles / +3.8% IPB (39 progs); LICM −34% / −36% / +4.3%
(44); Rotation is an enabling transform (adds guard/latch → +size standalone, not
in production). Production final over the 44 loop progs: +2.9% static, +7.2%
bundles, +4.8% IPB (static understates it — loop-reg/preheaders trade one-time
setup for per-iteration/dynamic savings a static count can't see).

FRAMEWORK OVERHEAD: constant-factor (IVSR 2.8×, LICM 5.7× vs legacy) but ABSOLUTE
negligible — sub-ms to ~1 ms/program vs mean compile 4.0 ms; 65 attempts, 46
commits, 0 rollbacks, 0 verifier failures corpus-wide. CORRECTNESS: pipeline
cross-check 124/124 IR+code+tier identical, 0/0/0. SCALING: small(<50) 0.8 ms avg,
medium 3.0, large(>200) 15.8 (max 77) — no super-linear blow-up.

FUTURE WORK (ranked by measured bottleneck, not speculation): 1 software
pipelining / modulo scheduling (58% deps + 22% occupancy); 2 stronger alias
analysis (21% mem deps); 3 global/cross-block scheduling (20% control); 4 loop
unrolling (57% single-lane bundles); 5 aggressive/speculative LICM (only paired
with #6); 6 enhanced register allocation (1 program spill-fell-back). Report +
auto-generated figures published as an Artifact. Full M0–M11 suite green (12 test
files). M11 done — do NOT begin M12.

## 2026-07-25 — LOOP-OPT FRAMEWORK M10: production pipeline integration

INTEGRATION milestone (no behaviour change): the migrated LoopTransform passes are
now the CANONICAL production execution path. compiler.py's loop-opt stage
previously called `ivsr.induction_strength_reduce` and `licm2.
loop_invariant_code_motion` directly; it now imports both names from
`loopopt.pipeline` (drop-in framework adapters). Every production loop
transformation runs through LoopTransform + MutationTransaction + framework
verification + rollback + shared analyses/legality/descriptors.

Pass order UNCHANGED (IVSR → strength-reduce → LICM → loop-reg tiers, then the
scalar-cleanup stage's opt-in LICM); no pass added/removed/reordered. The
adapters preserve exact legacy contracts: non-mutating on the caller's list;
`loop_invariant_code_motion` keeps licm2's APARA_LICM opt-in gate (default OFF =
no-op; ON = framework LICM, byte-identical per M8). The fresh-temp counter
`ivsr._iv_n` is NOT reset in the adapters (framework advances it identically to
legacy, so tier-to-tier temp numbering is unchanged).

OUT OF SCOPE (no framework migration yet, so unchanged legacy in the pipeline):
`licm.hoist_loop_invariants` (the older ad-hoc load/address LICM, distinct from
licm2) and `loop_reg.promote_loop_counters`. LoopRotation (M6) is a framework
transform but was never in the production pipeline, so M10 does NOT insert it
(that would be adding a pass). Legacy modules (ivsr.py, licm2.py, licm.py,
loop_reg.py) retained untouched as specs/regression refs.

Files added: `loopopt/pipeline.py` (drop-in adapters), `loopopt/pipeline_crosscheck.py`,
`loopopt/_m10_test.py`. Modified: `compiler.py` (2 import lines only — legacy
`from ivsr import ...` / `from licm2 import ...` → `from loopopt.pipeline import
...`). No framework file changed (M0–M9 frozen; move()/replace_span() already in place).

END-TO-END PIPELINE CROSS-CHECK (pipeline_crosscheck.py — reconstructs compiler.py's
6-tier loop-opt pipeline parameterised by the IVSR/LICM impls, runs legacy vs
framework per program, resets all three pass counters ivsr._iv_n/mem2reg._m2r_n/
loop_reg._lr_n before each build so numbering is comparable): 124/124 programs,
744 tiers — per-tier IR MATCH 124, generated-code MATCH 124, selected-tier MATCH
124, 0 IR/code/tier mismatches, 0 framework verifier failures, 0 rollbacks. PASSES
with the opt-in LICM gate BOTH OFF (default) and ON (APARA_LICM=1). Real end-to-end
`python3 compiler.py <file>` compiles clean through the integrated path (mincall,
d2_array_add: [OK], valid mcode). Full M0–M10 suite green (12 files); M8/M9
cross-checks still 124/124. STOP after M10 — do not begin M11.

## 2026-07-25 — LOOP-OPT FRAMEWORK M9: IVSR migrated onto the framework

Second pass MIGRATION onto the M5 framework. `LoopIVSR(LoopTransform)` reproduces
`ivsr.py` (Induction-Variable / pointer Strength Reduction) — maintain each
IV-linear address in ONE register initialized in a preheader and stepped by
`p += step`, replacing per-iteration multiply+offset+base-materialization —
byte-for-byte, running through the framework. Architectural consolidation, NOT an
optimization change: NO heuristic/profitability/legality changes, no new opt.
ivsr.py stays as the SPECIFICATION and A/B baseline.

REUSE over reimplementation (max fidelity, min risk): `ivsr._process_loop` is a
PURE planner (reads IR, RETURNS a new list, never mutates in place), so LoopIVSR
calls it DIRECTLY as its planner and routes the resulting region rewrite through
the framework as one reversible `replace_span`. Nothing is duplicated — IV
detection, invariance, decomposition, cloning, profitability AND the module-global
fresh-temp numbering (`ivsr._iv_n`) are all the spec's own code, so identical by
construction. `ivsr.dead_temp_elim` (whole-program cleanup, not a loop transform)
reused verbatim as the post-pass. Shared DefUse supplies single-def/multi-name.

The ONLY framework addition M9 needed: `MutationTransaction.replace_span(start,
end, new)` — inclusive-range slice replacement (grow/shrink/rewrite in one edit);
rollback is automatic via the saved order snapshot, same as splice(). (M8 added
move(); M9 adds replace_span(); framework otherwise untouched.)

Faithful control flow: `ivsr_module` mirrors `induction_strength_reduce` exactly —
fixpoint (<=200 rounds), each round re-enumerate loops smallest-region-first,
apply the FIRST that progresses, restart; then dead_temp_elim once. Enumeration
comes from the SHARED M0 DESCRIPTORS (discover_function): each loop's region =
(header-label index .. back-edge index), sorted by (size, e, s) — VERIFIED
identical to `_find_loops`'s order across the whole corpus (44 loop files, 79
regions, 0 multi-latch). This order is load-bearing: `_process_loop` bumps
`ivsr._iv_n` on every candidate-bearing attempt (incl. profitability rejects and
re-attempts), so preheader temp names depend on the exact attempt sequence; the
framework rolls back IR on no-op/verify-fail but never the counter, matching ivsr.

Files added: `loopopt/loop_ivsr.py` (LoopIVSR, IVSRReport, ivsr_module),
`loopopt/ivsr_crosscheck.py`, `loopopt/_m9_test.py`. Modified:
`loopopt/transform.py` (+replace_span()), `loopopt/__init__.py` (exports only).
ivsr.py NOT modified (spec frozen).

BEHAVIOURAL CROSS-CHECK (ivsr_crosscheck.py, whole corpus, ivsr vs M9 on
independent deep copies, `ivsr._iv_n` reset to 0 before each, compared by
per-instruction repr): 124/124 programs produce IDENTICAL IR, 0 MISMATCH, 17
loops strength-reduced, 0 framework verifier failures, 0 rollbacks. Full M0–M9
suite green (11 files). NOT wired into the compile pipeline (like M6/M8) — opt-in
via ivsr_module. STOP after M9 delivery — no loop_reg migration yet (M10, await
approval).

## 2026-07-25 — LOOP-OPT FRAMEWORK M8: LICM migrated onto the framework

First pass MIGRATION onto the M5 framework. `LoopLICM(LoopTransform)` reproduces
`licm2.py`'s conservative loop-invariant code motion — hoist PURE, single-dest,
non-memory, non-float computations to the existing preheader — but runs entirely
through the framework: every edit goes through MutationTransaction, the framework
owns rebuild/verify/rollback/stats, and discovery/nesting come from the shared M0
descriptors. It is an architectural consolidation, NOT a new optimization; memory
hoisting / aliasing / PRE / speculation / edge-splitting stay out of scope exactly
as in licm2. licm2.py stays as the SPECIFICATION and A/B baseline (APARA_LICM).

The ONLY framework addition M8 needed: `MutationTransaction.move(src, dst)` — an
additive relocation primitive (pop src / insert dst, dst<=src, matching licm2's
`del; insert` idiom). Because it only reorders existing objects, the txn's saved
order snapshot already rolls it back exactly (no new bookkeeping).

Faithful reproduction: `LoopLICM.run()` applies AT MOST ONE hoist per attempt
(licm2's exact invariant-fixpoint + per-instruction whitelist/legality order),
and `licm_module()` drives the outer fixpoint with licm2's IDENTICAL loop ordering
(stable sort on -max(depth over body blocks)) and restart-after-each-hoist — so
the decision sequence, and the final IR, match instruction-for-instruction. The
per-instruction whitelist (_HOIST_KINDS/_is_float/_TERMS) is copied verbatim from
licm2; loop-level legality (unique preheader that dominates the header) is the
shared M7 `has_unique_preheader` + a dom check. LICM legality is per-INSTRUCTION,
so it lives in the pass (M7 predicates are loop-level) — no design change.

Files added: `loopopt/loop_licm.py` (LoopLICM, LICMReport, licm_module),
`loopopt/licm_crosscheck.py`, `loopopt/_m8_test.py`. Modified:
`loopopt/transform.py` (+move()), `loopopt/__init__.py` (exports only).

BEHAVIOURAL CROSS-CHECK (licm_crosscheck.py, whole corpus, licm2 vs M8 on
independent deep copies, compared by per-instruction repr): 124/124 programs
produce IDENTICAL IR, 843 instructions hoisted on EACH side, 0 MISMATCH, 0
framework verifier failures, 0 rollbacks. Full M0–M8 suite green (10 files).
M8 does NOT wire LICM into the compile pipeline (like M6 rotation) — it is the
migrated, unit-tested, corpus-verified pass, opt-in via licm_module. STOP after
M8 delivery — no IVSR/loop_reg migration yet (await M9 approval).

## 2026-07-25 — LOOP-OPT FRAMEWORK M7: Legality framework (shared predicates)

Reusable, fact-only LEGALITY predicates shared by all future loop transforms.
NOT a pass, performs no transformation: centralizes the legality logic that was
embedded in LoopRotation so every transform asks the same questions the same way.

Files added: `loopopt/legality.py` (LegalityFact, evaluate(), the predicate menu),
`loopopt/_m7_test.py`. Modified: `loopopt/rotate.py` (legal() now COMPOSES shared
predicates; inline legality removed), `loopopt/__init__.py` (exports only).

Predicates (each returns a LegalityFact, truthy when the property holds; `known`
flag False when the backing analysis was unavailable): has_labeled_header,
is_top_tested, is_bottom_tested, has_unique_preheader, has_single_latch,
has_explicit_backedge, has_dedicated_exits (reuses M4 _shared_exit_edges),
has_side_effect_free_header, header_has_single_exit_test,
guard_inputs_loop_independent, and the analysis-backed has_clean_iv (M1 IV),
memory_safe (M2 MemEffects), profile_suitable (M3 Profile) -- these annotate the
descriptor on demand (reuse, no duplication). `evaluate(desc, [preds])` returns
the first failing fact; a transform composes exactly the predicates it needs.

LoopRotation.legal() is now `evaluate(desc, _ROTATION_LEGALITY)` over 8 shared
predicates whose conjunction is logically identical to the old inline checks.
CROSS-CHECK (canonicalize+rotate whole corpus, before vs after refactor):
IDENTICAL -- 73 rotations, 7 skips, 0 verifier failures, 0 rollbacks, 0 semantic
mismatches, all 80 loops verify clean. Composed legal() ~20 us/call (pure
descriptor-field predicates; <0.1% of the ~23 ms/loop transaction).

Note: a single-block self-loop do-while is classified TOP_TESTED by _classify_shape
(the header both tests and loops); such loops are still not rotated because their
header carries stores (has_side_effect_free_header fails) -- unchanged behavior.

Full M0–M7 suite green (9 files). STOP after M7 -- NO LICM/IVSR migration, no other
optimization (await M8 approval).

## 2026-07-25 — LOOP-OPT FRAMEWORK M6: Loop Rotation (first concrete transform)

First real optimization pass. `LoopRotation(LoopTransform)` runs ENTIRELY through
the M5 framework (MutationTransaction / framework rebuild / LoopVerifier / rollback
/ CFGDiff / TransformStats) — no framework change, no direct IR mutation. Turns a
canonical top-tested while loop into a guarded do-while.

Files added: `loopopt/rotate.py` (LoopRotation + RotationReport + rotate_module),
`loopopt/_m6_test.py`. Modified: `loopopt/__init__.py` (exports only).

IDENTITY-PRESERVING formulation (the M5 framework re-locates a loop by HEADER
LABEL): the old header is REUSED IN PLACE as the entry guard (relabeled hlbl->gl,
its in-loop edge pointed at a new header forwarder H2 that keeps label hlbl); the
guard test is deep-copied ONCE into a new bottom-test block L2 (the new single
latch); the back edge is routed through L2. No dead block. Header condition
duplicated so every predecessor of the body defines what the body/exit use ->
semantics preserved on all paths. legal() uses ONLY existing analyses/descriptor
facts (shape==TOP_TESTED, single explicit-back-edge latch, unique preheader, pure
side-effect-free header ending in one condjump, labeled in-loop+exit successors,
guard inputs not defined in the body). postcondition = BOTTOM_TESTED + single latch.

BUG found+fixed during build: first formulation left the old header as a DEAD block
that still had forward edges into the body, so the natural-loop backward flood
pulled it into the loop body -> header-dominance + reachability verifier violations
(framework correctly rolled it back). Fix: reuse old header as guard (no dead block).

Validation (`_m6_test.py`): simple while, already-rotated/idempotent skip, nested
(2 rotations, nesting preserved), do-while skip, short-circuit (&&: outer test
rotated, inner kept in loop), multiple exits (break preserved), illegal (header
side effect) skip, rollback (postcondition-fail -> byte-identical revert) — all
green; each checks CFGDiff + verifier + descriptor regen + semantic equivalence.
Full M0–M6 suite green (8 files). CORPUS (126 files, 80 loops; canonicalize then
rotate through the framework): 74 top-tested pre-rotation, **73 rotated, 7 skipped
(1 bottom-tested + 5 irregular + 1 top-tested with a side-effecting header:
d11a_ctrl.c), 0 verifier failures, 0 rollbacks, 0 semantic mismatches, ALL 80
verify clean post-rotation.** Rotation NOT wired into compile_c_to_mcode (M6 is the
pass + tests + corpus analysis only). ~23 ms/loop (incl. canon + deepcopy snapshot
+ rebuilds). STOP after M6 — LICM / migrations / any further optimization NOT
started (await M7 approval).

## 2026-07-25 — LOOP-OPT FRAMEWORK M5: LoopTransform framework (infrastructure only)

Generic transaction substrate every future loop optimization runs through. M5
implements NO optimization (no rotation/LICM/unswitch/peel/unroll/pipeline/IVSR-
or-LICM-migration) and ships ZERO passes — only reusable infrastructure.

Files added: `loopopt/transform.py` (LoopTransform base, MutationTransaction,
TransformResult, TransformStats, LoopTransformDriver, PassRegistry),
`loopopt/_m5_test.py`. Modified: `loopopt/__init__.py` (exports only).

The driver owns the ONE transaction protocol — begin → apply mutation → invalidate
→ rebuild (the sole analysis-regeneration point: `discover_function`) → run
LoopVerifier → commit / rollback — so no transform ever duplicates it. A transform
supplies only `legal()`, `run(instrs,lo,desc,txn)` (edits go THROUGH the txn), and
optional `postcondition()`. Reuses M4 mutation primitives (canonicalize._retarget/
_slice_end/_fresh_label). Rollback = undo recorded field edits + restore the list
snapshot → byte-identical IR. A faithful before/after LoopDiff comes from an
INDEPENDENT deep-copied slice snapshot (so field retargets don't corrupt it).
Epoch = monotonic invalidation token, bumped per commit; stale descriptors are
never reused. The M4 canonicalizer is FROZEN — not migrated onto the framework
(that would be a redesign; it's a later additive step). PassRegistry ships empty.

Validation (`_m5_test.py`, dummy transforms only): commit+diff+stats, rollback via
postcondition failure, rollback via lost loop identity, VERIFIER VETO via injected
failing verifier (proves verifier integration), no-op skip, illegal skip,
registration, and a MutationTransaction field-edit+splice rollback unit — all
green. Full M0–M5 suite green (7 files). Framework probe over all 80 real corpus
loops (throwaway dummy, NOT wired into the pipeline): 80/80 commit, 0 rollbacks,
0 verifier failures, ~1.74 ms/attempt (deepcopy snapshot + 2 rebuilds + verify).
STOP after M5 — Loop Rotation (M6) and all real passes NOT started (await M6 approval).

## 2026-07-24 — LOOP-OPT FRAMEWORK M4: LoopCanonicalizer + CFG-diff tool (superseded as Latest by M5)

First IR-MUTATING milestone of `compiler/loopopt/`. Purely STRUCTURAL loop
canonicalization — no rotation/LICM/IVSR/peeling/unrolling/unswitching. Built on
the frozen M0–M3 analysis layer (LoopDescriptor / LoopVerifier / CFG / Dom /
LoopInfo / InductionVars / MemEffects / Profile); recomputes none of it.

Files added: `loopopt/cfgdiff.py` (CFG-differencing DEVELOPER tool — blocks/edges
added/removed, header/latch/exit/preheader changes, label-keyed identity),
`loopopt/canonicalize.py` (LoopCanonicalizer + CanonReport), `loopopt/_cfgdiff_test.py`,
`loopopt/_m4_test.py`. Modified: `loopopt/__init__.py` (exports only).

Canonical form = LoopSimplify: (1) dedicated preheader, (2) single latch,
(3) dedicated exits. Each independently gated on its property already being
false ⇒ already-canonical loops are touched zero times. Exit normalization is
OPT-IN (`normalize_exits=True`, default OFF) so the default is a total no-op on
the corpus. Correctness protocol: every single mutation is transactional —
snapshot, splice, **rebuild CFG/Dom/LoopInfo/LoopDescriptors from scratch**, run
LoopVerifier, and ROLL BACK on any violation. Never trusts a stale descriptor.

Validation: full M0–M4 suite green (M0/M1/M2/M3 + `_cfgdiff_test` + `_m4_test`).
M4 covers already-canonical/missing-preheader/multi-preheader-edge/multi-latch/
nested/do-while/short-circuit/multi-exit/opt-in-exit/irreducible/no-change; each
checks CFG-diff + verifier + descriptor regen + semantic equivalence (a small
branch-executing IR interpreter, since eval_ir bails on branches). Corpus (126
files, 80 natural loops): 80/80 verify clean before AND after, **0 loops
modified, 0 rollbacks** with defaults — a true no-op, exactly as intended.
Opt-in exits would touch exactly 1 loop corpus-wide (testing/universal/c4.c, a
`break` sharing the loop-condition exit). Perf: 0.138 ms/loop. STOP after M4 —
LoopTransform/PassManager/rotation/LICM-migration NOT started (await M5 approval).

## 2026-07-19 — HANDOFF HARDENING: clone-and-use like gcc

For a clean handoff (works on any machine, no tribal knowledge):
- **`apara-cc`** — gcc-like front door: `./apara-cc prog.c --run` compiles,
  assembles, executes and prints the PASS/FAIL verdict vs the gcc golden.
- **`APARA_TOOLS` env var** — generated run.sh, `testing/run_gate.sh`, and
  the fuzz drivers all resolve the toolchain from it (hardcoded home paths
  removed); missing-toolchain errors are explicit.
- **`engine_patches/apply_engine_fixes.py`** — the six simulator fixes as an
  idempotent, checkable installer (`--check`/`--apply`) so any pristine
  upstream toolchain can be brought to the verified state AFTER the
  professor reviews and agrees (his tree is never touched otherwise;
  nothing was pushed to his repository).
- **`testing/run_gate.sh`** — the full 71-test golden gate, in-repo.
- **README "Getting started"** — fresh clone → working compiler in 5 steps.
Verified: gate 71/71 via the new script, `apara-cc` end-to-end smoke test.

---

## 2026-07-19 — fuzz1000 CAMPAIGN (testing/fuzz1000/): full-ISA coverage battery; SEVEN more bugs found & fixed (compiler, sim, AND process)

New `testing/fuzz1000/` — the "leave nothing untouched" campaign: a directed
battery (d01–d12) + extended generator (`gen_full.py`: all int widths
signed/unsigned, 2D/3D arrays, unions, bit-fields, enums, statics, goto,
f32+f64, intrinsics, plus everything from v1) + `cov_scan.py`, a coverage
AUDITOR that scans every passing program's mcode and proves each
(instruction × sub-op × type-tag) on the checklist was emitted and executed.
**Final: directed 13/13 + fuzz seeds 1–1000 = 613 PASS / 0 FAIL / 0 HANG /
0 COMPILE-FAIL (400 SKIP = IMEM size guard), checklist 102/102 covered —
every result slot of every executed program bit-matched native gcc.**
`check_engine_fixes.sh` now greps the engine SOURCE for all six sim fixes —
run it before/after any toolchain rebuild. The checklist encodes
justified conventions: floats load/store as raw-width ints; `%` is synthetic
(only its `/` appears); `<`/`<=` branches canonicalize to `>`/`>=`; casts use
$i32 as the generic int side; vector tags are $v-prefixed.

**Compiler bugs found & fixed:**
1. **Global initializers with literal suffixes zeroed** — `int('0x01LL',0)`
   raised and `except: return [0]` swallowed it; every `LL`/`u`-suffixed
   initializer became 0 (ir_gen `_flatten_init`).
2. **Struct-global float-field initializers zeroed** — one ftag applied to
   the whole InitList; `{2.5, 1.5f, 7}` became `{0,0,7}`. New
   `_flatten_struct_init` encodes per-field (guarded to float-bearing structs
   so bitfield layouts keep the old path).
3. **3D arrays: init laid out flat, accesses strode by inner-ROW size** (16
   for `long long[..][..][2]`) — int 3D arrays worked by symmetric luck.
   N-D decls now record full dims (`_array_ndims`) and `_2d_base_and_offset`
   walks any-depth subscript chains with per-level strides.
4. **`(long long)sqrt(x)` skipped the float→int cast** — `_float_tag_of`
   didn't know fsqrt intrinsics return floats; raw IEEE bits were stored.
5. **Unsigned 64-bit `/`, `%`, `>>` used the signed path** — IRBinOp had a
   dead `unsigned` field; now set by `_binop` (via `_expr_is_unsigned`) and
   honored by codegen ($u64 tag; the `%` expansion's divide carries it).
   The old "u64>> is a sim limit" no longer holds on the current engine
   build (verified: logical shift by 63 correct) — comment removed.
6. **`__nop()` was silently DELETED by the bundler** (`_parse_flat` dropped
   `$null`); kept now — codegen only emits $null for an explicit nop.
7. Golden driver now links `-lm` (sqrt/sqrtf natively); `__pack` directed
   test fixed (packed_nbits must be a multiple of word_nbits — the
   assembler checks this SILENTLY, no error text).

**Simulator bugs found & fixed (in SOURCE, see process note):**
- `$fsqrt` Execute was a "yet to be implemented" stub silently producing 0
  (MachineRun.cpp); now routes through the standard ALU path → fp_sqrt.
- `$vreduce` UNSIGNED variants sign-extended lanes (shared `r` in
  McodeOperations.cpp) — `__vreduce_vu8` summed 0xf2 as −14; unsigned lanes
  now zero-extend.
- Scalar 32-bit casts misclassified as vector (`is_vector_cast =
  in_vals.size()>1` fires for Break_Vector'd scalars) → result masked to 32
  bits, stripping sign extension of e.g. `(int)(-6.0f)`; now uses the
  actual type Vector_Flags.

**PROCESS INCIDENT — sim fixes lived only in binaries:** rebuilding the
toolchain for the fsqrt fix silently REVERTED the Jul-17/18 FP-campaign sim
fixes (cast dispatch swap, execute-cast float stub, cast_int_to_float
ternary promotion) — they had been built and deployed but the SOURCE was
left pristine. Detected because the gate dropped to 60/61. All four are now
re-applied IN SOURCE with re-application comments, so a rebuild can never
lose them again. fp_sub's macros were already correct in this tree.
Full gate re-verified 61/61 after every rebuild.

---

## 2026-07-18 — FUZZ CAMPAIGN (testing/fuzz/): randomized differential testing; TWO more latent bugs found & fixed

New `testing/fuzz/` — `gen_fuzz.py` (seeded random whole-program generator
mixing every supported feature; UB-free by construction: bounded loops &
recursion, masked shifts, guarded divisors, 64-bit-forced shift/multiply
width, no NaN, no u64>>) + `run_campaign.sh` (generate → compile with
automatic gcc-golden → align/assemble — with toolchain stderr CHECKED, see
bug 2 — → simulate → classify PASS/FAIL/HANG/TOOLING/COMPILE per seed;
failing seeds keep their whole directory for triage). Rationale: the LICM
bug proved gate suites miss whole-program-shape bugs.

**Bug 1 — struct-pointer subscript read garbage (silent wrong values).**
`p[i].field` where `p` is a POINTER-to-struct (param or var) hit
`_structref_base_and_total_off`'s ArrayRef branch, which only knew struct
ARRAYS (`_array_struct_elem`) and silently returned base `Const(0)` — the
load read DMEM address 0 and produced 0. `(p+i)->field` (semantically
identical) was always correct, so the stride machinery existed; the fix
routes the pointer case through `_var_struct_ptr_type` + `_eval_addr`, and
the residual unknown-shape fallback now FAILS LOUDLY instead of emitting an
address-0 access. Found by fuzz seeds 4/15 (`pdot(struct P *p, int n)`
looping `p[i].x * p[i].w - p[i].y`).

**Bug 2 — bundler ignored the hardware's per-bundle LANE LIMITS
(sim crash / undefined control flow).** A full bundle physically has 4
load/store lanes and 1 divide/sqrt lane (McodeProgram::
alignFullBundleToLanes). The bundler packed FIVE independent caller-save
stores plus a $call into one bundle; mcode_align's lane placement then
FAILED — it prints an Error (which run scripts discarded via 2>/dev/null)
but still emits the bundle WITHOUT the CTI moved to lane 0, and the sim's
return-address arithmetic sent a $return into the MIDDLE of a bundle
(assertion crash; on hardware, undefined behavior). Trigger: ≥5 temps live
across a call — increasingly common now that >4-arg/variadic calls save
more temps. Fixed in both the packer (`_pack_bundles`) and the scheduler
heuristic (`can_join`): ≤4 ld/st and ≤1 div/sqrt per bundle. Found by fuzz
seed 68 (plain calls + recursion — no new features involved).

Both fixes verified: repro seeds pass, full gate + fnptr/vararg/many_args
green.

**Final campaign result (after fixes): seeds 1–400 = 275 PASS, 0 FAIL,
0 HANG, 0 COMPILE-FAIL** (125 SKIP = generated program exceeded the 0x800-
word IMEM — size guard, not a failure). Every executed program's every
results[] slot matched native gcc bit-for-bit. Campaign scoreboard for the
day: 3 real compiler bugs found (LICM cross-function aliasing, struct-ptr
subscript, bundler lane limits), all fixed and covered.

---

## 2026-07-18 — >4 NAMED ARGS DONE (ma01–03 12/12) + LATENT LICM/loop_reg CROSS-FUNCTION ALIAS BUG FOUND & FIXED

**>4 named args** — unified with the variadic convention: ALL args beyond the
first 4 are stack-passed at [FP + 8 + 8*i] in the callee (caller reserves
below SP exactly like variadic extras — the same _gen_IRCall n_reg path,
now set to 4 for any known >4-param function). Callee prologue copies params
5+ from [FP + 8 + 8*(i-4)] into their normal local slots (borrowed scratch).
For a VARIADIC function with >4 named params, the stack area holds named-5+
first, then the extras — IRVaStart gained an `offset` field
(8 + 8*max(0, named-4)) so va_start skips past them. The old hard 4-param
compile error in _gen_IRFuncBegin is gone. Indirect calls keep the ≤4 limit
(callee signature unknown). Suite `testing/many_args/` ma01–03 = 12/12 vs
gcc: 5/6/8-param fns, recursion, loops, nested >4-arg calls, and the
variadic+5-named combo.

**Latent bug (pre-existing, NOT from this feature)**: ma02's loop case
exposed a **stale-loop-counter miscompile in LICM** — an infinite loop on
hardware. Root cause: `hoist_loop_invariants` built its `def_map` over the
WHOLE program, but temp names RESTART in every function (Temp.reset()), so a
later function's definition of the same temp name overrode the current one.
`_root_target` then resolved a loop's store base to the wrong slot, the store
to the counter never landed in `wr_stack`, and the counter's reload passed
`load_safe` and was hoisted → the loop compared a stale register forever.
Confirmed pre-existing (reproduces on HEAD~ with the feature stashed;
minimal repro: initialized global array + a callee whose inner loop uses
separate address temps for the counter slot — whether it fired depended on
which loop-opt TIER the whole program landed on, which is why the 61-test
gate never caught it). Same collision class as the 2026-07-18 loop_reg
Temp() bug.

**Fix**: `licm._func_bounds` — every name-keyed analysis map is now built
over the enclosing FUNCTION slice only: licm's def_map per loop, loop_reg's
`addr_off` + `addr_uses` (they scanned the whole program too — same latent
exposure, docstring said "function-wide" but code wasn't), and
`promoted_offsets` now keyed (func, off) since raw FP offsets also collide
across functions. Rule reaffirmed: **no IR pass may key any map by bare temp
name or FP offset across function boundaries.**

Full gate green after both changes: feature_sweep 16/16, universal 21/21,
pointer_bugs 15/15, fp 50/50, fnptr 16/16, vararg 12/12, many_args 12/12 —
zero regressions.

---

## 2026-07-18 — VARIADIC FUNCTIONS DONE: stack-passed extras + near-pure-C va_list macros (va01–03 12/12)

**Calling convention extension** (ours to define, no toolchain change):
- Named (fixed) params of a variadic function pass in r2–r5 as always.
- For the extras, the CALLER reserves `8*(n_extra+1)` bytes just below its SP
  (`+ $r27 ($i64) $r27 -K`), stores extra i at `[newSP + 8 + 8*i]` (slot 0 is
  where the callee's prologue saves the caller FP), calls, then releases the
  area. Since the callee sets FP = entry SP, the callee finds extra i at
  `[FP + 8 + 8*i]`. Limit: 62 stack args (single ALU-immediate SP bump);
  fail-loudly check added.

**va_list machinery is almost pure C** — only ONE new intrinsic:
`__va_start()` → IRVaStart → `+ dest ($i64) $r26 8` (= FP+8). The rest rides
on existing pointer support:

    long long *__va_start();
    #define va_list long long *
    #define va_start(ap, last) ((ap) = __va_start())
    #define va_arg(ap, type)   ((type)*(ap)++)     /* deref + stride-8 bump */
    #define va_end(ap)

**Shared-source golden verify**: tests guard the macros with `#ifdef
__APARA__`. `preprocess()` now passes `-D__APARA__`, and `try_golden_verify`
receives the RAW file text (not the preprocessed source) so native gcc takes
the real `<stdarg.h>` branch. This raw-source switch applies to all golden
tests (verified: full gate unchanged).

**Plumbing**: IRCall gains `n_reg` (split point: first n_reg args in
registers, rest on stack); ALL args stay in `.args` so every operand/liveness
scan (codegen `_get_src_temps`, licm) keeps working unmodified. ir_gen
pre-pass records variadic functions (EllipsisParam) with their named-param
count; existing param loops already skip EllipsisParam. Extras are stored via
$r25 scratch (safe: all live temps already caller-saved, same reasoning as
the indirect-call sequence).

**New suite `testing/vararg/` va01–va03 = 12/12 golden checks vs gcc**:
va01 sum(n,...) with 0/1/3/6 extras; va02 two named params + non-constant
args (array elements, expressions) + variadic calls in a loop; va03 variadic
calling variadic, nested variadic-call args, variadic mixed with normal
calls. Full gate green: feature_sweep 16/16, universal 21/21, pointer_bugs
15/15, fp 50/50, fnptr 16/16 — zero regressions.

Known scope limits: float/double variadic args untested (va_arg loads raw
8-byte words — f64 bits would pass through, f32 promotion semantics
unverified); variadic INDIRECT calls (through a function pointer) not wired.

Remaining ❌: >4 named args (NEXT — reuse the variadic stack-passing
mechanism). Real strings are NOT needed for the APARA accelerator (decided
2026-07-18): address-of-only stays by design.

---

## 2026-07-18 — FUNCTION POINTERS DONE: compiler-side linker pass unblocks them without any toolchain change (fn01–04 16/16)

Function pointers were "blocked at the assembler" (2026-06-17: $set's grammar
only takes numeric immediates, so no way to materialize a label's address).
Re-examined today: the block dissolves because the **compiler can act as the
linker** — after bundling, every label's final instruction address is fully
determined by mcode_align's layout rule, so the compiler patches numeric
addresses into $set placeholders itself. The .py pipeline still only emits
text; no assembler invocation, no toolchain change.

**Layout rule replicated** (McodeProgram::Align_Bundles +
McodeInstructionBundle::Calculate_Capacity): a bundle holding any CTI
(?-branch/$call/$return), load/store, divide, or $fsqrt gets capacity 8;
otherwise instruction count rounds up to 1/2/4/8. Bundles are null-padded to
capacity and placed at the next capacity-aligned address. For full bundles the
aligner moves the CTI to lane 0, so a $call's own address = its bundle's base
address. Verified: computed addresses match mcode_align's `// pc=0x..`
annotations exactly.

**Second discovery — indirect $call is PC-RELATIVE**: the old assumption
(ISA §6.2, "target read from bottom 32 bits") is wrong at the implementation:
`___execute_call_operation___` computes `npc = call_instr_addr +
(int32_t)reg` for the register form too (McodeExecute.cpp:408-416).
Convention chosen: function-pointer VALUES stay absolute (so fp==func
comparisons and storage work); each indirect call site converts just before
the call:

    $set $r25 0 __icall_K     ← patched to THIS call's bundle address
    -    $r1 ($i64) $r1 $r25  ← absolute target − call site
    $call $r1

**Implementation** (the IR/codegen scaffolding from June already existed):
- `bundler.resolve_code_labels(text)` — post-bundling linker pass: replicates
  the aligner layout, patches `$set rN 0 <func_label>` → absolute address and
  `$set rN 0 __icall_K` → bundle address of the next register-indirect $call
  (safe pairing: scheduling never crosses a CTI, and each placeholder's call
  terminates its own basic block). Fails loudly on unresolved labels.
- `bundler._parse_deps`: `$call $rN` now READS rN (real hazard gap — the
  bundler could previously have packed the target computation into the call's
  own bundle).
- `codegen._gen_IRIndirectCall`: emits the 3-instruction conversion above;
  $r25 is safe scratch there (all live temps already caller-saved).
- `compiler.py`: runs resolve_code_labels right after bundle_mcode.

**New suite `testing/fnptr/` fn01–fn04 = 16/16 golden checks vs gcc**:
fn01 assign/call/reassign/(*fp)/nested fp(fp(..)); fn02 callbacks (fp param,
fold over array); fn03 dispatch table (local array of fps, ops[i](..));
fn04 &func form, fp returned from function, fp == func-name comparison,
global fp. Full gate green after: feature_sweep 16/16, universal 21/21,
pointer_bugs 15/15, fp01–09 50/50 — zero regressions.

Remaining ❌: variadic functions, real string support, >4 args.

---

## 2026-07-18 — FP STEP 7 DONE: bit-exact float result verification (fp09 6/6) — FP CAMPAIGN FULLY COMPLETE

The last (optional) FP item. `try_golden_verify` (compiler.py) now supports two
new golden-convention arrays alongside `results`: **`fresults[]`** (float) and
**`dresults[]`** (double). The gcc driver captures their IEEE bit patterns via
`__builtin_memcpy` (a value cast would numerically convert), and each APARA
DMEM word is compared BIT-EXACTLY. Two conventions encoded in the harness:
- element count = total_bytes / **stride** (DMEM footprint, one 8-byte word per
  element), not / elem_bytes — dividing by elem_bytes over-read gcc's arrays;
- f32 expectations are shifted **<<32**: APARA stores sub-word (i32/f32) data
  in the HIGH half of its 8-byte DMEM word.

fp09 = 6/6 bit-exact vs gcc, which also proves the simulator's f32 AND f64
arithmetic are IEEE-identical to the host (including 1.1*3.0's last-bit
rounding = 0x400A666666666667 and 1.1-0.1 = exactly 1.0).

Full gate green: feature_sweep 16/16, universal 21/21, pointer_bugs 15/15,
fp01–fp09 = **50/50**. All 7 FP-plan steps + all gap closures complete; the
floating-point campaign is finished.

## 2026-07-18 — FP GAPS ALL CLOSED (fp08 8/8): implicit int→float conversions everywhere, struct float fields, float truthiness

All "known deliberate gaps" from the FP campaign are now closed, gcc-verified
(fp08), full gate green (16/16, 21/21, 15/15, fp01–fp08 = 44/44):
- **int args to float params** (`half(3)`) — pre-pass records
  `_func_param_ftags` per function; `_call` converts each argument.
- **int returns in float functions** (`return 3;`) — `visit_Return` converts
  via the function's `_func_ret_ftag`.
- **int→float in decl-init and assignment** (`float a = 7; a = 9;`) — both
  store the IEEE bits now.
- **float COMPOUND assignment** (`acc /= 4.0f`) — was an integer binop on the
  bit patterns; now float-tagged with mixed-operand conversion, same as
  `_binop`.
- **struct float/double fields** (`p.x*2.0f`, `p->y`, `pts[i].f`, nested
  `s.a.b`) — `_register_struct` records `_struct_field_ftag`; `_float_tag_of`
  resolves StructRef bases via new `_structref_struct_name`.
- **float truthiness** (`if(f)`, `!f`) — condition/`!` lowering pass the float
  tag so -0.0 is falsy (float compare instead of raw-bits test).

The scalar f32/f64 subset is now C-semantics-complete for everything outside
step 7 (optional bit-exact float result verification), which remains open.

## 2026-07-18 — FP CAMPAIGN COMPLETE (Steps 4–6 + globals/arrays): comparisons, f64, mixed types, params/returns, float arrays all pass vs gcc

Steps 4–6 of `testing/fp_check/FP_PLAN.md`, all gcc-verified, full gate green
(feature_sweep 16/16, universal 21/21, pointer_bugs 15/15, fp01–fp07 = 36/36).

- **Step 4 — float comparisons (fp04 8/8).** All 6 operators + float-driven
  `while`. Lowering: float-subtract, `+0.0` (canonicalizes -0), branch on the
  diff's SIGN via an ($i32) test tag for f32 / ($i64) for f64 — the branch
  instruction sign-extends from the test-type width, so bit 31/63 IS the float
  sign bit. IRCondJump grew an `ftype`; both creation sites pass it.
- **Step 5 — double + usual arithmetic conversions (fp05 8/8).** New
  `_float_operand`: a mixed int operand of a float op/comparison converts
  (constants re-encoded to IEEE bits at compile time, otherwise a runtime
  `$cast`), and f32 widens to f64 (`_float_tag_of` prefers $f64). Fixed float
  **unary minus** — it was an integer `0 - bits` (turned -1.25 into -3.5); now
  a float subtract when the operand is float.
- **Step 6 — params/returns (fp06 4/4).** Params now recorded in `_var_ctype`;
  float return tags pre-collected per function (`_func_ret_ftag`) in the
  FileAST pre-pass so calls compiled before the callee's definition work.
- **Float globals + arrays (fp07 4/4).** Global float/double initializers are
  IEEE-encoded at the DECLARED element width (`_flatten_init` gained `ftag`;
  `float g[]={1.5}` packs f32). `_float_tag_of` extended to ArrayRef (1D/2D)
  and `*p` via the declared element type (`_float_tag_of_decl_elem`).

Known deliberate gaps (see FP_PLAN.md): struct float fields, int-literal args
to float params, `return 2` in float functions, float truthiness `if(f)`.
Coverage table FP row -> ✅ (scalar f32/f64 subset).

## 2026-07-18 — FP Step 3 GREEN: int→float cast works incl. negatives; simulator ternary-promotion bug + 2 more bundler holes fixed

FP campaign Step 3 (`(float)i` via `$cast ($f32) rd ($i32) rs`). The compiler
side was already in place from Step 1; fp03 exposed two toolchain bugs:

- **Simulator `cast_int_to_float` ternary-promotion bug (McodeFpuUtils.cpp:517)**
  — `double x = (unsigned ? (u & 0xffffffff) : (int) u);` promotes the signed
  arm's `int` back to uint64_t (the unsigned arm's type), so `(float)(-3)`
  became `(double)(2^64-3)` = 0x5F800000 and then saturated to INT64_MIN on the
  way back. All POSITIVE int→float casts were unaffected, which is why Step 1
  passed. Fixed with an explicit if/else + `Sign_Extend_64` from the source
  width; rebuilt + deployed to engine_new/bin (same workflow as the Jul-17 sim
  fixes). Verified by hand-written `$cast` micro-test and fp03.
- **Bundler `$ld`/`$st` regexes ignored float tags (bundler.py)** — `($f32)`
  loads/stores parsed as nothing: no dest/base register deps, no memory-hazard
  tracking. Widened to `[iuf]` (same hole class as the Jul-17 ALU/compare fix).
- **Bundler memory-hazard check was tuple-equality (bundler.py)** — a store
  via `$r8` and a load via `$r9` of the SAME address landed in one bundle
  (both regs held FP-16), so the load read stale data. New `_mem_may_alias`:
  after a store, a memory access stays in the bundle only if provably disjoint
  (same base register, different constant offsets). Applied to both
  `_pack_bundles` and the clustering heuristic.

Verified: fp03 = 14, -6, 3, 17 vs gcc (int→float positive/negative, ×, ÷, +);
integer narrowing casts re-checked post-rebuild (44/255/4464/-56 ✓); full gate
re-run GREEN (feature_sweep 16/16, universal 21/21, pointer_bugs 15/15,
fp01/fp02/fp03 0 err).

**Next:** FP Step 4 — float comparisons (`a<b`, `a==b` → branch/cmov).

## 2026-07-18 — TWO integer codegen bugs found via u2_binsearch hang: _ptr_stride cross-function leak + loop_reg temp-name collision

The universal-suite re-run (lost in the Jul-18 forced shutdown) hung on
u2_binsearch. Root-caused to two independent pre-existing bugs, both fixed:

**Bug 1 — `_ptr_stride` leaks across functions (ir_gen.py `_record_ptr`).**
`_ptr_stride` is never scope-popped, and in `_eval_addr` the pointer path
outranks the array path. So `bsearch(int *a, ...)` registers `a` as a pointer,
and a LATER function's local array `a` inherits that stale entry: passing the
array to a call then LOADS `a[0]` and passes the value as the "pointer"
(callee reads zeros; binsearch returned -1 for every query). Repro'd minimally
(d2.c: `get(int*,int)` + `int a[7]` local in main → returned 0s). Fix in
`_record_ptr`: a non-pointer declaration now pops any stale
`_ptr_stride`/`_ptr_elem_bytes` entry for that name (same shadowing pattern as
`_unsigned_vars` add/discard).

**Bug 2 — loop_reg fresh temps collide with the function's own temps
(loop_reg.py).** `Temp.reset()` runs per function during IR generation, so
`_tN` names are only unique within a function — but `promote_loop_counters`
created scaffolding temps with bare `Temp()`, which continues the global
counter from wherever the LAST function left it. In u2_binsearch the preheader
temps came out as `_t11`/`_t12`, colliding with bsearch's own `_t11`/`_t12`:
two live values shared one register (`$r8` held the address of `lo`, then got
clobbered with another slot's address before the loop), so `m=(lo+hi)/2` read
the wrong slot and the loop never terminated → the hang. Only triggers when
LICM+loop-reg run together (numbering alignment); each pass alone passed. Fix:
loop_reg temps now use their own `_lrN` namespace (`_new_temp()`), which can
never collide with `_tN`.

Verified: u2_binsearch 4/4 (3, 0, 6, -1); d2/d3/d4 minimal repros pass; full
gate GREEN — feature_sweep 16/16, universal 21/21, pointer_bugs 15/15,
fp01/fp02 0 errors (all with the rebuilt Jul-17 toolchain).

## 2026-07-18 — FP CAMPAIGN Steps 1–2: f32 literals, arithmetic, variables, float↔int casts all pass vs gcc; 3 simulator FP bugs found+fixed

Floating-point campaign per `testing/fp_check/FP_PLAN.md`. Session cut short by a
forced shutdown right after Step 2 passed; state recorded here on resume.

**Compiler changes** (all gated on `_is_float_expr` — integer paths untouched):
- ir.py / ir_gen.py / codegen.py — float type tracking (`_is_float_expr` mirroring
  `_expr_is_unsigned`), float literals parsed to IEEE bit patterns (struct.pack),
  float tag on IRBinOp → `+ ($f32) …`, float↔int casts via `$cast`.
- bundler.py — ALU-op regex (l.191) and compare regex (l.185) only matched `($i\d+)`
  tags, so `($f32)` ops had **zero hazard tracking** and got reordered before their
  constant-materialization (`<<16`/`|`) completed. Fixed to accept `i`/`u`/`f` tags.

**Three simulator bugs found + fixed** (prof_git_folder …/assembler/src, toolchain
rebuilt):
1. `McodeOperations.cpp` `___cast_operation___` — int↔float dispatch called the two
   conversion functions **swapped** (each read its operand as the wrong type → garbage).
2. `McodeExecute.cpp` `___execute_cast_operation___` — the float path (the function
   MachineRun actually calls) was an unimplemented stub leaving `ovalues`
   uninitialized; now routes through the fixed `___cast_operation___`.
3. `McodeFpuUtils.cpp` `fp_sub` — case 32 subtracted the raw **integer bit patterns**
   (`as - bs` instead of `sa - sb`); case 64 used `+` instead of `-`. fp_mul/fp_div
   checked clean.

**Verified:** fp01 (Step 1: literals + arith + float→int cast) = 5, 7, 12, 2 vs gcc ✅;
fp02 (Step 2: float variables load/store, `a+b`/`a-b`/`a*b`/`c/d`) = 5, 2, 5, 2 vs
gcc ✅. Regression gates after sim rebuild + bundler change: feature_sweep 16/16 ✅,
pointer_bugs 15/15 ✅, universal re-run pending (result lost in shutdown).

**Next:** FP Step 3 — int→float cast (`(float)i`), then comparisons, f64, params.

## 2026-07-17 — FEATURE SWEEP GREEN: sizeof, enum, static locals, goto, designated inits, anon-struct typedef all implemented (16/16)

Divide-and-conquer of the coverage table's ⚠️/❌ integer items. Six fixes, each
gated (feature sweep + real suite + pointer battery), zero regressions:
- **sizeof** — compile-time constant via `_type_size`; tracks each var's C type
  (`_var_ctype`); pointers = 8B. (was: returned 0)
- **enum** — pre-pass `_collect_enums` registers every enum constant (auto-inc or
  explicit `=expr`); `_load_var` resolves them. (was: explicit values ignored)
- **static locals** — allocated as a hidden global (`__static_<fn>_<name>`,
  persists in data.map), local name bound to it. (was: reset each call)
- **goto/labels** — `visit_Goto`/`visit_Label` (function-mangled label names).
- **designated array initializers** `{[2]=30}` — `_flatten_init` handles
  `NamedInitializer`, sparse-fills with zeros.
- **anonymous-struct typedef** (`typedef struct{..} Pair; Pair p;`) — new
  `_struct_name_of` resolves a struct-typedef IdentifierType to its struct, so
  `_record_struct_var` recognizes `Pair p` as a struct.

Verified: feature sweep 16/16 (testing/feature_sweep/), universal 14/14, pointer
battery 15/15, real suite 0 err. The C-feature coverage table above is now all-✅
for the integer subset; only floating point, function pointers (assembler-
blocked), variadics, real strings, and >4 args remain.

## 2026-07-17 — LICM correctness fix: `&&`/`||` loop conditions now work (u5 fixed). Universal battery 13/13

Last universality gap (u5 sieve) root-caused to a LICM bug, NOT a control-flow
codegen bug. A short-circuit `&&`/`||` (or a comparison) in a loop condition
produces a result temp assigned TWICE (res=0 / res=1 on the two arms). LICM
treated the `res=Const(0)` arm as loop-invariant (constant source) and hoisted it
out of the loop, so `while(a && b)` evaluated the condition once with stale values
and the body never ran (u5 all zeros). Confirmed via APARA_NO_LOOPOPT=1 (passed).
Fix (licm.py): never hoist an instruction whose destination is assigned more than
once in the loop (control-dependent). Legit invariants (addresses/loads, defined
once) are unaffected -- LICM wins preserved (matmul_n16 124->72, etc.).

Verified: c1/c4/b2 (`&&` loops) + u5 pass; **universal battery 13/13** (all novel
algorithms: bubblesort, binsearch, gcd, popcount, sieve, matrix transpose,
struct-ptr, in-place reverse); pointer battery 15/15; real suite 0 err; LICM
bundle reductions intact. Integer C (scalar + vector + pointers + structs +
struct-arrays + control flow) is now functionally universal.

## 2026-07-17 — STRUCT ARRAYS implemented: pts[i].field, &pts[i], p->field, (p+1)->field (u7 fixed)

Executed testing/universal/STRUCT_ARRAY_PLAN.md (3 gated steps). Arrays of
structs now work; they were previously flattened at allocation and had no
`pts[i].field` access path.
1. `_record_struct_var` now handles ArrayDecl-of-struct -> tracks element struct
   type in new `_array_struct_elem`; visit_Decl overrides the array's indexing
   stride to the struct DMEM size (`_struct_total_dmem`, e.g. 16) AFTER the flat
   init (the flat 8-byte-word data layout was already correct).
2. `_structref_base_and_total_off` gained an ArrayRef case: `pts[i].field` ->
   address of pts[i] via `_eval_addr` (now struct-strided) + field offset from
   `_struct_layouts`.
3. `_record_ptr` sets a pointer-to-struct's stride to the struct size so
   `(p+1)->field` advances by a whole struct.
Verified: sa.c 8/8 and u7_struct_ptr_algo pass; universal 11/12 (only u5 sieve
left); pointer battery 15/15; real suite (struct/2D/array/pointer/matmul/spill/
ldst/subword/call-return) 0 err.

## 2026-07-17 — BUG FIX: unified local/global array convention -> pointer store into a LOCAL array now works (u8)

Found via the universality test u8 (in-place reverse via pointers into a local
array). Root cause: local `int` arrays were accessed as $i64 (value in low bits,
elem_bytes==stride==8) while GLOBAL arrays and POINTERS use $i32 (value in
bits[63:32], elem_bytes==4). So a pointer store `*pl=88` into a local array wrote
bits[63:32] but the $i64 read got the low half (0x5800000001). Isolated in
testing/ptr_isolate/pls.c (global ptr store OK, local ptr store wrong).

Fix (ir_gen.py): unify -- local arrays now use the element WIDTH (via
`_local_elem_bytes`) as elem_bytes in `_eval_addr`, matching globals+pointers;
the local-array initializer stores at `esz` width (bits[63:32]) with a stride-8
offset. Now local and global int arrays, and pointers into either, all use the
same $i32/bits[63:32] convention.

Verified: pls + u8 pass; universal 9/11 (only u5 sieve, u7 struct-array left);
pointer battery 15/15; wider regression 10/10 clean; real suite 0 err.

## 2026-07-17 — UNIVERSALITY AUDIT: no codegen bias; local-array-initializer bug FIXED; 3 gaps mapped

Audited for test-specific bias (request: "make it universal"). NO codegen bias:
the `results` name is used ONLY by the golden-verify harness in compiler.py
(gcc as an independent oracle), never by ir_gen/codegen; `0x400` GBASE is a
configurable default. Codegen treats all names/programs uniformly.

Tested with NOVEL algorithms (not feature unit-tests), `testing/universal/`
u1-u8 (bubblesort, binsearch, gcd, popcount, sieve, matrix transpose, struct-ptr,
in-place reverse). Found real gaps: 4/8 -> **6/9 after fixing local-array
initializers**.

**FIXED: local array with initializer** (`int a[3]={11,22,33}`). visit_Decl's
InitList store used `i*esz` offset AND `esz` width, but local-array elements sit
one per DMEM word (stride 8), accessed full-word ($i64) -- so a[1] landed in
a[0]'s low half (0xb00000016) and $ld($i64) read the wrong half. Fix: use the
array's DMEM stride (`_array_elem[name]`) for BOTH offset and width. m1/u1 pass,
real suite 0 err, pointer battery still 15/15.

STILL OPEN (universality campaign, reproducers in testing/universal/): u8
(pointer store `*lo=*hi` into a LOCAL array -- swap doesn't take), u7 (array-of-
structs `&pts[i]`/`p->x` reads wrong fields), u5 (sieve: global array +
`results[n++]` var-index + compound `i<30 && n<5` loop cond -> all 0).

## 2026-07-17 — POINTER REFACTOR COMPLETE: central `_eval_addr`; battery 15/15, zero regressions

Executed `testing/pointer_bugs/REFACTOR_PLAN.md` end to end (9 steps, each
gated on battery + real suite). The ad-hoc address/decay/stride code paths in
`ir_gen.py` are replaced by ONE central evaluator:
`_eval_addr(node) -> _Addr(gaddr, base, off, stride, eb, unsigned, scope)`,
with thin wrappers `_addr_load` / `_addr_store` / `_addr_value` and the
decaying value-site visitor `_visit_operand` (used at init/assign RHS,
comparison operands, call args, return). Covered in one place: array→pointer
decay (global + local), pointer-variable value loads, `p[i]`/`arr[i]`,
`*p`/`*(p±n)`, `&x`/`&arr[i]`/`&p`, ptr±int with stride scaling, pointer
difference `q-p` (byte diff / DMEM stride — divisor is the 8-byte stride, not
the C width), pointer comparisons (`p == arr` compares addresses), `p++`/`--`,
call args, return values. Pointer element STORE width (RC2) fixed en route:
stores through `*p`/`p[i]` now use the pointee width, mirroring the earlier
load-width fix. Global locations keep the efficient GBASE-relative
IRGlobalLoad/IRGlobalStore path via `_Addr.gaddr`, so no code-size growth on
the array-heavy tests. Deleted as now-dead: `_visit_rvalue`,
`_ptr_stride_of_node`, the `_binop` +/- scaling block, the call-arg `is_arr`
hack. `_array_base_off`/`_get_esz` remain only as the legacy fallback for
shapes `_eval_addr` doesn't claim (e.g. struct-field bases).

Results: battery t01–t15 **15/15** (was 4/15). Real-suite gate
(pointer/array/2d/struct/matmul) 0 err after EVERY step. Final full-suite A/B
(78 discovered run.sh tests, baseline vs refactored compiler): only
improvements — `t15_ptr_param_store` and `testing/ptr_isolate/L` went to
0 err; nothing else changed.

`test_scalar_full` (3 err) root-caused: NOT a compiler bug. The assembled
program (577 bundles) exceeds the simulator's half-sized IMEM — loading stops
at word 0x800 and PC runs into empty IMEM before sections 8–10 (switch,
calls, aggregate) execute; results[8..10] stay 0. Identical before/after the
refactor; unfixable from the compiler side (needs the IMEM fix or a split
test). `stress/shift_bug` (2 err) is the known `$u64 >>` simulator limit.

## 2026-07-17 — POINTER CAMPAIGN (session 2): array-to-pointer decay at rvalue sites; local pointer init now works

Started the systematic pointer bug campaign (`testing/pointer_bugs/`, 15-test
battery t01-t15 + POINTER_BUGS_CAMPAIGN.md). Landed the first fix: C
array-to-pointer decay at rvalue SITES. New `_visit_rvalue` helper +
`_is_array_name` (array-but-not-struct predicate, since structs also land in
_array_elem via n_elems>1), applied at initializer-RHS and assignment-RHS. Fixes
`int *p = arr` / `p = arr` (previously loaded arr[0]'s VALUE instead of &arr[0]).
Battery 2/15 -> 4/15 (t09 char*, t10 long long*, t12 ptr-struct, t13 loop via
*(p+i)); real suite clean (pointer/array/2d/struct 0 err, matmul 256/256).

Two decay approaches tried and REVERTED (regressions): blanket decay in
_load_var (breaks struct/2D t12/t13) and in binop operands (breaks
pointer/test_pointer + array indexing) -- decay must stay site-specific with
element-stride awareness. Remaining pointer work (open, see campaign doc): `arr+n`
decay+scale, comparison-operand decay (t11), pointer STORE width (t07/t15),
`&x`/`&arr[i]` (t03/t04), `p++` (t08), `q-p` (t06), return `a+i` (t14).

## 2026-07-17 — BUG FIX: pointer-element load used $i64 (full word) instead of pointee width; found by stress testing

Differential stress testing (compiler vs gcc golden) found a real correctness
bug: `p[i]` / `*p` on a pointer loaded via `$ld ($i64)` (full 64-bit word)
instead of the pointee's type (`$ld ($i32)` for `int*`). Because sub-word ints
live in bits[63:32] of the 8-byte DMEM word, the full-word load returned a
duplicated/garbage value (e.g. `int f(int *p){return p[0];}` on {10,...} gave
`0xa0000000a` instead of `0xa`). Root cause: `_arrayref` used `_get_esz`, which
returns `_ptr_stride` (=8, the DMEM slot stride) for pointers, as BOTH the
address stride AND the load width. Fix: new `_ptr_elem_bytes` map (pointee width,
set in `_record_ptr`); `_arrayref` now uses it for the IRLoad width while keeping
stride=8 for address arithmetic. Verified: pointer-param deref now correct;
regression clean (pointer 10/10, array/2d/struct, matmul_n16 256/256).

KNOWN STILL-OPEN (separate, pre-existing): a LOCAL pointer initialized from an
array (`int *p = arr; p[0]`) computes the wrong address (array->pointer decay in
a local initializer); `p-arr` gives -70 not 0. Also the pointer STORE path width
not yet audited. See `cmp_wd/testing/STRESS_FINDINGS.md`. Other stress findings:
unsigned-64 `>>` is a simulator limit ($u64 ALU zeroes via __mmask__(64) UB);
signed-overflow (INT_MIN-1 etc.) diverges from gcc (UB).

## 2026-07-17 — TOOLCHAIN MIGRATION + $vreduce MAX sub-op implemented. Integer ISA coverage now complete

Migrated to the prof's updated engine/assembler (Jul-5 build, scp'd to
`prof_git_folder/`). Two toolchain findings, both handled:

**(a) Prof's Jul-5 build FIXES the `$vreduce` unsigned bug (E4)** — `McodeOperations.cpp`
unsigned reduce now zero-extends. So unsigned `$vreduce` is reliable on the new
toolchain (old "prefer signed" caution obsolete).

**(b) Found + fixed a 1-line regression in the prof's decode that broke BACKWARD
`$call`.** `McodeDisassemble.cpp:266` had `Sign_Extend(25,...)` for the 25-bit
[24:0] call offset (sign bit = bit 24) -> negative offsets never sign-extended ->
backward calls (function defined before its call site) resolved to garbage
(0x2000018). Proven by encode/decode round-trip + byte-identical objects. Fixed
to `Sign_Extend(24,...)`, rebuilt via scons, deployed to `engine_new/.../bin`
(backups: bin_backup_jun18, bin_backup_jul5_unfixed). Reported to prof. Full
writeup: `prof_git_folder/BUG_REPORT_backward_call/`. Regression 36->39 PASS.
The new toolchain also requires `$call` in an 8-aligned bundle (return =
call_addr+8); the compiler already pads calls to 8-wide, so no codegen change.

**$vreduce MAX sub-op implemented** — closes the last integer-ISA gap. Audited
coverage: scalar ALU (all 12 ops + nand/nor/xnor), $cmov, $slice, $pack, $cast,
vector $v add/sub/mul (+replicate), $dot, and $ld/$st (all widths) were already
complete; only `$vreduce` was ADD-only. Empirically verified on the fixed
toolchain which reduce sub-ops actually work: **ADD and MAX work (all types);
MIN/MUL/OR/XOR/AND/XNOR return 0 (simulator-unimplemented)** -- so only MAX was
added (same "don't emit broken opcodes" policy as native abs/max/min).
Changes: `IRVecReduce` gained an `op` field (default '+'); `codegen` emits
`$vreduce <op>`; `ir_gen` maps `__vreduce_max_{type}` -> op '$max' (and keeps
`__vreduce_{type}` -> '+'); golden_stubs.h gained `__vreduce_max_*` references.
New test `new_isa_tests/test_vreduce_max` (3/3: sum 36, max-vi8 8, max-vi32 5,
gcc-verified). matmul_n16 recompiled 256/256, zero regressions.

Integer (scalar + vector) ISA is now functionally complete. Remaining ISA items
are all out of scope: FP arithmetic + $fsqrt (next phase), $scale (no C mapping),
vi4/vu4 (assembler/sim-broken E5), native abs/max/min (broken, covered via $cmov).

---

## 2026-06-26 — PHASE 3: loop-carried register promotion (counters/accumulators) WORKING. sumloop exec-loads -99%, test_matmul -69%, zero regressions

New pass `loop_reg.py` (`promote_loop_counters`) promotes loop-carried induction
variables / accumulators out of the per-iteration memory round-trip: load once into a
register in a preheader, do all in-loop reads/writes as register moves, write back once
at each loop exit. Relies on the existing loop-aware live-range extension to keep the
register alive across the back-edge; uses ONLY existing IR nodes (no codegen changes).

NARROW + conservative (user-chosen scope: counters only). A stack slot is promoted only if:
single-entry loop; the slot is touched everywhere in the function only via the exact
IRLoadAddr->immediate IRLoad/IRStore(offset 0) pattern with the address never escaping;
it is both loaded AND stored in the loop with one consistent width; and the loop body has
no call. A slot is promoted by at most ONE loop (innermost) -- `promoted_offsets` prevents
an enclosing loop from re-promoting a slot whose inner-loop preheader/write-back still
maintains its memory (this was a real nested-loop miscompile caught in test_matmul and
fixed before commit).

Wiring (compiler.py): TIERED fallback -- try LICM+loop-reg, then LICM-only, then
loop-reg-only, then baseline; keep the first that compiles with NO spilling and no crash.
This preserves the prior LICM win on register-starved kernels (matmul_n16 still takes
"LICM only" because LICM+promotion spills past the 28-reg wall) while letting simpler
loops take full promotion. Same "can only help, never miscompile" guarantee as LICM.
Added APARA_NO_LOOPOPT=1 debug knob to force the baseline for A/B measurement.

Measured on the real simulator (executed = dynamic, not static):
  sumloop (100-iter):  exec-loads 403 -> 4  (-99%);  non-null instrs 1930 -> 1343 (-30%)
  test_matmul (3x3):   exec-loads 281 -> 86 (-69%);  non-null instrs 1481 -> 1286 (-13%)
Both bit-exact correct (0 PostCondition errors).

Full regression after the change: **PASS=40, FAIL=1 (vreduce E4 only), CRASH=0, TIMEOUT=0**
over 63 programs. Zero correctness regressions.

This completes the 3-phase post-regression plan (1: regression baseline; 2: remaining ISA
instructions; 3: loop-carried register optimization).

## 2026-06-26 — REMAINING ISA INSTRUCTIONS: $nop parse-bug FIXED; $abs/$max/$min added as working $cmov-lowered intrinsics; native $abs/$max/$min audited as toolchain-broken (Latest)

Phase 2 of the post-regression plan ("implement whatever instructions are left in the
APARA ISA"). Audited every grammar mnemonic against what the compiler emits. Four were
legal-but-unemitted: `$abs`, `$max`, `$min`, `$scale`. Findings + actions:

**$nop parse bug — FIXED.** `_gen_IRNop` emitted `$nop`, but the grammar has no `$nop`
token (only `$null`), so any `__nop()` failed to assemble. Changed to emit `$null`.
Verified: `__nop()` now assembles and runs (0 errors). Resolves the long-standing
project_nop_parse_bug item.

**$abs / $max / $min — native opcodes are toolchain-broken; shipped as working $cmov
lowerings instead.** Empirically probed on the real simulator (test_minmaxabs.c):
  - `$max`/`$min` (scalar): assembler/disassembler rejects them ("Illegal instruction"),
    AND the scalar ALU executor `__uexec_64__` (McodeOperations.cpp) has no MAX/MIN case
    -> `default: assert(0)`. Unrunnable.
  - `$abs`: `__vabs_operation__` does `mask << nbits`, UB at nbits=64 -> returns 0 for
    every i64 input; at nbits=32 it leaves a stray bit-32 (got 0x10000007b vs 0x7b).
    Unreliable.
  Same "grammar-legal but simulator/assembler-broken" category as vi4/vu4 (E5) and the
  vreduce unsigned bug (E4). So rather than emit a broken native instruction, the
  `__abs/__max/__min` intrinsics now lower to the VERIFIED `$cmov` instruction:
    __abs(x)   = (x >= 0 ? x : -x)        [IRUnaryOp + IRCmov]
    __max(a,b) = ((a-b) > 0 ? a : b)      [IRBinOp '-' + IRCmov]
    __min(a,b) = ((a-b) < 0 ? a : b)      [IRBinOp '-' + IRCmov]
  Type suffixes __abs_i32/__max_u32/... select the $cmov type token (default $i64).
  New test new_isa_tests/test_minmaxabs.c: 8/8 PostConditions pass (incl. negative
  results -4/-8). golden_stubs.h gained the gcc reference definitions.
  (`$scale` left unimplemented: vector-scaling helper, no scalar-C mapping / no demand.)

Full regression after the change: **PASS=40 (was 39), FAIL=1 (vreduce E4 only), CRASH=0,
TIMEOUT=0** over 63 programs. Zero regressions; the new test accounts for the +1.

Next (phase 3): loop-carried register allocation for loop counters i/j/k.

---

## 2026-06-26 — REGRESSION BASELINE: full-suite run vs simulator, 39 PASS / 0 crashes / 0 timeouts (only the known vreduce simulator bug fails)

Ran a comprehensive, SAFE regression of every test program against the real simulator
(`/tmp/regress.sh`: timeout 90s + 20MB log cap per run, so a miscompile can never balloon a
log like the earlier 24GB runaway). Covers isa_coverage_tests, new_isa_tests, array, branch,
pointer, alu, ldst, matmul_tests -- 62 programs total.

Result: **PASS=39, FAIL=1, CRASH=0, TIMEOUT=0, SKIP/ABORT=22.**
- The single FAIL is `test_vreduce_full` (3 errors) = the documented simulator bug **E4**
  ($vreduce unsigned sign-extension), NOT a compiler fault.
- 0 crashes confirms the LICM spill/crash fallback guard holds across the whole suite.
- 0 timeouts confirms no remaining infinite-loop miscompiles.
- SKIP/ABORT = tests with no independent golden, plus the deliberate global/stack-overlap
  aborts (matmul_n64, test_matmul_while_n64).

This is the robustness baseline going into the next phase. Plan from here:
(1) [done] regression baseline; (2) implement the remaining in-scope ISA instructions
($abs, scalar $max/$min, $nop parse fix, the non-ADD $vreduce sub-ops); (3) loop-carried
register allocation for loop counters (executed-load reduction). Float and vi4/vu4 remain
out of scope (deprioritized / simulator-broken E5).

---

## 2026-06-26 — OPTIMIZATION (step 5): LICM + loop-aware register allocation NOW WORKING. matmul executed-loads -33.5%, zero regressions

Loop-Invariant Code Motion is now correct and enabled, delivering the runtime goal (fewer
executed loads/stores). Three pieces:

1. **`licm.py`** -- hoists loop-invariant address/load instructions out of inner loops into
   a preheader, with conservative memory-alias safety (traces base addrs to a specific
   global/stack region; bails on uncertainty/opaque-store/call).
2. **Loop-aware live-range extension (`codegen.py` `_compute_last_uses`)** -- the fix for the
   first LICM miscompile: any value DEFINED BEFORE a loop and USED INSIDE it has its live
   range extended to the loop's back-edge, so its register isn't reused mid-loop and the
   hoisted value survives every iteration. Verified INERT without LICM (byte-identical
   4-pass output), so zero risk to the baseline.
3. **Safety guard (`compiler.py`)** -- the fix for the second class of miscompile: the extra
   pressure from hoisted, loop-resident values can force SPILLING (or exhaust registers and
   crash codegen). Spilling a loop-live value across the back-edge is not reliable, so LICM
   is kept ONLY when its codegen neither spills nor raises; otherwise we fall back to the
   validated non-LICM codegen for that program (LICM can only help, never miscompile).

**Two real bugs found & fixed during this step** (both were infinite-loop miscompiles --
one produced a 24GB runaway simulator log): (a) textual-last-use register reuse across the
back-edge -> fixed by loop-aware liveness; (b) over-pressure spilling/crash on
test_matmul / test_matmul_packed / test_matmul_u128 -> fixed by the spill/crash fallback
guard (those programs now fall back to the identical validated 4-pass codegen).

**Validation (logs capped + timeouts this time to prevent runaway):** critical 19/19 PASS;
broad sweep **37 PASS / 0 new regressions** (vreduce = known bug E4; n64 = pre-existing
stack-overlap abort). matmul_n16 stays correct: 256/256.

**Runtime win (matmul_n16, executed at runtime -- the compute-speed metric):**
- loads executed **6441 -> 4281 (-33.5%)**
- total instructions **55201 -> 42665 (-22.7%)**
(static bundle count 68 -> 64; LICM relocates the invariant load to the preheader so it runs
16x per matrix instead of 256x -- the win is dynamic, not static.)

Backups: `compiler_backup_4pass/`, tags `opt-4pass-validated`, `opt-3steps-validated`.

---

## 2026-06-26 — OPTIMIZATION (LICM ATTEMPTED then DISABLED): correctly hoists invariant inner-loop loads, but MISCOMPILES because the register allocator is not loop-aware

Goal: cut *executed* loads/stores (runtime speed), specifically the matmul's redundant
re-load of A's row on every inner `j` iteration (row `i` is invariant across the j-loop ->
~240 redundant wide loads for 16x16, ~992 for 32x32).

**Implemented `compiler/licm.py`** -- a conservative Loop-Invariant Code Motion pass on the
flat IR: finds innermost loops, hoists pure address/load instructions whose inputs are
loop-invariant into a preheader, with strict memory-aliasing safety (traces base addresses
to a specific global/stack region; bails on any uncertainty, opaque store, or call). Verified
it DOES hoist correctly: A's row load moved out of the inner j-loop into the outer body
(once per i).

**But it MISCOMPILES matmul** -> garbage addresses (`Error: Unaligned address ... addr=1,2,3`)
-> runaway/effectively-infinite execution (256-check run produced a 97M-line log, 0 correct).

**Root cause (the important finding):** the codegen register allocator's liveness is **linear
/ textual** -- it frees a value at its last *textual* use and reuses that register later in
the same loop body. The inner loop has a **back-edge**, so on the next iteration the hoisted
A-row value (kept in `$r17`/`$r8`) has been overwritten -> the `$dot` reads garbage. The
allocator does **not extend live ranges across loop back-edges**, i.e. it is **not
loop-aware**. The memory-backed model masked this because it reloaded everything each
iteration (no cross-iteration register residency needed); LICM (and loop unrolling) both
*require* that residency.

**This is the SAME blocker that defeated loop unrolling.** Both load-reducing optimizations
need **loop-aware register allocation** (extend the live range of any value defined outside a
loop and used inside it to span the whole loop, so its register isn't reused mid-loop). That
is the real prerequisite -- a change to the validated codegen liveness -- and must come first.

**Action:** LICM is **wired off** in `compiler.py` (one commented block); `licm.py` is kept
for when loop-aware liveness lands. Compiler restored to the validated 4-pass state
(matmul_n16 = 68 bundles, 256/256 correct). Next step (pending go-ahead): implement
loop-aware live-range extension in codegen, then re-enable + re-validate LICM.

---

## 2026-06-26 — OPTIMIZATION (step 5 ATTEMPTED then REVERTED): loop unrolling -- correct but a net bundle-count REGRESSION without register renaming

Implemented induction-variable-substitution loop unrolling (factor 4) for counted
`while (V < N) { body; V++ }` loops, with a guard + remainder loop so it is correct for any
start/trip-count, and IV substitution done in `_load_var` (read of V in copy k means V+k)
rather than by AST rewriting. Heavily guarded: only straight-line bodies with no
break/continue/nested-loop/decl and no write to V.

**Correctness: PASSED** -- 19/19 aliasing-critical, 37 broad, zero new regressions
(matmul_n16 still 256/256 correct).

**But it REGRESSED the real metric (total bundle count = cycles):**
- matmul_n16 **68 -> 149**, matmul_n32 **74 -> 179** (roughly doubled).
- Unrolled matmul density only **1.98** (not the 5-8 hoped for).

**Root cause:** the linear-scan register allocator REUSES the same registers across the
unrolled copies, creating false WAR/WAW dependencies that the (correct) scheduler must
respect -- so the 4 independent iterations cannot interleave. Net effect: 4x the code at the
same ~2 density = ~2x the bundles. Making unrolling pay off needs **register renaming**
(distinct registers per copy) -- a much larger change, and throttled by the 28-register
limit anyway.

**Decision: reverted** (`ir_gen.py` restored to the validated 4-pass state, commit 06f936c;
backup at `compiler_backup_4pass/`, tag `opt-4pass-validated`). The 4-pass compiler
(caching + storage-class aliasing + CSE + scheduling) is the final optimized deliverable:
test_valu_full -74%, test_alu_full -65%, matmul -28..33%, density 1.69->2.14, bundles reach
8-wide -- all with zero correctness regressions. Loop unrolling is left as documented future
work (requires register renaming first).

---

## 2026-06-26 — OPTIMIZATION (step 4): instruction scheduling (list scheduling in bundler.py); big density win, bundles now reach 8-wide. Zero regressions

Added a within-basic-block **list scheduler** in `bundler.py`, run between `_parse_flat` and
`_pack_bundles`. It reorders mutually-independent instructions so they end up adjacent, which
lets the existing greedy packer form much denser VLIW bundles.

**Implementation (`bundler.py`):**
- `_must_precede(a,b)`: the dependency oracle -- a must stay before b on any of RAW / WAW /
  **WAR** (anti-dependence; essential now that registers are reused) / store-ordering
  (a store cannot cross another memory op) / call / control-transfer barrier. Conservative:
  extra edges only cost optimization, never correctness.
- `_schedule_block`: builds the predecessor sets, then list-schedules with a bundle-aware
  greedy priority (prefer a ready instruction that can join the currently-forming bundle;
  else close it and take the lowest-index ready one). Block labels are reattached to the
  first scheduled instruction; the control-transfer naturally schedules last (every other
  instruction is its predecessor).
- `_schedule_within_blocks`: splits the flat stream into basic blocks (a labelled instruction
  starts one, a control-transfer ends one) and schedules each. `_pack_bundles` then re-packs
  the reordered stream and re-applies ALL its hazard checks (so the phase hazard etc. are
  still enforced at packing time).
- Correctness rests entirely on `_must_precede` being complete: only independent instructions
  are ever moved past each other, so the reordered stream is semantically identical.

**Validation:** 19/19 aliasing-critical PASS; broad sweep 37 PASS, 0 new regressions
(vreduce = known bug E4; n64 = pre-existing stack-overlap abort). This was the riskiest pass
(it physically reorders post-register-allocation code) so it was validated the same way and
specifically re-checked on struct/pointer/2D/call/control-flow tests.

**Density:** average real-instructions-per-bundle (no nulls counted) rose **1.69 -> 2.14**,
and bundles now reach the full **8-wide** (distribution gained 4..8-instruction bundles;
e.g. 25 bundles at 8 across the sampled set). 5+ instruction bundles are now common in
parallel regions (vector ALU, dot kernels).

**Final cumulative bundle reduction (pre-optimization backup -> now, all 4 passes):**
| test | pre-opt | now | reduction |
|---|---|---|---|
| test_valu_full | 142 | 37 | -74% |
| test_alu_full | 68 | 24 | -65% |
| matmul_n32 | 111 | 74 | -33% |
| matmul_n16 | 95 | 68 | -28% |
| test_2d | 181 | 140 | -23% |
| test_scalar_full | 348 | 294 | -16% |

Backup of the validated 3-step compiler at `compiler_backup_3steps/`; git tag
`opt-3steps-validated`. **Next (optional): loop unrolling of the matmul kernel** to push the
hot-loop density toward 8-wide (matching the ISA spec's unrolled-dot example).

---

## 2026-06-26 — OPTIMIZATION (step 3/3): common subexpression elimination; matmul_n16 95->76 (-20%), matmul_n32 111->86 (-22.5%). Optimization plan COMPLETE, zero regressions

Final optimization step: eliminate recomputation of identical arithmetic expressions whose
operands are unchanged within a straight-line region -- directly fixes the documented
root cause (the `i*16` computed twice in `results[i*16+j] = dot(&A[i*16], ...)`).

**Implementation (`ir_gen.py`), local value numbering layered on register caching:**
- New `_cse_table` keyed `(op, left_operand_key, right_operand_key)` -> result Temp.
- New `_emit_binop(op,l,r)`: returns the existing result if the same op on the same operands
  was already computed (and still valid), else emits a fresh IRBinOp and records it.
- Routed the arithmetic path of `_binop`, plus the address-scaling sites
  (`_scale_by_stride`, `_array_base_off`'s index*elem_size), through `_emit_binop`.
- Correctness: this works *because* register caching already returns the SAME Temp for
  repeated reads of a variable, so the second `i*16` sees identical operands. The table is
  cleared at every basic-block boundary / call (the `_CACHE_FLUSH_NODES` path in `_emit`),
  so a result is never reused on a control path where it might not have been computed. It is
  NOT cleared on plain stores (pure arithmetic results don't depend on memory), which keeps
  CSE alive across stores within a block.

**Validation:** 19/19 aliasing-critical PASS; broader sweep 37 PASS, 0 new regressions
(vreduce = known bug E4; n64 = pre-existing stack-overlap abort).

**Final cumulative gains (vs pre-optimization backup `compiler_backup_preopt/`):**
| test | before | after | reduction |
|---|---|---|---|
| test_alu_full | 68 | 37 | -46% |
| matmul_n32 | 111 | 86 | -22.5% |
| matmul_n16 | 95 | 76 | -20% |
| test_2d | 181 | 173 | -4.4% |
| test_array | 51 | 46 | -10% |
| test_scalar_full | 348 | 344 | -1% |

**The 3-step deferred-optimization plan (register caching -> storage-class aliasing -> CSE)
is now COMPLETE**, every step committed separately, every step validated end-to-end against
the real simulator with zero correctness regressions.

---

## 2026-06-26 — OPTIMIZATION (step 2/3): storage-class alias analysis; test_alu_full 68->37 (-46%), matmul_n16 95->79, matmul_n32 111->91. Zero regressions

Refines step-1 register caching so an aliasing memory write only flushes the cache entries
it can actually touch, instead of the whole cache.

**Key idea:** globals live in the global DMEM region (from GBASE) and stack locals live in
the stack region -- they are **disjoint**, so a global-memory write cannot alias a stack
local and vice-versa.

**Implementation (`ir_gen.py`):**
- Cache entries now carry a storage class: `_var_cache[name] = (value, 'global'|'local')`.
- New `_flush_cache(scope)`: `scope='all'` clears everything; `'global'`/`'local'` clears
  only that class.
- `_emit` consults `self._store_scope` on an aliasing store and flushes just that scope,
  then resets it to the safe default `'all'`. Any un-annotated store therefore still flushes
  everything (correctness-preserving default).
- Annotated the high-value, provably-correct store sites in `_assign_lval`: a **global
  array** write sets scope `'global'` (can't touch locals); a genuine **local array** write
  (name not a pointer) sets `'local'` (can't touch globals); pointer derefs / structs / 2D /
  wide-intrinsic stores keep the default `'all'`.
- Basic-block boundaries and calls still flush `'all'` (a call may modify globals and may
  reach locals via an escaped pointer).

**Validation:** same harnesses as step 1. Aliasing-critical set **19/19 PASS**; broader
sweep **37 PASS, 0 new regressions** (vreduce = known bug E4; n64 = pre-existing
global/stack-overlap abort). Re-checked struct/pointer/2D/call tests specifically since this
step loosens flushing.

**Measured gains (cumulative, vs pre-optimization backup):**
- test_alu_full **68 -> 37 (-46%)** -- a/b now stay cached across the `results[]` writes.
- matmul_n16 95 -> 79 (-17%), matmul_n32 111 -> 91 (-18%), test_array 51 -> 46,
  test_scalar_full 348 -> 344.

**Next step (3/3, not yet done):** common subexpression elimination -- eliminate the
`i*16`-computed-twice address recomputation (local value numbering layered on the cache).

---

## 2026-06-26 — OPTIMIZATION (step 1/3): register caching implemented in ir_gen.py, full suite re-validated with ZERO correctness regressions

First optimization pass after the thesis presentation (the deferred bundle-efficiency work).
Implements **register caching**: repeated reads of a named variable within a basic block
reuse the value already in a register instead of re-loading it from memory.

**Implementation (`ir_gen.py` only; backup of all 5 .py files at `compiler_backup_preopt/`):**
- New `self._var_cache` dict: variable name -> Temp/Const currently holding its value.
- `_load_var`: returns the cached value if present; otherwise loads, caches, returns.
- `_store_var`: forwards the stored value to the cache **only for 8-byte (full-width)
  values**; for narrower types it invalidates instead, because store truncates to the type
  width and a later load re-applies sign/zero extension (store->load is NOT identity for
  sub-word types -- this was a real bug caught by `test_subword_full`, see below).
- Cache flushing (conservative, correctness-first) via `_emit`: flushed at every
  basic-block boundary (IRLabel/IRJump/IRCondJump/IRReturn/IRFuncBegin/IRFuncEnd/IRHalt),
  every call (IRCall/IRIndirectCall -- callee may modify globals/locals via pointers), and
  every aliasing memory write. The named-scalar store uses an `_in_named_store` flag so it
  does NOT flush other variables (it cannot alias them); every OTHER store
  (array/pointer/struct/wide, i.e. not from `_store_var`) flushes the whole cache.

**Bug found & fixed during this step:** initial version forwarded ALL stored values to the
cache, which broke sub-word semantics -- `test_subword_full` failed 4 checks (sign-extension
/ truncation: e.g. Mem=0x80 vs expected 0xffffffffffffff80). Fixed by restricting
store-forwarding to 8-byte values (load-caching remains safe for all widths since it caches
the already-extended result of a real load).

**Validation (end-to-end against the real simulator, `engine_new/.../mcode_run`):**
- Curated aliasing-critical set (struct, pointer, 2D array, calls, control flow): **19/19 PASS**.
- Broader sweep: **37 PASS, 0 real failures**, 20 skipped (no independent golden).
- The 2 apparent "fails" are NOT regressions (identical with the pre-opt backup compiler):
  `test_vreduce_full` = the known simulator bug E4 (3 errors); `test_matmul_while_n64` =
  the pre-existing global/stack-overlap safety abort (needs a larger `--stack-top`).

**Measured gains (correct, validated):** matmul_n16 95->86 (-9.5%), matmul_n32 111->98
(-11.7%), test_scalar_full 348->344. `test_alu_full` unchanged (68->68): its `results[]`
global-array writes conservatively flush the cached local scalars a/b -- a global and a
stack local cannot actually alias, so this is the next refinement.

**Next steps (planned, not yet done):** (2) storage-class alias analysis (a global-memory
write should not flush cached locals, and vice-versa -- unlocks test_alu_full and compounds
everywhere globals/locals mix); (3) common subexpression elimination (attacks the
`i*16`-computed-twice address recomputation directly).

---

## 2026-06-20 — Clarification: engine_isp/assembler/bin/mcode_run is the authoritative simulator binary; verification/bin/mcode_run is not

Confirmed via `ls -lh` on both directories, in response to an earlier report draft that treated the
two binaries as just "different" without saying which one is correct:

```
engine_isp/assembler/bin/mcode_run   1.8M  Jun 18 13:05   <- authoritative
verification/bin/mcode_run           1.7M  Jun 20 12:22   <- NOT authoritative
```

`engine_isp/assembler/bin/mcode_run` is the binary this project's `run.sh` pipeline has used
throughout, and is the one all 5992 verification checks across `isa_coverage_tests/`,
`matmul_tests/`, and the pre-existing baseline suite were run against. `verification/bin/mcode_run`
is a separate, non-canonical build (confirmed different MD5; the same directory also holds a `tb`
binary and a 243MB `test_setup_test_bench`, pointing to a different RTL/testbench-oriented
verification setup, not this project's compiler-targeted simulator) — despite having a *newer*
file timestamp, which is not evidence of correctness on its own. It expects a different
`PostCondition` file format (`<keyword> <args...>`, no leading thread-id) than the authoritative
binary (`<thread-id> <keyword> <args...>`) — see `ENGINE_ISP_BUG_REPORT.md` for the full writeup,
including the two confirmed code-level bugs in the authoritative binary's source
(`McodeOperations.cpp`'s `__vreduce_operation__` unsigned sign-extension bug, and
`McodeAccelerator.cpp`'s `Verify_Line` misleading error messages).

Several pre-existing example result files elsewhere in this project tree (e.g. under
`verification/lastsem/`) appear to have been written against the non-authoritative binary's format
— worth keeping in mind if any of those are ever reused, since they won't verify correctly against
the authoritative one.

---

## 2026-06-20 — All 25 pre-existing tests restructured to results[]: 393 independently-verified checks, 0 errors. Plus one more real compiler bug found and fixed

Per request, ran the golden-verification treatment across every remaining pre-existing test (the
22 that had ZERO real verification before today -- confirmed by checking each one's actual output:
branch/loop-containing programs got an empty placeholder, since the static-eval path can't handle
control flow -- plus the 3 that already had some static-eval coverage). Final tally: **393
individually-verified PostCondition checks across all 25 tests, 0 errors.**

Restructured: `array/test_array` (6), `test_struct` (17, all 8 documented sub-tests),
`test_2d` (15, all 6 sub-tests incl. the 2x2 matmul), `branch/test_branch` (6, all 6 conditions),
`pointer/test_pointer` (10, all 10 sub-tests), `new_isa_tests/test_subword` (12), `test_dot` (2),
`test_scalar_full` (11 -- for the first time ever verifies the function-call results that
`g_func_res` computed but never checked), `test_vadd` (4, now full packed results not just the low
element), `test_vreduce` (3), `test_cast` (4), `test_cmov` (3), `test_pack` (1 -- resolves the
historical "0xdead" result as a bit-order documentation issue, not a bug), `test_matmul` (9),
`test_u128_load`/`test_u256_load` (2/4), `test_dot128_split` (1), `test_matmul_packed` (256, full
cell-by-cell via the split load+dot path), `test_dot128_direct` (1), `test_u128_store`/
`test_u256_store` (2/4), `test_spill` (1, see below).

Two real bugs found and fixed along the way:
1. **`test_slice.c`'s `__slice` declared `(int,int,int)` while `golden_stubs.h`'s reference is
   `(long long,int,int)`** -- gcc correctly rejected this as a conflicting declaration, caught and
   reported by `try_golden_verify`'s fallback (not a silent failure). Fixed the test's declaration.
2. **`eval_ir` (the static-eval fallback used for branch-free, non-`results[]` programs) doesn't
   scope its evaluation to `main`** -- it walks the full flattened instruction list (every
   function's body back to back, in declaration order) and breaks on the FIRST `IRReturn` it
   finds, which can belong to any function declared before `main`. `test_spill.c` defines
   `f01()..f30()` before `main`; the old code broke on `f01`'s `return 1` and confidently reported
   `r1=1` instead of the correct `465` -- a **wrong answer that looked like a working result**, not
   a placeholder. Confirmed by checking `test_spill.result`'s actual content against the simulator's
   real output (`0x1d1`=465) before assuming anything. Fixed by tracking `IRFuncBegin`/`IRFuncEnd`
   boundaries and only evaluating instructions while inside `main`'s scope. `test_spill.c` was also
   restructured to `results[]` regardless, sidestepping the whole static-eval path going forward.

Full regression re-run after both fixes: all 25 tests, 393 checks, 0 errors.

---

## 2026-06-20 — Golden verification wired into compiler.py: one command now produces a real, independently-verified .result

Per explicit request: `python3 compiler.py test_X.c` is now the single command that produces both
`data.map` (already automatic) and a REAL `.result` file, not a placeholder -- as long as the test
follows the `results[]` convention used throughout `isa_coverage_tests/` and `matmul_tests/`.

`try_golden_verify()` (new function in `compiler.py`) runs automatically after every compile:
finds a global literally named `results` in `ir_globals` (no source-text parsing needed -- the
address and element size are already known internally), compiles the same preprocessed source
natively with `gcc` against `isa_coverage_tests/golden/golden_stubs.h`, captures every slot's
ground-truth value, and writes the `.result` file. Falls back cleanly (with a printed reason, never
silently) to the existing static-eval/placeholder path if there's no `results[]`, `gcc` isn't
available, `golden_stubs.h` is missing, or the native build/run itself fails.

Verified: re-running `compiler.py` alone (no separate `golden_gen.py` call) on `test_alu_full.c`,
`matmul_n16.c`, and `test_vreduce_full.c` (the one with intentionally-architecturally-correct values
for the known-buggy unsigned cases) produces `.result` files byte-identical to the ones already
verified against the simulator. Full existing 25-test suite re-run -- zero regressions.

**Also found and fixed, same investigation**: the pre-existing static-eval fallback
(`write_result_file`, used for any branch-free program with no `results[]`) had the exact same
missing-leading-thread-id format bug found and fixed earlier for `isa_coverage_tests` -- it wrote
`reg 0x1 0x...`/`mem 0x... 0x...` (3 tokens), which the real simulator silently ignores entirely
(confirmed: zero `PostCondition` output either way). Every branch-free program compiled by this
project before today produced a `.result` that looked plausible but verified nothing. Fixed to
`0 reg 0x1 0x...`/`0 mem 0x... 0x...`, confirmed against the simulator with a fresh static probe.

`golden_gen.py` kept as a standalone tool (regenerate just the golden file without a full
recompile) but marked superseded for normal use -- `compiler.py` is now the authoritative path.

---

## 2026-06-20 — ISA coverage audit closed: 12 new test files, 6 real compiler bugs fixed, 1 simulator bug found and flagged

Closing entry for the systematic instruction-coverage sweep (`isa_coverage_tests/`, see its
`README.md` for the full matrix). Final state: **12 new test files + the existing 25-test suite,
37 tests total, all passing on hardware, zero regressions.**

Bugs found and fixed in the compiler during this sweep (chronological, each already has its own
detailed entry below):
1. Unsigned char/short/int sign-extended on every load instead of zero-extending.
2. `$cast` was a no-op whenever casting directly from a 64-bit value.
3. Global data area could silently overlap the stack with no check.
4. `$st ($u128)/($u256)` (wide store) didn't exist at all.
5. **Function parameters narrower than 64 bits always read back as garbage** — the single most
   significant finding, affecting every function with an `int`/`short`/`char` parameter.
6. The calling convention's hard 4-argument ceiling silently dropped extra args with no error.

Found but NOT fixed (simulator-side, flagged for a decision): `$vreduce` on unsigned vector types
sign-extends instead of zero-extending, traced to a variable-shadowing bug in
`McodeOperations.cpp`'s `__vreduce_operation__` — the compiler emits the correct type tag, the
simulator's execution is wrong.

Confirmed, intentionally not pursued: `$abs`, standalone `$max`, `$nop`'s parse bug, float
arithmetic, `$vreduce`'s MAX/MUL/AND/OR/XOR/XNOR sub-ops, `vi4`/`vu4`.

This is the closing state for this audit — see `isa_coverage_tests/README.md` for the full
per-instruction coverage matrix suitable for the report.

---

## 2026-06-20 — MAJOR FIX: narrower-than-64-bit function parameters always read back as garbage

The most significant finding of tonight's ISA-coverage audit. Found while building
test_call_return_full.c: `int add1(int a) { return a + 100; }` called as `add1(7)` returned 100,
not 107 -- the parameter `a` was reading as 0 inside the function, regardless of what was passed
(confirmed with both a literal and a variable argument; confirmed pre-existing on the unmodified
compiler via git stash, not something introduced tonight).

**Root cause**: the function prologue stores each incoming argument register to the parameter's
stack slot via a hardcoded `$st ($i64) [...]` (full 64-bit store) regardless of the parameter's
actual C-level type, while reads of that parameter correctly use its real width (`$ld ($i32)` for
a plain `int`). Per this compiler's established DMEM convention (`_alloc_global`'s comment: "$ld
($i32) always reads bits[63:32] of the 8-byte DMEM word"), a 32-bit load reads the *upper* 32 bits
of the 8-byte word -- so a parameter stored as a full 64-bit value (lower bits = the real value,
upper bits = 0 for any small value) gets read back as 0, the upper half. `IRFuncBegin.params` only
ever carried `(name, fp_offset)` -- the parameter's width was computed correctly elsewhere
(`_alloc_local`'s `esz`) but never threaded through to the prologue's store.

**Why no existing test caught this**: `test_scalar_full.c` calls `add3`/`max2`/`fact` (all `int`
params) and stores results in `g_func_res` -- but never actually asserts `g_func_res` against its
expected value (205, per its own comment). Verified directly: the exact same call pattern,
isolated, returns `1` (wrong) on the unmodified compiler and `0xcd`=205 (correct) after this fix.
Every C function with a parameter narrower than `long long` was silently broken.

**Fix**:
- `ir_gen.py` (`visit_FuncDef`): `param_list` entries are now `(name, fp_offset, elem_bytes)` --
  3-tuples instead of 2; also now registers parameters in `self._unsigned_vars` (a related,
  previously-missed gap: an `unsigned int` parameter was never tracked there either, since
  `visit_FuncDef`'s parameter loop never called `_is_unsigned_decl`, only `visit_Decl` did).
- `codegen.py` (`_gen_IRFuncBegin`): prologue store now uses `self._atype(width)` per parameter
  instead of a hardcoded `($i64)`.

Verified end-to-end: the minimal probe (7+100=107), the real test_scalar_full.c pattern in
isolation (205), and the new test_call_return_full.c (4-arg call, 3-level nesting, factorial AND
fibonacci recursion -- the latter stressing the RAS with two recursive calls per invocation, a
different push/pop pattern than a single recursive chain). Re-ran the full existing suite (34
tests) on hardware -- every result identical to before, zero regressions, including
test_scalar_full.c's outer r1=0xc (the bug was in a value that test's own pass/fail signal never
depended on, exactly why it went undetected).

Also fixed alongside (same investigation, smaller and unrelated to the width bug): the calling
convention hardcodes exactly 4 argument registers (r2-r5) on both caller and callee sides -- a 5th+
argument/parameter was previously silently dropped with no error. `_gen_IRFuncBegin`/`_gen_IRCall`/
`_gen_IRIndirectCall` now fail loudly at compile time instead (`sys.exit(1)` with a clear message),
matching the global/stack-overlap check added earlier tonight. Not a capacity fix (no stack-passed
args), just fail-loudly instead of silently-wrong, by design -- extending the ABI to support 5+
args via stack passing would be a separate, bigger feature.

---

## 2026-06-20 — FOUND (not fixed -- simulator-side, out of compiler scope): $vreduce unsigned sub-types sign-extend instead of zero-extend

Found while building test_vreduce_full.c. Unlike every other bug found tonight, this is a
**hardware/simulator bug, not a compiler bug** -- the compiler correctly emits
`$vreduce + rd ($vu8) rs` (confirmed in the generated mcode); the simulator's execution is wrong.

Root cause, traced into `McodeOperations.cpp`'s `__vreduce_operation__` (~line 110): it
unconditionally sign-extends each element into a local `r` (~line 151, *before* checking
`signed_flag`), then the `if(signed_flag)` branch redeclares its own (identical, redundant) `r`
and uses it -- but the `else` (unsigned) branch has no such redeclaration and silently reuses the
*outer*, already-sign-extended `r` instead of the raw element value. So `$vreduce` on `$vu8`/
`$vu16`/`$vu32` currently sign-extends exactly like the signed variant, with no zero-extension
path actually reachable.

Verified empirically across all three widths (not just assumed from reading the source): a vector
with one negative-bit-pattern element gives the SAME sum for `__vreduce_vu8`/`vu16`/`vu32` as
their signed counterparts (28/2/-1), not the architecturally-correct zero-extended sums
(284/65538/4294967295).

`isa_coverage_tests/test_vreduce_full.c` asserts the CONFIRMED actual behavior (not the
architecturally-correct one), with comments explaining the discrepancy clearly, so the test suite
reflects reality rather than silently failing or asserting something false. r1=0x1, zero pipeline
errors -- 12 checks, 6 positive-only (signed/unsigned agree, as expected) + 6 documenting this bug.

Also confirms a separate, already-known gap from the ISA audit: only the ADD sub-op is reachable
from this compiler (`_gen_IRVecReduce` hardcodes `$vreduce +`) -- MAX/MUL/AND/OR/XOR/XNOR sub-ops
are real per the ISA doc but never emitted, not tested here.

**This needs your call**: fixing it means editing the simulator's C++ source
(`engine_isp/assembler/src/McodeOperations.cpp`), which is a different codebase/scope than the
Python compiler this whole project has been about. Flagging for your decision rather than
silently fixing or silently ignoring it.

---

## 2026-06-20 — Fix: $cast was a no-op whenever casting straight from i64

Found while building test_cast_full.c for the coverage suite: `long long r = (unsigned char)(-1);`
gave -1, not 255 -- the cast did nothing. `int8_t a = (int8_t)big;` (the existing test_cast.c's
pattern) never caught this because its result always gets assigned to a narrow named variable,
and the subsequent store-truncate + load-extend round trip (the latter just fixed above) produces
the correct value regardless of what $cast itself computed -- masking the bug completely whenever
the cast's result lands in a narrow variable, and only exposing it when assigned to something wide
(`long long`) or used directly in an expression.

Root cause, traced into the simulator (`McodeOperations.cpp ___cast_operation___`): scalar cast
execution masks/sign-or-zero-extends using the SECOND type tag's width and unsigned flag (i.e.
`Break_Vector(src_type.Get_Nbits(), ...)`), not the first. `ir_gen.py` was emitting
`$cast (narrow_type) rd ($i64) rs` -- with `$i64` (64 bits, always "signed") in the position that
actually drives the computation, making it an unconditional no-op (mask to 64 bits + sign-extend
from bit 63 are both no-ops on an already-64-bit value) regardless of the narrow C-level dest type.

This isn't a hardware bug -- the mechanism is clearly designed for $cast's other documented use
(vector element-width widening/narrowing, e.g. vi8->vi16), where "break the SOURCE register into
its native element width, then sign/zero-extend each element per the source's signedness" is
exactly right. The scalar narrowing use case just needs the narrow type fed into that same slot.

Fix: swapped the call to `IRCast(res, expr_val, '$i64', dest_type)` -- one line in `ir_gen.py`,
with a comment explaining the inversion since "$i64 first, narrow type second" reads backwards
from the natural "cast FROM i64 TO u8" intuition. `IRCast`/`_gen_IRCast` themselves needed no
change (they already just pass both type tags straight through to mcode text).

Verified: `(unsigned char)(-1)` now gives 255, `(signed char)(-1)` still gives -1, `(signed
char)(200)` gives -56, `(unsigned char)(200)` gives 200 -- all direct-use (no intermediate
variable) cases. Re-ran the full existing suite (27 tests) on hardware -- every result identical
to before this fix, confirming test_cast.c's narrow-variable pattern truly never exercised the
buggy path. Zero regressions.

---

## 2026-06-20 — Fix: unsigned char/short/int were sign-extended on every load

Found while building a systematic ISA-coverage test suite (isa_coverage_tests/): `unsigned char
uc = 255; if (uc != 255) ...` failed. Root cause: `codegen.py`'s `_atype`/`_WIDTH_TO_TYPE` mapped
byte-width straight to the SIGNED type tag ($i8/$i16/$i32) with no unsigned variant at all --
every load of a narrower-than-64-bit value sign-extended, regardless of the C-level `unsigned`
qualifier. Confirmed at the simulator level too (`MachineRun.cpp`:
`signed_flag = !t.Get_Float_Flag() && !t.Get_Unsigned_Flag()`) that `$ld ($u8)` vs `$ld ($i8)`
is exactly the zero- vs sign-extend switch needed -- store doesn't need this fix (the grammar
explicitly ignores `$u` there, truncation is the same either way).

This affected every `unsigned char`/`unsigned short`/`unsigned int` scalar and 1D-array read in
the entire compiler -- a real, previously-undiscovered correctness bug, not a new feature gap.

Fix, scoped to LOAD only:
- `ir.py`: `IRLoad`/`IRGlobalLoad` gained an `unsigned=False` flag.
- `codegen.py`: `_atype(elem_bytes, unsigned=False)` now picks between `_WIDTH_TO_TYPE` and a new
  `_WIDTH_TO_TYPE_U` ($u8/$u16/$u32/$u64); `_gen_IRLoad`/`_gen_IRGlobalLoad` pass it through.
- `ir_gen.py`: new `_is_unsigned_decl()` (recurses into array element / pointer pointee types --
  a bare pointer's own type always maps to `$u64` regardless of pointee, which would otherwise
  make every pointer name look "unsigned" and leak into `p[i]`'s actual element signedness).
  New `self._unsigned_vars` set, kept in sync by explicit add/discard on every `visit_Decl` rather
  than reset-per-function, to avoid the exact global-wipe bug class `_global_array_elem` was
  already split out to avoid. Wired into `_load_var` and `_arrayref` -- the only two paths that
  ever load narrower than 8 bytes (struct fields, 2D array elements, and pointer-value-itself
  loads are all confirmed always-8-byte, so signedness is moot there).

Verified: the probe now reads back 255, not -1, with `$ld ($u8)` confirmed in the generated mcode.
Re-ran the full existing suite (25 tests) on hardware, not just compile-stage -- all results
unchanged (e.g. test_pack's `0xdead` vs its comment's expected `0xBEEF` is confirmed pre-existing
and unrelated via a direct mcode diff with/without this fix -- likely just a wrong assumption
about __pack's argument order in that test's comment, to be resolved properly in the new coverage
suite). Zero regressions.

---

## 2026-06-20 — Implemented $st ($u128)/($u256) wide store

Closed the gap found during the full ISA-coverage audit: wide *load* (`$ld ($u128)/($u256)`) was
built and hardware-proven weeks ago for the matmul work, but wide *store* was never built —
every result so far had been scalar, so the need never came up.

Verified the real semantics from the assembler grammar (`isa.g`, `mcode_store_instruction`)
before writing anything: the ISA doc's own `$st ($u128)` example text is a copy-paste artifact
from the load section ("will fetch... keep it in rd,rd+1" makes no sense for a store) — the
grammar confirms `$st` takes a single `rd` token just like `$ld`, so the hardware reads
`rd..rd+n-1` as the *source* register group being written to memory.

- `ir.py`: new `IRStoreWide(srcs, base, offset)`, mirroring `IRLoadWide` in reverse.
- `codegen.py`: new `_gen_IRStoreWide` — borrows an aligned pair/quad transiently (same
  `_safe_borrow_pair`/`_safe_borrow_quad` as load), copies each source value into it, emits one
  `$st`, releases the borrow immediately. Added to `_get_src_temps` for liveness (`srcs` are read).
- `bundler.py`: the `$st` hazard regex only ever matched `\$i\d+` and captured a single source
  register — exactly the same class of bug the wide-load fix addressed on 2026-06-17, just never
  triggered until now. Fixed to match `\$[iu]\d+` and mark `rs..rs+n_regs-1` as read, not just `rs`.
- `ir_gen.py`: new intrinsics `__st128(dst, src)` / `__st256(dst, src)`, mirroring `__ld128`/
  `__ld256` in reverse (plain 64-bit loads of the source halves/quarters, one wide store).

Verified end-to-end on hardware: `test_u128_store.c` / `test_u256_store.c` (mirrors of the
existing load tests), both `r1=0x1`, zero pipeline errors, exact byte patterns confirmed in the
generated mcode (`$st ($u128) [...] $r8`, `$st ($u256) [...] $r12`). Re-ran the full existing
suite (compile-stage, 25 tests) plus hardware re-execution of the highest-risk subset
(`test_matmul_packed_direct`, `test_spill`, `test_struct`, `test_cast` — all touch `$st` heavily
via results/spilling/struct fields) — zero regressions.

---

## 2026-06-20 — Fix: compiler now refuses to silently overlap globals with the stack

Root-caused and fixed the bug found while building the N=64 scaling case: the compiler had no
check that the global data area actually fits below `--stack-top` before emitting code. Globals
grow up from `global_base`, the stack grows down from `stack_top` — if the global area's end
address gets within (or past) `stack_top`, the two regions overlap and silently corrupt each
other with no error, no crash, just wrong values in whichever variables happen to land in the
overlap. This is exactly what happened with N=64's matmul: none of the original corner
spot-checks touched the corrupted range, so it would have passed as "correct" without the
follow-up check.

Fix in `compiler.py` (`compile()`, right after IR generation so `ir_gen._next_global` is final):
errors out with `sys.exit(1)` if `_next_global + 4096 > stack_top` (4KB safety margin — generous
versus the actual frame costs seen in this compiler, ~800B for non-recursive functions), printing
the exact byte counts and a concrete `--stack-top` suggestion. Verified both directions: N=64
without an explicit `--stack-top` now fails loudly with a clear message instead of compiling
silently-wrong code; N=64 with `--stack-top 0xfff8` still compiles normally. Re-ran all 23 existing
tests (alu/array/struct/2d/branch/ldst/pointer/subword/dot/spill/scalar_full/vadd/vreduce/slice/
cast/cmov/pack/matmul/u128_load/u256_load/dot128_split/matmul_packed/dot128_direct) — zero false
positives, all still compile.

---

## 2026-06-20 — Scaling table: 38-bundle fused intrinsic + while-loop savings, at N=8/16/32/64. Final data for the report

### The table
| N | Matmul-loop bundles | Reference | Notes |
|---|---|---|---|
| 8 | 36 | — | compiler output, no hand-written equivalent for comparison |
| 16 | 37 | 19 | only size with a hand-written baseline; while-loop version, down from 38 with `for` |
| 32 | 52 | — | compiler output, no hand-written equivalent for comparison |
| 64 | 80 | — | compiler output, no hand-written equivalent for comparison |

All four: `__dot128_direct_vu8` (the verified, fused, no-round-trip intrinsic), no unrolling, no
batching — both confirmed worse earlier tonight. All loops converted to `while` to capture the
1-bundle-per-nest savings found earlier. Correctness verified at every size via hand-computed
spot-checks (same `a_val=(r*N+k+1)%256`, `b_val=(k*N+c+1)%256` formula as the original 16x16
reference, computed independently for each N): N=8 — `C[0]`/`C[7]`/`C[63]`; N=16 — `C[0]`/`C[15]`/
`C[255]`; N=32 — `C[0]`/`C[31]`/`C[1023]`; N=64 — `C[0]`/`C[63]`/`C[4095]`/`C[2900]`. All pass,
zero pipeline errors at every size. No compiler source was modified for this experiment — only
new test C files using already-existing, already-verified intrinsics.

### A real bug caught before trusting N=64's result
N=64's global data footprint (`A`+`BT`+`C` combined) needs up to address `0xa400`, which overlaps
the *default* `--stack-top` (`0x7ff8`) — the stack and `C[2840..2943]` would have silently
corrupted each other. Caught by checking the actual address ranges, not by symptom — none of the
three original corner spot-checks would have touched that range, so this would have silently
passed as "correct" otherwise. Fixed by recompiling with `--stack-top 0xfff8` (well above the
global area), and added a fourth spot-check (`C[2900]`) specifically inside the
previously-corrupted range to prove the fix rather than assume it. N=8/16/32 have small enough
footprints that they never approach the default stack-top, so they were not at risk.

### Log files: N=32 and N=64 omitted from their commits
N=32's verbose run log was 143MB, N=64's was 725MB — both far over GitHub's 100MB file limit (the
`-v` trace scales with total dynamic instruction count, which grows fast with N). The N=32 commit
was amended before it was ever pushed (so nothing public was rewritten) to drop the log; N=64's
log was deleted before staging. All other artifacts (`.mcode`/`.obj`/`.aligned.mcode`/
`.disass.mcode`/`.result`) are small and committed normally at every size.

### Observation, not chased further (per instruction)
Bundle growth isn't linear in N: 36→37→52→80 for N=8→16→32→64. This tracks the number of
`__dot128_direct_vu8` calls needed per dot product (`ceil(N/16)`: 1 for N=8/16, 2 for N=32, 4 for
N=64) rather than N itself — N=8→16 barely moves because both still need exactly one call per dot.
Not investigated further; this is the final data point for the report, not a new optimization
target.

### This is the closing state — no further optimization attempts after this entry.

---

## 2026-06-20 — FINAL experiment of tonight's sequence: hand-written load-batching (38→83, still short of the reference's 19). Closing summary below, no further changes after this entry (Latest)

### The experiment
One-off `__dot128_batch4_vu8(a_ptr, b0,b1,b2,b3, c0,c1,c2,c3)` (`ir_gen.py`, explicitly scoped to
this single measurement, not a general pass): loads A's row and all 4 B columns first (5 loads
total — hits the ISA's 4-loads-per-bundle ceiling regardless of order), *then* issues all 4 dot
products against the still-register-resident halves, *then* stores each result straight to its
own final `C[]` address (no intermediate buffer, so no store-then-reload hazard on the results
either). `test_matmul_packed_batched.c` — same 16x16 case, same three corner spot-checks.

**Result: `r1=0x1`, zero pipeline errors, `spill_counter=0`. Matmul-loop bundles: 83.**

### Closing summary of tonight's full experiment sequence
| Step | Change | Matmul-loop bundles | Verdict |
|---|---|---|---|
| Baseline | `__ld128`+`__dot128_vu8`, memory round-trip | 65 | starting point |
| **Round-trip elimination** | Fused `__dot128_direct_vu8`, no round-trip | **38** | **the real win — confirmed correctness + zero regressions** |
| Unroll attempt #1 | Unroll-by-4 on the *broken* (round-trip) primitive | 246 (full count, not loop-only) | wrong direction — diagnosed as 4x the round-trip overhead |
| Unroll attempt #2 | Unroll-by-4 on the *fixed* (fused) primitive | 95 | wrong direction — diagnosed as the 4-loads-per-bundle ISA ceiling (§12.5), confirmed NOT register pressure (`spill_counter=0`, probe cross-checked against `test_spill.c`) |
| Load-batching (this entry) | Load-4-then-dot-4 ordering on the fused primitive | 83 | still short of the reference (19) — beats interleaved unrolling by 12 bundles (confirms ordering has *some* effect) but the 5-loads-per-batch still exceeds the 4-per-bundle ceiling regardless of order |
| Reference | hand-written, loads A once per row (not per batch), reuses across 2 batches of 4 | 19 | not matched |

### What actually worked tonight, plainly
**Eliminating the memory round-trip (`__ld128`→`__dot128_direct_vu8`) was the one real, durable
improvement: 65→38 bundles, with correctness verified at every step and zero regressions across
the full test suite.** Every subsequent attempt to close the remaining gap to the reference's 19
(unrolling, twice; load-batching) made things worse or only marginally better, and each time the
cause was identified precisely rather than guessed: round-trip overhead (first unroll), the
hardware's 4-loads-per-bundle ceiling (second unroll, batching). The reference's actual edge isn't
unrolling or reordering in the abstract — it's loading `A`'s row exactly *once per row* and reusing
it across multiple 4-column batches, rather than once per batch the way every version built
tonight does. That's a specific, identified, *not yet attempted* optimization, not a mystery.

### Stopping here — no further changes tonight
This closes tonight's experiment sequence per explicit instruction. The fused intrinsic
(`__dot128_direct_vu8`) and its verified 38-bundle result remain the standing, correct baseline.
The batch4 intrinsic built for this final measurement is left in place as-is (one-off,
documented, not wired into anything else).

---

## 2026-06-20 — Retried unroll-by-4 on the fused intrinsic: still worse, and confirmed NOT register pressure (Latest)

### Numbers
| Version | Matmul-loop bundles | Register spilling? |
|---|---|---|
| Fused `__dot128_direct_vu8`, real loop (no unroll) | 38 | n/a |
| **Fused, unrolled by 4** (`test_matmul_packed_direct_unrolled.c`) | **95** | **No** |
| Reference | 19 | — |

`r1=0x1`, zero pipeline errors, same three corner spot-checks pass — unrolling didn't break
correctness, just density.

### Spilling ruled out directly, not assumed
Wrote a small probe that runs the actual `IRGenerator`→`CodeGen` pipeline in-process and reads
`cg._spill_counter` after generation (the same counter `_get_spill_slot` increments on every new
spill slot). Result: **0** for the unrolled matmul. Cross-checked the probe is actually sound by
running it against `test_spill.c` (known to spill): **3**, as expected. So this isn't a blind
spot in the check — the 28-register pool genuinely had enough room for all 16 live temps (4 calls
× 4 temps each) plus loop/address bookkeeping, no eviction needed anywhere.

### Why it's worse anyway: a hardware ceiling, not the bundler or register pressure
`$ld` always forces its bundle to a full 8 slots (`Has_Load_Store()` → capacity 8), and ISA doc
§12.5 caps any single bundle at **4 memory-access instructions**. Each `__dot128_direct_vu8` call
issues 2 loads; 4 unrolled calls issue 8 — a hard floor of 2 full bundles for loads alone, before
any dot/address-math instructions. Unrolling quadruples the *static* code (4 calls' worth instead
of 1 call's worth reused via a loop), and the bundler's packing gains (95 vs a naive ~4×38) don't
come close to covering that increase. The non-unrolled loop pays the load cost once per body and
amortizes it across 16 dynamic iterations; unrolling front-loads 4x the static cost for 4x fewer
iterations, and the load-bundle ceiling means that cost can't be packed away.

### Conclusion
**Naive unrolling is the wrong direction here, twice confirmed now** (first with the round-trip
intrinsic: 156→246; now with the fused one: 38→95). The remaining gap to the reference's 19
bundles is not closeable by unrolling our current load primitive at all — the reference's density
comes from batching 4 loads into exactly one bundle (matching the §12.5 ceiling) *and* reusing
each load's result across multiple dot products without reloading, neither of which a generic
unroller would produce automatically from this C source. Any future work here needs to target the
load-batching pattern specifically, not loop unrolling in general.

---

## 2026-06-20 — Re-measured matmul density with the fused intrinsic: 65→38 bundles, no unrolling involved (Latest)

### The numbers, matmul loop body only (data-init loop excluded both times, same methodology as before)
| Version | Matmul-loop bundles | vs reference (19) |
|---|---|---|
| Original split (`__ld128` + `__dot128_vu8`, memory round-trip) | 65 | ~3.4x |
| Hand-unrolled by 4 (same round-trip, just 4x more of it) | n/a (full count went 156→246, density *worse*) | — |
| **Fused (`__dot128_direct_vu8`, no round-trip)** | **38** | **~2x** |

`test_matmul_packed_direct.c`: same algorithm/data as `test_matmul_packed.c`, just
`C[i*16+j] = __dot128_direct_vu8(&A[i*16], &BT[j*16])` instead of separate `__ld128`+copy-out+
`__dot128_vu8`. `r1=0x1`, zero pipeline errors, same three corner spot-checks pass. Full program:
186→129 bundles (was 232→156).

### This confirms yesterday's diagnosis was right, and the unroller hypothesis was the wrong lever
Eliminating the memory round-trip alone — with **zero loop unrolling** — closed most of the gap
the hand-unroll experiment couldn't touch (that experiment made density *worse*, 156→246, because
it was unrolling the wrong thing 4x). The remaining ~2x gap (38 vs 19) is now almost certainly just
the unrolling/4-wide-batching difference described in Step 4 — the reference still fully unrolls
its inner loop and packs 4 independent loads/dots per bundle; our fused version still has one real
loop with one `__dot128_direct_vu8` per iteration, so the bundler still only ever sees one
iteration's worth of independent work at a time. Closing that remaining gap *would* be the loop
unroller's job — but that's exactly the next-session question, not tonight's.

---

## 2026-06-20 — Narrow fix built and verified: __dot128_direct_{type}(a_ptr, b_ptr), zero memory round-trip for the intermediate halves (Latest)

### What was built — reuses existing IR/codegen entirely, no new nodes
New `ir_gen.py` dispatch only: `__dot128_direct_{type}(a_ptr, b_ptr)` allocates four anonymous IR
temps (`self._tmp()`, never bound to a named C variable, never touching `_alloc_local`), emits two
`IRLoadWide` calls (a_lo/a_hi, b_lo/b_hi) feeding straight into one `IRVecDot128` — no `IRStore`
anywhere in this lowering. Deliberately does not touch the named-variable memory model (every
named local still round-trips through its own stack slot on every use; that's confirmed
architectural, not in scope here per yesterday's finding).

### Verified in the generated mcode, not just the result
`test_dot128_direct.c` (same hand-computed case as Stage 2: 16-element `vu8` dot, A=1..16, B=all
1s, expected 136=`0x88`). The `fe_4` block in the generated mcode shows the intermediate halves
flowing purely through ALU register copies (`+r20=r0+r2`, `+r22=r0+r2`, etc.) straight into
`$dot`/`$dot $accumulate` — **zero `$st`/`$ld` for a_lo/a_hi/b_lo/b_hi anywhere**. Only the final
result (bound to named variable `r`) gets a store, exactly as expected and out of scope. Register
trace confirms both intermediate steps exactly: `0x24` (36, lo-half sum) → `0x88` (136, after
accumulate). `r1=0x1`, zero pipeline errors. Full regression, 22 tests: zero regressions.

### Status: fix verified. Next: re-measure bundle density on the matmul using this intrinsic.

---

## 2026-06-19 — Hand-unroll experiment: bundle count went UP (156→246), not down — real finding, not a dead end (Latest)

### What was tested
Before committing to building a general loop-unroller, validated the hypothesis cheaply by
hand-unrolling `test_matmul_packed.c`'s inner `j`-loop by 4 (`test_matmul_packed_unrolled.c`,
separate `buf0`..`buf3` so the 4 loads don't share a false memory dependency through one buffer).
Correctness holds: `r1=0x1`, zero pipeline errors, same three corner spot-checks pass.

### Result: bundle count went UP, not down
| Version | Bundles |
|---|---|
| Original (real loop, no unroll) | 156 |
| Hand-unrolled by 4 | **246** |
| 16x16 reference | 19 |

### Why — confirmed in the generated mcode, not guessed
`__ld128`/`__ld256` (built in Stage 1) always round-trip the loaded pair through memory: load into
a register pair → copy to ordinary registers → **store immediately out** to the destination buffer
(`dst[0]`/`dst[8]`). Confirmed directly: `$ld ($u128) $r4 [...]` → `+r21=r0+r4; +r20=r0+r5` →
`$st [$r13+0] $r21; $st [$r13+8] $r20`. When the caller then reads `buf[0]`/`buf[1]` back (to feed
`__dot128_vu8`), that's a **second** load of the same data. `bundler.py` correctly refuses to pack
a store with its own immediate reload (the exact memory-hazard fix from 2026-06-17) — so unrolling
just multiplies this store+reload overhead by 4 instead of buying any parallelism. The hypothesis
("does the existing bundler close the gap once given unrolled, independent work") is **answered:
no — not because the bundler is weak, but because `__ld128`'s memory-round-trip design is the
wrong shape for chaining straight into a vector op.** It was the right design for Stage 1's actual
goal (verify the load mechanism by observing it through memory); it's the wrong tool here.

### What this means for next steps (not started tonight, per instruction)
The real fix isn't a loop unroller — it's a **different load primitive** that keeps the pair/quad
live in registers for direct use by `__dot128_vu8`, with no forced memory round-trip (closer to
what the reference does: `$ld` straight into `$rN`, fed straight into `$dot`). That's a smaller,
better-understood change than a general unroller, but it's still new work. **Not started tonight**
— stopping here as instructed. Next session: build that direct-use load primitive first, *then*
revisit whether unrolling is still worth it on top of it.

---

## 2026-06-19 — Step 4: bundle-count gap measured and explained, not chased (Latest)

### Raw numbers
Our `test_matmul_packed.mcode`: 232 bundles before VLIW packing, **156 after**. Reference
`16x16.mcode`: **19 bundles** total. Comparing these two numbers directly is unfair, though — the
reference's data is pre-loaded via `data.map` before the program even starts (no runtime
initialization at all), while our test computes `A`/`BT` at runtime via a nested loop with two
`% 256` modulo operations (each needing a div/mul/sub synthesis sequence, since there's no native
MOD instruction).

### Fair comparison: matmul loop body only
Broke our 156 bundles down by label range: **47** bundles are the data-init double-loop (not
present in the reference at all), **65** are the actual matmul double-loop, **44** are
prologue/epilogue/spot-checks. So the real comparison is **65 (ours) vs 19 (reference)** for
logically equivalent work — still a ~3.4x gap, and here's precisely why:

1. **The reference fully unrolls its inner 16-column loop.** Its outer loop (16 row iterations)
   contains zero inner branches — all 16 output columns per row are computed via straight-line
   code. Our C source has a real `for(j=0;j<16;j++)` inner loop, so every one of our 65 bundles
   includes the recurring cost of the inner loop's branch + counter increment + bounds check,
   paid once per source-level loop body rather than amortized away by unrolling.
2. **The reference batches 4 independent `$ld ($u128)` calls into a single bundle** (`load1`/
   `load2`/`load3`/`load4`, each 4 parallel loads in one VLIW bundle) — maximum use of per-bundle
   parallelism. Our C source issues exactly one `__ld128` per loop iteration, so there is no
   *static* 4-way-parallel code for `bundler.py` to discover and pack together — the bundler can
   only pack what the source actually expresses; it can't see "4 loop iterations from now" and
   pull work forward across iterations.
3. **Same story for `$dot`/`$dot $accumulate`**: the reference's `dot1`-`dot8` blocks batch 4 dot
   operations per bundle; ours does one dot-pair per inner-loop iteration.

### Not chasing this further, per instruction
Closing this gap would mean either hand-unrolling the inner loop in the C source (tedious, and
arguably defeats the point of writing it as a loop) or building a real loop-unrolling optimization
pass in the compiler (a substantial new feature, not a cheap fix). Neither qualifies as "obviously
cheap," so — as instructed — not doing it now. The gap is fully explained and the cause is
structural (unrolling + batched VLIW packing in the reference vs. a real loop in ours), not a
compiler bug.

### Status: all four numbered steps done and committed.

---

## 2026-06-19 — Step 3 done: 16x16 vu8 matmul with packed arrays, PASSES (Latest)

`new_isa_tests/test_matmul_packed.c` — identical algorithm to `test_matmul_u128.c` (the earlier,
blocked attempt), but `A`/`BT` declared as `vu8_t` instead of `unsigned char`. Same `__ld128`/
`__dot128_vu8` primitives, unchanged. Global footprint dropped from 7184 bytes (padded) to 2576
bytes (packed) — confirms the Step 2 fix is actually taking effect, not just compiling.

**Result: `r1=0x1`, zero pipeline errors.** Three spot checks across the result matrix (corners):
`C[0]`=`0x5588` (row0,col0), `C[15]`=`0x4d80` (row0,col15), `C[255]`=`0x75580` (row15,col15) — all
exact matches against `16x16_loop/16x16.result` (the reference's own computed expected values).
This is the first time the full u128-load → dot-split pipeline has worked end-to-end on real
matrix data, closing out the Stage 3 finding from earlier today.

Note: did not re-add the full 256-value checksum check from `test_matmul_u128.c` — that earlier
hit a separate, unrelated bug (comparing against a large multi-field-literal constant in an `if`).
Not chased here; three corner spot-checks already give strong confidence the computation is
correct, and chasing the checksum-comparison bug is out of scope for today's matmul work.

### Status: Steps 1-3 done and committed. Step 4 (bundle-count comparison vs the 16x16 reference) next.

---

## 2026-06-19 — Steps 1+2 done: confirmed no half-implementation existed, then added opt-in packed-array stride. Zero regressions (Latest)

### Step 1 — checked for existing partial work first
Grepped `ir_gen.py`/`codegen.py`/`ir.py`/`bundler.py` for "packed"/"natural stride"/anything
narrow-type-stride related: nothing exists. `dmem_stride = max(elem_bytes, 8)` is hardcoded with
no opt-out path anywhere, in both `_alloc_global` and `_alloc_local`. Confirmed before building
anything new, as instructed.

### Step 2 — opt-in natural stride for char/short/int arrays, long long/pointer/struct untouched
`__attribute__((packed))` is not parseable by this pycparser setup (confirmed by testing it
directly — hard parse error). Used the same mechanism the compiler already relies on for
`int64_t`/etc. (`_FAKE_TYPEDEFS` in `compiler.py`): six new opt-in marker typedefs --
`vu8_t`/`vi8_t`/`vu16_t`/`vi16_t`/`vu32_t`/`vi32_t` -- aliasing the obvious base types. An array
declared with one of these specific type names gets `dmem_stride = elem_bytes` (no padding);
**every other array, including plain `char`/`short`/`int`, is completely unaffected** (default
unchanged: `max(elem_bytes, 8)`). `ir_gen.py`: new `_is_packed_array_decl()` checks the element's
literal `IdentifierType` name against the marker set; naturally scoped to plain 1D arrays only —
2D arrays (own separate `col_stride` path) and struct fields (forced `esz=8`) never match this
check, so they're unaffected without needing extra exclusion logic. Wired through `visit_Decl` →
`_alloc_global`/`_alloc_local`'s new `packed=` parameter.

Verified directly: `vu8_t A[16]` → `GLOBAL A @0x400 (16B stride=1)`, indexing offset `+1` for
`A[1]`. `unsigned char B[16]` (same probe file) → `GLOBAL B @0x410 (128B stride=8)`, completely
unchanged. **Full regression, all 20 pre-existing tests**: zero regressions, every value exactly
matches prior runs. `test_ld128_then_dot` (Stage 3's reproduction evidence, uses plain
`unsigned char` deliberately) still fails exactly as documented — expected, since it never opts in.

### Status: Steps 1-2 done and committed. Step 3 (16x16 matmul with packed arrays) next.

---

## 2026-06-19 — Stage 3 (full matmul) blocked by a real architectural finding, not a bug to fix in passing: byte arrays are NOT tightly packed in this compiler, so u128/u256 loads can't see them as packed byte vectors (Latest)

### What was attempted
Composed the already-verified Stage 1 (`__ld128`) and Stage 2 (`__dot128_vu8`) pieces into an
actual 16x16 `vu8` matmul (`new_isa_tests/test_matmul_u128.c`), matching the hand-written 16x16
reference's data layout (B pre-transposed) and verified against its own checksum (67517440) and
spot-checked output values. **Failed**: `C[0]` came out wrong.

### Root cause — found, not guessed, and it's bigger than this stage
Isolated with a minimal repro (`new_isa_tests/test_ld128_then_dot.c`: load two 16-byte `unsigned
char` arrays via `__ld128`, dot them with `__dot128_vu8`, expect 136). Checked the actual IR:
```
GLOBAL A @0x400 (128B stride=8)
...
_t9 = _t8 * 8        // index k -> byte offset k*8, not k*1
```
**Every array element in this compiler — regardless of its C type's actual size — gets its own
8-byte-aligned DMEM slot** (the established convention, documented elsewhere as working around an
`$ld ($i32)` hardware quirk). A 16-element `unsigned char A[16]` therefore occupies 128 bytes in
memory, not 16: each real byte is followed by 7 bytes of padding. A `$ld ($u128)` reads 16
*physically consecutive bytes* — which under this layout is `A[0]`'s one real byte, 7 padding
zeros, and 1 byte of `A[1]`'s slot. It never sees 16 packed logical elements. Confirmed in the
runtime trace: the loaded "vector" came back as `0x0100000000000000` — exactly one real byte
(value 1) followed by zeros, matching this explanation precisely.

**Why Stage 1 didn't catch this**: it used `long long src[2]` — for an 8-byte C type, the 8-byte
stride *is* the element size, so there's no padding to expose. Stage 2 was fed literal constants
directly, no memory layout involved at all. Byte vectors are specifically what u128/u256 loads
exist for, so this stage was the first one that could possibly hit it.

### This is an architectural decision, not a quick fix — stopping per instruction
This isn't "implement the obvious fix" — it's "decide how byte arrays meant for vector use should
be laid out," which has real tradeoffs (a separate tightly-packed array kind? a `__packed`
attribute? change the global stride rule only for `vu8`-tagged arrays?) that aren't mine to choose
unilaterally. Flagging precisely and stopping here rather than guessing at a fix, per the explicit
instruction for exactly this kind of finding.

### What's verified and what's not (precise state, nothing half-applied)
- **Verified, hardware-confirmed, solid**: `$ld ($u128)`/`$ld ($u256)` register-pair/quad load
  mechanics (Stage 1), alignment-correct `borrow_pair`/`borrow_quad` (with a targeted unit test),
  the `$dot`+`$dot $accumulate` split lowering for 128-bit-wide dot products fed from registers
  directly (Stage 2, `test_dot128_split.c`, exact match).
- **NOT verified, blocked**: composing a wide load's output into a vector op when the source data
  is a C byte array — blocked by the stride-8-per-element layout above, not by anything in the
  Stage 1/2 code itself.
- **Test files left in place, not reverted**: `test_matmul_u128.c` and `test_ld128_then_dot.c` are
  committed as-is (the latter with its one failing check) — they're the reproduction evidence for
  this finding, not a regression to fix later. `test_matmul_u128.c`'s checksum check is commented
  out (the spot-checks below it already fail first; left the comment explaining why rather than
  deleting it).

---

## 2026-06-19 — Dot-split stage DONE: u128-wide $dot auto-lowering implemented and hardware-verified (Latest)

### What was implemented (no re-derivation — emitted the proven 16x16 reference pattern exactly)
New `IRVecDot128(dest, a_lo, a_hi, b_lo, b_hi, type_str)` + `_gen_IRVecDot128` in `codegen.py`,
emitting exactly:
```
$dot              dest (type) lo_a lo_b
$dot $accumulate  dest (type) hi_a hi_b
```
New intrinsic `__dot128_{type}(a_lo, a_hi, b_lo, b_hi)` in `ir_gen.py`. No bundler change needed —
`$dot`/`$dot $accumulate` hazard tracking already existed and already matches this exact emitted
form. `$dot`'s own operands have no register-pair alignment requirement (unlike `$ld ($u128)`/
`$pack`), so `a_lo`/`a_hi`/`b_lo`/`b_hi` are ordinary, independently-allocated registers here —
no borrow_pair/borrow_quad involved in this stage.

### Verification — `new_isa_tests/test_dot128_split.c`
16-element `vu8` dot: A = 1..16, B = all 1s. Hand-computed: sum(1..16)×1 = 136 = `0x88`.
Emitted mcode confirmed exact target pattern: `$dot $r13 ($vu8) $r6 $r10` then
`$dot $accumulate $r13 ($vu8) $r8 $r12`. Register trace: `$r13` goes `0x24` (36, lo-half sum,
matches 1+2+...+8 by hand) → `0x88` (136, after accumulate, matches 36+100 by hand) — exact match
at both steps, not just the final aggregate. `r1=0x1`, zero pipeline errors. Quick regression on
overlapping tests (`test_dot`, `test_pack`, `test_u128_load`, `test_u256_load`, `test_matmul`):
unchanged.

### Status: dot-split stage verified and committed. Ready for Stage 3 (full matmul) on confirmation.
All three staged pieces (u128/u256 load mechanics, alignment-correct borrow_pair/borrow_quad, and
now the dot-split lowering) are independently hardware-verified. Not yet wired into an actual
matrix multiply — that's the next stage, not started, awaiting go-ahead per the staged plan.

---

## 2026-06-19 — u128/u256 redesigned to transient borrow (mirroring $pack exactly); found and fixed a real latent alignment bug in borrow_pair() along the way; u256 now hardware-verified too (Latest)

### borrow_pair() had NO alignment check at all -- not just "needs generalizing"
Re-read the actual code before touching anything (per instruction): `borrow_pair()` only checked
`n2 == n1+1` (consecutive). It never checked that the start index is even. ISA doc 12.2 confirmed
verbatim: "When a pair of registers is used... the register specified... must have an even
index... When a quad... an index which is a multiple of 4." So this wasn't "extend an existing
even-check to also handle multiple-of-4" -- it was **add the missing even-check to `borrow_pair`
itself** (a latent bug that happened not to bite `$pack` yet, presumably because the free pool's
first consecutive run has so far always happened to start even) **and** add a correctly,
independently-parameterized multiple-of-4 check for the new `borrow_quad`. Implemented both via
one shared `_find_aligned_group(count, alignment)` scan so the two checks can't drift apart by
accident.

### Stage 1 reworked: permanent reg_pair() reverted, transient borrow like $pack
Removed `RegAlloc.reg_pair()`/`_alloc_reg_pair()` entirely. `IRLoad128` generalized to
`IRLoadWide(dests, base, offset)` (length 2 or 4). `_gen_IRLoadWide` now mirrors `_gen_IRPack`
exactly: borrow an aligned pair/quad just for the one `$ld` instruction, copy each register out to
an ordinary unconstrained register immediately, release the borrowed group right away. This means
loaded values don't tie up alignment-sensitive registers for their whole lifetime -- important
once Stage 2/3 need many loads live at once.

### Unit test specifically targeting the trap flagged before implementing
Direct `RegAlloc` test (not C-level): pool = `{$r2,$r3,$r4,$r5,$r8,$r9,$r10,$r11}` -- a tempting
*contiguous* run at 2..5 (invalid, start=2 isn't a multiple of 4) alongside a genuinely valid one
at 8..11. `borrow_quad()` returns `($r8,$r9,$r10,$r11)`, never touches `$r2`. Second case: pool =
`{$r2,$r3,$r4,$r5}` only (no valid run anywhere) -- `has_free_quad()` is `False` and `borrow_quad()`
raises rather than silently accepting the misaligned run. Third (bonus) case confirms
`borrow_pair()` itself skips an odd-start pair (`$r3,$r4`) in favor of an even one (`$r6,$r7`) when
both are present. All three pass.

### Hardware verification, both widths
`test_u128_load.c` (re-run after the rework): `$ld ($u128) $r6 [...]` (r6 is even) →
`r1=0x1`, register trace `$r6=0x1111...111`, `$r7=0x2222...222`, exact match.
`test_u256_load.c` (new): `$ld ($u256) $r8 [...]` (r8 is a multiple of 4) → `r1=0x1`, register trace
`$r8=0x1111...`, `$r9=0x2222...`, `$r10=0x3333...`, `$r11=0x4444...`, exact match across all four
quarters. Full 20-test regression (everything from the suite plus both new tests): zero
regressions, including `test_pack` (also uses `borrow_pair()`) — unaffected by the new alignment
check, consistent with it having always gotten lucky rather than ever needing an odd-start pair.

### Status
Both u128 and u256 load mechanics are now hardware-verified with correct ISA-mandated register
alignment, proven by both a hardware test and a targeted allocator unit test. Stopping here per
the staged plan — not touching the dot-split stage without confirmation.

---

## 2026-06-19 — u128 register-pair load: Stage 1 (load mechanics only) PASSES. Stopping here per the staged plan, awaiting confirmation before Stage 2 (Latest)

### What was built
- `ir.py`: new `IRLoad128(dest_lo, dest_hi, base, offset)` node.
- `codegen.py`: `RegAlloc.reg_pair()` (permanent consecutive-pair allocation, mirrors the
  existing transient `borrow_pair()` used by `$pack`) + `_alloc_reg_pair()` (spill-aware
  wrapper) + `_gen_IRLoad128` (emits `$ld ($u128) {lo} [{base}+{offset}]`).
- `bundler.py`: generalized the `$ld` hazard regex from `($iN)`-only to `($[iu]N)`, and made it
  compute the write-set as a register *range* (`{rd..rd+nbits/64-1}`) instead of always `{rd}` --
  needed because a single `$u128` load writes two registers, not one.
- `ir_gen.py`: new intrinsic `__ld128(dst, src)` — one `$ld ($u128)` into a register pair, then
  two plain 64-bit stores of the halves to `dst[0]`/`dst[8]`.

### A real, separate, pre-existing bug found and fixed along the way
Bare global array names passed to a call were never decaying to their address — only *local*
arrays did. Root cause: `_array_elem` (the dict the call-arg decay check consults) gets reset to
`{}` at the top of `visit_FuncDef` for per-function local scoping, which silently wiped out any
global array registered before `main` was visited. (`_array_row_stride`, used for 2D arrays,
already avoids this — it's never reset, which is exactly why global 2D arrays never had this bug.)
**Fix**: new `self._global_array_elem` dict, populated by `_alloc_global`, never reset, consulted
alongside `_array_elem`/`_array_row_stride` in the call-arg decay check. This was diagnosed in a
few steps (read the IR dump, found values instead of addresses, found the exact reset line) — a
contained, well-understood Python fix, not the kind of source-archaeology that warranted stopping
to ask first. Verified zero regressions on every array-using test (`test_array`, `test_2d`,
`test_struct`, `test_scalar_full`, `test_matmul`, `test_spill`).

### Stage 1 verification — `new_isa_tests/test_u128_load.c`
```c
src[0] = 0x1111111111111111LL; src[1] = 0x2222222222222222LL;
__ld128(dst, src);
// dst[0] must equal src[0], dst[1] must equal src[1]
```
Register trace confirms exactly: `$ld ($u128) $r1 [...]` set `$r1=0x1111111111111111`,
`$r2=0x2222222222222222` — lower register gets the lower address, matching every other
consecutive-register convention already in this compiler ($pack, $cast). r1=`0x1` (both checks
pass). Zero pipeline errors.

### Stopping here per the staged plan
Per explicit instruction: report back after each stage, don't continue without confirmation.
**Stage 1 (load mechanics) is hardware-verified.** Stage 2 (auto-split `$dot`/`$v` across the pair
into plain + `$accumulate`, matching the 16x16 reference) not started — awaiting go-ahead.

---

## 2026-06-19 — $vreduce FIXED: missing sub-opcode token, same bug family as $cmov's missing '?'. All vector instructions now confirmed working (non-4-bit integer types) (Latest)

### Root cause (isolated the same way as $cmov: minimal repro, read the actual grammar)
`$vreduce $rd ($type) $rs` (what we emitted, and what the ISA doc's own example shows) fails to
parse: `unexpected token: $r6` right after `$vreduce`. The real grammar
(`mcode_vreduce_instruction` in `isa.g`) is:
```
opcode = mcode_vreduce_op_code        // consumes the $vreduce mnemonic itself
sub_opcode = mcode_vreduce_sub_op_code  // REQUIRED: one of + * | & ^ ~^ $max $min
rd = mcode_reg_specifier
mcode_type_specifier
rs1 = mcode_reg_specifier
```
The sub-opcode selects *what kind* of reduction (`+`=sum, `*`=product, `\|`/`&`/`^`/`~^`=bitwise,
`$max`/`$min`). The ISA doc's example (§5.5) omits it entirely — third time this exact failure
mode has shown up (`$cmov`'s missing `?`, now this), all because the doc's own examples are
incomplete relative to the actual grammar. Our `__vreduce_*` intrinsics only ever do sum-reduce,
so the fix is to always emit the `+` sub-opcode.

### Fix
`codegen.py`'s `_gen_IRVecReduce`: `$vreduce {dest} ({type}) {src}` → `$vreduce + {dest} ({type})
{src}`. `bundler.py`'s `$vreduce` hazard regex updated to skip the new sub-opcode token.

### Verification
`test_vec_reduce2.c` (6 checks: vi8/vi16/vi32/vu8/vu16/vu32 sum-reduce): all pass, r1=`0x1`.
Original `test_vreduce.c`: r1=`0x4c` (76) — exact match with the historical expected value, now
running cleanly instead of crashing the aligner. Full 19-test regression: zero regressions.

### Bottom line: every non-4-bit-integer vector instruction is now confirmed working
`$v` (add/sub/mul): all of `vi8`/`vi16`/`vi32`/`vu8`/`vu16`/`vu32`. `$dot`/`$dot $accumulate`:
`vi8`/`vi16`/`vu8`/`vu16` (the only widths the ISA defines dot for). `$vreduce`: all six widths.
`vi4`/`vu4` remain known-broken/skipped per explicit direction (not used frequently). Float
vectors (`vf*`) deferred entirely per explicit direction. **Vectors are now solid enough to build
matrix multiplication on top of** — next step per the user's plan.

---

## 2026-06-19 — $dot/$dot $accumulate fully hand-verified for matmul readiness (8/8 exact); $vreduce hits a second, separate, not-yet-root-caused bundler/aligner crash (Latest)

Per explicit direction: skip `vi4`/`vu4` (not used frequently), prioritize `$dot`/`$dot $accumulate`
(most important for matrix multiplication), float vectors (`vf*`) deferred entirely, `$nop` not
urgent. Goal: get enough of `$v`/`$dot`/`$vreduce` verified to trust vector-based matmul.

### $dot / $dot $accumulate — ALL 8 checks pass exactly (`new_isa_tests/test_vec_dot.c`)
Hand-computed sum-of-products for `vi8`/`vi16`/`vu8`/`vu16`, both plain and `$accumulate` forms
(`vi32`/`vu32` correctly excluded — ISA doc §5.4: "dot is defined only for elements <=16 bits").
Final r1 = `0x1` (all pass). **This is the piece the user said matters most for matmul, and it's
solid.**

### $v add for the remaining untested widths — both pass exactly (`test_vec_add16_32.c`)
`vu16`: `0x00060008000a000c` (element-wise (1+5),(2+6),(3+7),(4+8)) — exact match.
`vu32`: `0x000000080000000a` (element-wise (3+5),(4+6)) — exact match.
Combined with earlier confirmation of `vi8`/`vi16`/`vi32`/`vu8`, **`$v` add is now verified across
every non-4-bit integer width.**

### $vreduce — still broken, but now isolated to a SECOND, different bug than the label-merge fix
`test_vec_reduce2.c` (6 plain `__vreduce_*` checks, no `$dot`/`$v` mixed in) crashes
`mcode_align` with the same `Calculate_Pad_For_Alignment` assertion — but **confirmed via the same
bisection technique used for the label bug that there is no consecutive-label pattern here**, so
this is NOT the bug fixed yesterday. Bisected down to the crash appearing right around the
*first* `$vreduce` call's bundle (`$vreduce $r6 ($vi8) $r5` + an address-compute ALU op, followed
by the store of its result) — consistent with `test_vreduce` (the pre-existing official test)
failing the exact same way. **Not root-caused this session** — ran out of quota for the
bisect-deeper-into-bundler.py work this would need (same rigor as yesterday's label-merge fix,
just not finished). This is the one piece standing between "vectors are matmul-ready" and "fully
verified" — `$vreduce` itself (sum-reduction) isn't needed for a dot-product-based matmul, only
`$dot`/`$dot $accumulate` are, so this doesn't block vector matmul, just full ISA coverage.

### Bottom line for vector-based matrix multiplication
The two operations that actually matter for matmul — `$dot` and `$dot $accumulate` — are now
fully hand-verified across every non-4-bit integer width. `$v` add is fully verified too (useful
for elementwise vector ops alongside matmul). `$vreduce` remains broken but isn't on the matmul
critical path. **Next planned step** (not started this session, flagged for next time): `u128`/
`u256` wide vector load/store — needed for real 32x32 vector matmul, currently zero compiler
support (confirmed via grep, see earlier entries).

---

## 2026-06-19 — vi4 garbage value confirmed reproducible (4/4 runs); audited all other switch(nbits) blocks — CastToU64 appears to be an isolated bug, not a pattern (Latest)

### Reproducibility check
Re-ran the `test_vi4_check` repro 4 more times (fresh `run.sh` invocation each time, full
align→assemble→run). **All 4 runs: `Set_Register(7, 0x0)` — identical every time.** Not
coincidental; consistent with reading a deterministic (if uninitialized) stack slot reached via
the exact same call path every run, not random garbage that happens to vary.

### Audit of the other switch(nbits) blocks flagged yesterday
Checked `McodeNumeric.cpp:493` and **all seven** `switch(nbits)` blocks in `McodeFpuUtils.cpp`
(corrected count — said "five" yesterday without actually counting; there are 7) for the same
missing-`case 4`-with-uninitialized-fallthrough pattern as `CastToU64`. None of them have it:

| Location | Function | Has `case 4`? | Fallback if no match |
|---|---|---|---|
| `McodeNumeric.cpp:493` | `to_ufp64` | yes | `default:` reinterprets bits directly (defined behavior) |
| `McodeFpuUtils.cpp:318` | `fp_mul` | yes | `result` pre-initialized to 0; `default:` no-op |
| `McodeFpuUtils.cpp:344` | `fp_add` | yes | same |
| `McodeFpuUtils.cpp:371` | `fp_sub` | yes | same |
| `McodeFpuUtils.cpp:398` | `fp_div` | **no** (only 32/64) | `default: assert(0)` — **crashes loudly**, doesn't silently return garbage |
| `McodeFpuUtils.cpp:417` | `fp_sqrt` | **no** (only 32/64) | `default: assert(0)` — same, loud crash |
| `McodeFpuUtils.cpp:539` | `double_to_fp` | yes | `default: assert(0)` |
| `McodeFpuUtils.cpp:553` | `fp_to_double` | yes | `default: assert(0)` |

Also checked the three related cast helpers right next to these (`cast_int_to_float`,
`cast_float_to_int`, `cast_float_to_float`, all in `McodeFpuUtils.cpp`) since they weren't in the
original flagged list but are directly adjacent and relevant — all three have `case 4:` and
`default: assert(0)`.

**Conclusion: `CastToU64`'s bug looks isolated, not systemic.** Every other switch(nbits) either
explicitly handles 4 bits, or fails loudly (`assert(0)`) instead of silently returning an
uninitialized value. `fp_div`/`fp_sqrt` simply don't support 4-bit floats by design (consistent
with there being no ISA-documented 4-bit float div/sqrt) and crash rather than corrupt — that's a
deliberate restriction, not the same bug class as `CastToU64`. Only `CastToU64` declares `result`
without an initializer and has no `default:` label at all, which is exactly why it alone returns
silent garbage instead of crashing or working.

---

## 2026-06-19 — Hand-verified vi4/vu8: vu8 correct, vi4 confirmed BROKEN (engine bug, exact line found) (Latest)

Yesterday's "vi4/vu8 don't crash" claim was correctly challenged as insufficient. Wrote two
minimal, hand-computable tests (`new_isa_tests/test_vi4_check.c`, `test_vu8_check.c`) and compared
the exact hardware register value against a hand-computed expected value — not just "did it run."

| Type | a | b | Expected (hand-computed) | Hardware actual | Result |
|---|---|---|---|---|---|
| `vu8` | `0x0102030405060708` | `0x1010101010101010` | `0x1112131415161718` (each byte +0x10, no overflow) | `0x1112131415161718` | **exact match** |
| `vi4` | `0x1111111111111111` | `0x2222222222222222` | `0x3333333333333333` (each nibble 1+2=3, no overflow) | `0x0` | **WRONG** |

### Root cause for vi4 — found, not guessed (engine bug)
Traced `$v +` for `(vi4)` through `___execute_valu_operation___` → `__valu_operation__` →
`__alu_operation__` → `CastToU64()` (all in `McodeOperations.cpp`). `CastToU64(int signed_flag,
uint32_t nbits, uint64_t ival)` (line 50) has a `switch(nbits)` with cases for **8, 16, 32, 64
only** — no `case 4`. For any 4-bit-wide result (every `vi4`/`vu4`/`vf4` element op), the switch
falls through with no case matching, `result` is declared but **never assigned**, and the function
returns whatever garbage was already on the stack — which happened to be `0` in this run, hence
every element of the vi4 add silently became `0`, concatenating to a final `0x0`. `vu8`/`vi8`/etc.
all hit the `case 8` (or 16/32/64) branch correctly, which is why every other tested vector width
is fine and only the 4-bit path is broken.

Type parsing itself is correct (confirmed in `isa.g`'s `mcode_type_specifier` rule: `vi4_t` →
`nbits=4, vector_flag=1`) — the bug is purely in this one switch statement's missing case, not in
how `vi4` is recognized or decoded.

**Not fixed** (engine-side, same protocol as today's other engine findings — needs the professor).
The fix is mechanical: add a `case 4:` doing a manual 4-bit sign-extend/mask (no native C++ `int4_t`
to reuse the existing `___signed_cast___`/`___unsigned_cast___` macros with). **Flagging, not
guessing further**: there are similar `switch(nbits)` statements in `McodeNumeric.cpp:493` (used by
`$cast`) and five in `McodeFpuUtils.cpp` (float ops) — not checked for the same missing-case-4 gap;
don't assume `i4`/`u4`/`f4` scalar casts or float-4 ops are safe until checked the same way.

### Bottom line on vectors
`vi8`/`vi16`/`vi32` (signed) and `vu8` (unsigned, newly hand-verified) are confirmed correct.
**`vi4` is confirmed broken with a precise, citable root cause** — do not use it, and don't claim
4-bit vector types work in general until `CastToU64` is fixed and re-verified. `vu4`/`vf4` are
untested but share the exact same code path, so should be assumed broken too until checked.

---

## 2026-06-19 — Three real compiler-side bugs found and fixed: $cmov operand grammar, and a bundler bug that was silently causing test_2d/test_fsqrt/test_matmul's aligner crashes (Latest)

### 1. `$cmov` fixed — `codegen.py`'s `_gen_IRCmov`
Professor's freshly-pulled engine (`10.107.90.220:/students/mohith/AjitHpc_new/...`) confirmed
`$cmov` requires a `?` token right after the mnemonic, matching `engine_new`'s grammar (not the
historical `engine_isp` grammar our codegen targeted). Traced the actual register-role wiring
empirically (`McodeInstructions.cpp`'s `Get_Operands`/`Execute`) rather than trusting grammar
comments, since theoretical grammar-position reasoning gave contradictory results twice. **Fix**:
add `?`; the check/src_true register ORDER in the text is unchanged (`check` first, `src_true`
last) — only the `?` was missing. `bundler.py`'s `$cmov` hazard regex updated to match. Verified:
`test_cmov` now returns `0x258` (600), exact match, zero regressions on anything else.

### 2. Real bundler bug found: consecutive labels on one bundle aren't valid syntax
While building a matrix-multiply test (`new_isa_tests/test_matmul.c`, 3x3, flattened 1D arrays to
avoid the already-known 2D-array aligner issue), hit the *same* `Calculate_Pad_For_Alignment`
assertion crash that blocks `test_2d`/`test_fsqrt`/`test_vreduce`. Root-caused properly this time
(bisected the mcode by bundle boundaries): `bundler.py`'s `_emit_bundles` prints **every** label
attached to a bundle on its own line before `||` — but the assembler grammar only allows ONE label
directly before a bundle (confirmed: `expecting PARALLEL, found <label>` when two appear in a
row). This happens whenever, e.g., an inner loop's exit label lands on the exact same bundle as an
outer loop's increment label with no real instruction between them — common in nested loops. The
resulting parse error leaves a zero-instruction bundle, and `Calculate_Capacity()` in
`McodeBundle.cpp` silently returns 0 for it (logs an error but doesn't abort) instead of crashing
there — the crash only surfaces later in `Calculate_Pad_For_Alignment`'s division-by-zero guard,
which is why this looked unrelated for so long.

**Fix** (`bundler.py`, new `_merge_duplicate_labels`, wired into `bundle_mcode` between
`_pack_bundles` and `_emit_bundles`): when a bundle ends up with multiple labels, collapse to one
canonical label (the first) and rewrite every `$goto`/`$call` reference to the dropped labels so
they point at the canonical one instead. (`$call $rN`, register-indirect, is never matched — the
regex requires a bare identifier with no leading `$`.)

**This one fix resolved four programs at once**: `test_matmul` now returns `0x26d` (621, exact —
hand-verified 3x3 matrix multiply), and as a bonus, `test_2d` and `test_fsqrt` — both blocked by
this exact crash for weeks — now align and run cleanly (`test_2d`=`0x0`, matching expected).
`test_vreduce` still crashes, but confirmed via the same bisection technique that it has **no**
consecutive-label pattern — a different, separate, not-yet-diagnosed cause.

### 3. Vector type coverage: `vi4`/`vu8` confirmed not to crash (not bit-level verified)
Wrote `new_isa_tests/test_vec_extra.c` exercising `__vadd_vi4`/`__vadd_vu8` (previously completely
untested — the intrinsic parser does zero suffix validation). Compiles and runs cleanly, zero
pipeline errors. **Not yet hand-verified bit-exact** — that needs dedicated test design, not a
quick check; flagging honestly rather than claiming full confidence here.

### Also fixed: `compiler.py`'s `write_run_script` template
Was still emitting the stale March-6 `engine_isp` `BIN_DIR` for any newly-compiled test (flagged
yesterday, actually bit us today recompiling `test_cmov`). Now points at `engine_new`.

### Full regression after all of the above
`test_alu`=0xd, `test_array`=0x96, `test_struct`=0x0, `test_branch`=0x1, `test_ldst`=0x3e8,
`test_pointer`=0xf, `test_subword`=0x1, `test_dot`=0x5a, `test_spill`=0x1d1,
`test_scalar_full`=0xc, `test_vadd`=0x4, `test_slice`=0xb7, `test_cast`=0x78ab9bcd,
`test_pack`=0xdead, `test_cmov`=0x258 — all exactly correct, zero regressions from today's changes.

---

## 2026-06-18 — IMEM size bug CONFIRMED against the official ISA doc and fixed for real this time: simulator was [2048] (8KB), spec says 16KB; corrected to [4096]. test_struct/test_spill/test_scalar_full ALL pass (Latest)

### What changed since the last entry
User checked `AparaReference.pdf`, p.6, §1, Figure 1.1 directly: **"The instruction memory provides
16KB of instruction space to each accelerator. Each instruction is 4-bytes."** 16KB ÷ 4 bytes/instr
= **4096 words**. `McodeClasses.hpp` had `__instruction_memory[2048]` / `Instr_Mem_Size_In_Words()
= 2*1024` — **half the documented size**, mislabeled with a stale `// 16KB` comment that never
matched the actual 8KB the array provided. This is not a "maybe the simulator default is smaller
than hardware" situation (the open question from the previous entry) — it's a confirmed, citable
discrepancy between the simulator and the spec. (Data memory was already correct: `__data_memory[8
* 1024]` qwords = 64KB, matching the doc's "data memory provides 64KB" exactly — only instruction
memory was wrong.)

### Fix applied (no longer "verification only")
`McodeClasses.hpp:139,143`: `__instruction_memory[2048]` → `[4096]`,
`Instr_Mem_Size_In_Words()` → `4*1024`. Rebuilt with `scons`.

| Test | Before (8KB IMEM, the bug) | After (16KB IMEM, matches spec) | Expected |
|---|---|---|---|
| test_scalar_full | 0x7ff0 | **0xc** | 0xc (12) |
| test_spill | 0x19 | **0x1d1** | 0x1d1 (465) |

Both real program sizes (2688 / 2496 words) now fit comfortably under the corrected 4096-word
budget — zero "beyond the i-mem size" errors. Full 19-test regression re-run: all 14
runnable/passing tests produce exactly their expected values
(`test_alu`=0xd, `test_array`=0x96, `test_struct`=0x0, `test_branch`=0x1, `test_ldst`=0x3e8,
`test_pointer`=0xf, `test_subword`=0x1, `test_dot`=0x5a, `test_spill`=0x1d1,
`test_scalar_full`=0xc, `test_vadd`=0x4, `test_slice`=0xb7, `test_cast`=0x78ab9bcd,
`test_pack`=0xdead). Zero regressions from this change. `test_vreduce`/`test_cmov` still fail at
the pipeline level for their already-documented, unrelated `engine_new`-divergence reason;
`test_2d`/`test_logic`/`test_fsqrt` remain blocked for their own pre-existing, unrelated reasons.

### Bottom line
**All three originally-broken tests (`test_struct`, `test_spill`, `test_scalar_full`) are now fully
fixed**, via two independent, confirmed root causes: (1) CALL's disassembler sign-extend using bit
index 25 instead of 24 (`McodeDisassemble.cpp:266`), and (2) the simulator's instruction memory
being built to half the size the ISA reference document specifies (`McodeClasses.hpp:139,143`).
Both are precise, citable, reproducible bugs — not hypotheses. Still pending: confirming with the
professor whether this IMEM correction should be applied to the distributed/official engine build
(it should be, per the doc, but it's still his binary to update), and the separate, still-open
`engine_new`-divergence issue behind `test_vreduce`/`test_cmov`'s pipeline failures.

### Data-type coverage note (asked separately, recorded for the record)
Confirmed: `i4`/`u4` are arithmetic-only, no load/store form (matches hardware — minimum transfer
granularity is a byte). `$ld`/`$st` support `i8`/`u8` through `i64`/`u64`, plus `u128`/`u256` wide
loads at the ISA level — but the **compiler** does not yet generate `u128`/`u256` loads/stores
(confirmed via grep: zero references in `codegen.py`/`ir_gen.py`/`ir.py`); vector arithmetic
(`$v`/`$dot`/`$vreduce`) only operates on values already manually packed into a 64-bit register.
Tested/hardware-confirmed vector element widths: `vi32`/`vi16`/`vi8` for `$v` ops and `$vreduce`,
`vi16` for `$dot`. Untested: `vi4`, any unsigned vector (`vu*`) type — the intrinsic parser
(`ir_gen.py` `__vadd_`/`__dot_`/`__vreduce_` handlers) does no validation on the type suffix, so
these would compile silently but have never actually been run.

---

## 2026-06-18 — IMEM bump reverted; confirmed by user that 2048 words IS the real hardware limit, not a simulator default (Latest)

The 2048→16384-word IMEM bump from the previous entry was explicitly a verification-only change.
**User confirmed 2048 words is the actual hardware IMEM capacity** — not something the simulator
can legitimately just expand. Reverted `McodeClasses.hpp:139,143` back to `[2048]` / `2*1024`,
rebuilt with `scons`, and re-confirmed `test_spill`/`test_scalar_full` are back to their original
truncated-program values (`0x19` / `0x7ff0`) — i.e. the build is hardware-faithful again.

**The diagnosis from the previous entry stands and is still useful**: both tests' wrongness is
fully and exactly explained by program-too-big-for-IMEM (640 / 448 words silently dropped), not by
any remaining logic bug. The fix now has exactly one viable path: reduce `bundler.py`'s padding
overhead (currently >80% of `test_scalar_full`'s compiled size is mandatory 8-slot control-transfer
padding) so real programs fit in the real 2048-word budget. Not started — this is compiler-side
work, distinct from anything engine-side, and doesn't need the professor's involvement the way the
IMEM question did.

---

## 2026-06-18 — SECOND ROOT CAUSE FOUND: fixed-size 2048-word IMEM silently truncates larger programs. With both fixes together, `test_struct` / `test_spill` / `test_scalar_full` ALL now produce exactly the expected values (Latest)

### The bug
`test_spill`'s and `test_scalar_full`'s remaining wrongness (`0x19`/`0x7ff0` after yesterday's CALL
fix) was never a logic bug at all. `McodeClasses.hpp:139` declares a fixed
`uint32_t __instruction_memory[2048]` (`Instr_Mem_Size_In_Words()` returns `2*1024`, line 143).
`McodeAccelerator.cpp:88-101` (`Init_Instruction_Memory`) silently drops — logs an `Error:`, does
not write, does not abort — any instruction whose `pc >= 2048`. Both programs are bigger than that:

| Test | Real program size | Overflow ("beyond the i-mem size") errors |
|---|---|---|
| test_scalar_full | 2688 words | 640 |
| test_spill | 2496 words | 448 |

The actual `+ $r1 = $r0 + $r9` (the real return-value write, computed correctly from
`g_arith+g_compare+g_logical`) for `test_scalar_full` lives at `pc=0xa68` (2664) — **never loaded**.
Execution runs straight off the end of the truncated program at `pc=0x800`, into zero-filled
("$null") memory, until the tick budget runs out, with r1 frozen at whatever it last held —
in this case the address (`FP-8`, `0x7ff0`) of local `a`, computed many instructions earlier inside
the `while(a>0)` loop's `a--;`, which only *looked* like a meaningful wrong value by coincidence.
Same mechanism for `test_spill` (`0x19` was likewise a stale leftover, not a computed wrong sum).

Both programs are this large mostly because of `bundler.py`'s mandatory full-8-slot padding on any
bundle containing a control-transfer instruction: `test_scalar_full`'s own run reported
"439 non-null / 1921 null" instructions executed — **over 80% of the program is padding**.

### Verification fix (local build only — see caveat below)
`McodeClasses.hpp:139,143`: `__instruction_memory[2048]` → `[16384]`,
`Instr_Mem_Size_In_Words()` → `16*1024`. Rebuilt with `scons`. Re-ran both tests:

| Test | Before (2048-word IMEM) | After (16384-word IMEM) | Expected |
|---|---|---|---|
| test_scalar_full | 0x7ff0 | **0xc** | 0xc (12) |
| test_spill | 0x19 | **0x1d1** | 0x1d1 (465) |

**Exact match, both of them.** Combined with yesterday's CALL sign-extend fix, all three originally
broken tests (`test_struct`, `test_spill`, `test_scalar_full`) now produce exactly the expected
value. Full 19-test re-run with the IMEM-bumped build: all 14 previously-runnable/passing tests
still correct (`test_alu`=0xd, `test_array`=0x96, `test_branch`=0x1, `test_ldst`=0x3e8,
`test_pointer`=0xf, `test_subword`=0x1, `test_dot`=0x5a, `test_cast`=0x78ab9bcd, `test_vadd`=0x4,
`test_slice`=0xb7, `test_pack`=0xdead, plus the three above) — zero regressions from the IMEM bump
itself. `test_vreduce`/`test_cmov` still fail at the pipeline level exactly as before (unrelated —
already traced to `engine_new`'s broader divergence from the `engine_isp` baseline, not to IMEM
size or either CALL/RAS fix). `test_2d`/`test_logic`/`test_fsqrt` remain blocked for their own
pre-existing, unrelated reasons.

### Important caveat — do not treat the IMEM bump as a verified real fix
Unlike the CALL sign-extend bug (a clear-cut decode error against the ISA's own 25-bit field
width), **it is not known whether 2048 words is a real hardware IMEM capacity limit or just an
arbitrary simulator default smaller than the real chip.** Two very different correct fixes follow
depending on which it is:
- If 2048 words is *not* a real hardware limit: bumping the simulator's constant (as done here) is
  the right, permanent fix.
- If 2048 words *is* a real hardware limit: the simulator is correctly modeling the constraint, and
  the actual fix belongs in `bundler.py` — cut the >80% control-transfer-bundle padding overhead so
  compiled programs fit in the real budget, not in the simulator.
**This must go back to the professor before either path is taken as official** — exactly the kind
of "who fixes this" question flagged for later, now with a precise, numeric, reproducible bug
report instead of a mystery. The constant bump here is a local verification build only, same status
as yesterday's two engine-source edits.

Stopping here — both originally-reported test_spill/test_scalar_full mysteries are now fully
explained and numerically confirmed fixed on this verification build.

---

## 2026-06-18 — All `run.sh` scripts under `cmp_wd` repointed from the stale March-6 `engine_isp` snapshot to `engine_new`; 19-test regression re-run through the corrected scripts (Latest)

### What was wrong
Every `run.sh` under `cmp_wd` (and the `write_run_script` template in `compiler.py` that generates
new ones) hardcoded `BIN_DIR=/home/mohithkota/engine_isp/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin`
— a March 6th snapshot, untouched by any of today's work. The actual engine being built and patched
all day lives at `complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin`.

**Correction to the literal instruction this was actioned from**: this did *not* invalidate today's
*reported numbers* — every regression result reported earlier today (the `Pop_From_Ras` checks, the
CALL sign-extend fix verification, the 16-test table) was produced by invoking the `engine_new`
binaries directly with explicit paths, never through a test's own stale `run.sh`. So today's prior
numbers are not "wrong engine" results and don't need to be disregarded. What *was* true: any
**future** run using a bare `./run.sh` (the normal, expected way to run these tests) would have
silently gone back to the stale, unpatched March-6 engine and silently lost every fix from today.
That's the real bug this fixes — a footgun for next time, not a correctness problem with anything
already reported.

### Fix applied
Repointed `BIN_DIR` in every `run.sh` under `cmp_wd` that had the stale absolute path (26 files —
all of `alu/`, `array/`, `branch/`, `ldst/`, `pointer/`, `new_isa_tests/`, including their top-level
per-category scripts) to `complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin`.
Left untouched: `mem_march/run.sh`, `not_used_files/**/run.sh` — these use an unrelated relative-path
scheme (`../../../assembler/bin`) and aren't part of the test suite. (`compiler.py`'s `write_run_script`
template itself was not changed in this pass — still emits the stale path for any newly-compiled test;
flagging for a future pass, not fixed now.)

### 19-test regression, run via the corrected `./run.sh` scripts (not direct binary calls this time)
| Test | Result | Expected | Status |
|---|---|---|---|
| test_alu | 0xd | 13 | pass |
| test_array | 0x96 | 150 | pass |
| test_ldst | 0x3e8 | 1000 | pass |
| test_branch | 0x1 | 1 | pass |
| test_pointer | 0xf | 15 | pass |
| test_subword | 0x1 | 1 | pass |
| test_dot | 0x5a | 90 | pass |
| test_cast | 0x78ab9bcd | 0x78ab9bcd | pass |
| test_vadd | 0x4 | 4 | pass |
| test_slice | 0xb7 | 183 | pass |
| test_pack | 0xdead | 0xdead | pass |
| test_struct | 0x0 | 0 | **pass** |
| test_spill | 0x19 | 0x1d1 (465) | fail (wrong value, separate bug) |
| test_scalar_full | 0x7ff0 | 0xc (12) | fail (wrong value, separate bug) |
| test_vreduce | pipeline crash (`mcode_align` assertion) | 76 | fail — regression vs. historical baseline |
| test_cmov | pipeline crash (parse exception + segfault) | 600 | fail — regression vs. historical baseline |
| test_logic | pipeline crash (parse exception) | — | fail (pre-existing, held) |
| test_2d | pipeline crash (`mcode_align` assertion) | — | fail (pre-existing, held) |
| test_fsqrt | pipeline crash (`mcode_align` assertion) | — | fail (pre-existing, held) |

**Every number is identical to today's already-reported results.** Running through the corrected
`run.sh` scripts instead of direct binary invocation changed nothing — confirms the prior report was
accurate. test_logic/test_2d/test_fsqrt remain blocked for their pre-existing, unrelated reasons
(documented earlier in this file). test_vreduce/test_cmov remain newly broken — still attributed to
`engine_new` being a diverged codebase from the historical `engine_isp` baseline (~15 files differ
beyond today's two intentional edits), not to either of today's fixes. test_spill/test_scalar_full
remain wrong for their own separate, unidentified reasons. test_struct remains the one confirmed fix.

Stopping here as instructed — no new bugs, no new hypotheses, no further investigation tonight.

---

## 2026-06-18 — ROOT CAUSE CONFIRMED (found manually, not by Claude Code): CALL's disassembler sign-extend used the wrong bit index. Fix verified on a local build — test_struct now passes; test_spill/test_scalar_full still wrong for separate reasons; two new pipeline regressions traced to engine_new being a diverged codebase, not to either fix (Latest)

**Root cause, found by tracing engine source directly (`McodeDisassemble.cpp`, `DisassembleToCallInstr`,
line 266):**
```cpp
int32_t relative_jump = (int32_t) Sign_Extend(25, Get_Slice (24, 0, hex_instr));
```
`Sign_Extend`'s first argument is a bit **index** (`McodeUtils.cpp`: `pad_ones = (1 << sign_index) & x`).
CALL's jump field is 25 bits wide, bits `24:0` — its real sign bit is at index 24, not 25. Calling it
with 25 checks a bit that's always 0 on a value already masked to `24:0`, so negative/backward call
offsets are never sign-extended. Every backward call (callee defined before caller — the normal C
pattern, e.g. `f` before `main`) computes `target + 2^25` instead of `target`, landing in
zero-filled garbage memory. This is exactly why all six engine-layer checks in the
[[project_call_phase_hazard|standalone incident report]] came back clean — none of that code ever
ran in the failing case; the call never reached the callee at all.

**Confirmed precisely isolated to CALL, not systemic.** `DisassembleToBranchInstr` (same file,
line 302) does `Sign_Extend(11, Get_Slice(16,5,...))` — a 12-bit field (bits `16:5`), real sign bit
at index 11, called with 11. **Correct, no off-by-one.** Consistent with for/while loops (which use
BRANCH, not CALL, for backward jumps) having worked correctly all along.

### Fix applied and rebuilt (verification build only — not a replacement for the professor's distributed binaries)
`complier_Apara/engine_new/.../McodeDisassemble.cpp:266`: `Sign_Extend(25, ...)` → `Sign_Extend(24, ...)`.
Rebuilt with `scons` (11:58 timestamp) on top of the same local copy that already had today's
earlier, separately-confirmed-inert `mc->Pop_From_Ras()` fix in `McodeExecute.cpp`.

### Check 3 — noop_call.c halt-before-`f`'s-return probe
**`f`'s body now executes, and r1 = 6 at the halt point.** Before the fix: `$call f` resolved to
`npc=0x2000018`, ran 10 ticks into garbage, never entered `f`. After the fix: disassembly shows
`$call l_24` (correct), the bundle at `pc=0x28` sets r1=6 and branches into `f_epilogue`, SP/FP
restore runs, and the inserted `$halt` fires cleanly at `pc=0x31` after 17 ticks — **r1=6, confirmed**.

### Check 4 — full regression (16 runnable tests; `test_logic`/`test_2d`/`test_fsqrt` excluded, pre-existing unrelated blockers)
| Test | Before today | After fix | Expected | Status |
|---|---|---|---|---|
| test_alu | 0xd | 0xd | 13 | unchanged ✓ |
| test_array | 0x96 | 0x96 | 150 | unchanged ✓ |
| test_ldst | 0x3e8 | 0x3e8 | 1000 | unchanged ✓ |
| test_branch | 0x1 | 0x1 | 1 | unchanged ✓ |
| test_pointer | 0xf | 0xf | 15 | unchanged ✓ |
| test_subword | 0x1 | 0x1 | 1 | unchanged ✓ |
| test_dot | 0x5a | 0x5a | 90 | unchanged ✓ |
| test_cast | 0x78ab9bcd | 0x78ab9bcd | 0x78ab9bcd | unchanged ✓ |
| test_vadd | 0x4 | 0x4 | 4 | unchanged ✓ |
| test_slice | 0xb7 | 0xb7 | 183 | unchanged ✓ |
| test_pack | 0xdead | 0xdead | 0xdead | unchanged ✓ |
| **test_struct** | 0xa | **0x0** | 0 | **NOW PASSES** |
| test_spill | 0x328 | 0x19 | 0x1d1 (465) | changed, still wrong |
| test_scalar_full | 0x7ff0 | 0x7ff0 | 0xc (12) | unchanged, still wrong |
| test_vreduce | 0x4c (pass) | **crashes** | 76 | **NEW regression** |
| test_cmov | 0x258 (pass) | **crashes** | 600 | **NEW regression** |

**Only `test_struct` is actually fixed by today's change.** `test_spill` now runs to natural
completion instead of jumping into garbage (confirmed: its final `$return` reaches `npc=0x800`
cleanly, no `0x2000xxx` jump anywhere in the trace) — real structural progress — but its computed
value (`0x19`) still doesn't match expected (`0x1d1`), so a second, separate bug remains in it.
`test_scalar_full` was already running to natural completion before today (no garbage jump either
before or after the fix) — its wrongness was never caused by the CALL bug, so the fix had no effect
on it; a separate, still-unidentified bug remains.

**`test_vreduce` and `test_cmov` are new pipeline-level failures — NOT caused by either of today's
two edits.** Confirmed by running the identical, untouched `.mcode` source through the original
pre-rebuild `engine_isp` binary: both align and run cleanly there. Only `engine_new`'s rebuilt
toolchain crashes on them (`test_vreduce`: `mcode_align` aborts with the same pre-existing
`Calculate_Pad_For_Alignment: Assertion '0' failed` that `test_2d`/`test_fsqrt` have always hit;
`test_cmov`: a parser exception — `expecting QUESTION, found '('` — followed by a segfault, a
different failure mode entirely). **Cause found, not chased further per instruction**: a direct
`diff -rq` between `engine_isp/AjitHpcAccelRepo/.../assembler/src` and
`complier_Apara/engine_new/AjitHpcAccelRepo/.../assembler/src` shows roughly 15 files differ beyond
today's two intentional one-line edits — `McodeBundle.cpp`, `McodeParser.cpp`, `McodeOperations.cpp`,
`McodeProgram.cpp`, `McodeRoot.cpp`, `McodeUtils.cpp`, `MachineRun.cpp`, `McodeBinaryCode.cpp`,
`McodeInstructions.cpp`, plus two files (`McodeAccelerator.cpp`, `McodeFpuUtils.cpp`) that don't
exist in the `engine_isp` tree at all. **`engine_new` is not "`engine_isp` plus two patches" — it's
a separately-diverged codebase snapshot**, and today is the first time its toolchain has actually
been built and exercised against `test_vreduce`/`test_cmov`. The regression is real but belongs to
that pre-existing divergence, not to the CALL sign-extend fix or the `Pop_From_Ras` fix.

### Bottom line for the professor report
The CALL disassembler sign-extend bug (`Sign_Extend(25,...)` → should be `Sign_Extend(24,...)`,
`McodeDisassemble.cpp:266`) is real, precisely isolated (BRANCH confirmed unaffected), and the fix
measurably works — `noop_call.c` now executes `f` and gets r1=6, and `test_struct` now passes
end-to-end on hardware-equivalent simulation. It is **not** a complete fix for the three originally
broken tests: `test_spill` and `test_scalar_full` each have at least one more, separate, unidentified
bug. **This verification build (`engine_new`) should not be treated as a clean baseline** — it
diverges from the `engine_isp` binaries used for the original 13-test passing baseline in ~15 files
unrelated to this fix, which is the most likely explanation for `test_vreduce`/`test_cmov` newly
failing here. Recommend re-testing this exact one-line fix against the professor's actual
distributed `engine_isp` source tree (not `engine_new`) before reporting it as the official fix.
Stopping here as instructed — no further investigation this session.

---

## 2026-06-18 — Engine rebuilt with `mc->Pop_From_Ras()` fix in `McodeExecute.cpp`; three confirmation checks run, all unchanged

Engine rebuilt (`mcode_run` timestamp Jun 18 11:18, in
`complier_Apara/engine_new/AjitHpcAccelRepo/.../assembler/bin/`) with one change: a second source
suggested adding `mc->Pop_From_Ras();` inside `___execute_return_operation___` in
`McodeExecute.cpp`, right after `Top_Of_Ras_Stack()`, before `Set_Npc`, as a possible fix for the
call-depth bug. Three checks run against the rebuilt binary, as instructed. No further
investigation performed this session per explicit instruction.

**1. `noop_call.c` with `$halt` placed right before `f`'s `$return` — r1 at that point now?**
Could not observe r1 at the intended point, for a reason orthogonal to the fix being tested: the
`$call f` instruction (`main`→`f`, a backward call — `f` is emitted at a lower address than the
call site) does not transfer control into `f` at all. The runtime's own trace shows
`npc=0x2000018` instead of `0x18` (`f`'s real address); PC then reads zero-filled memory and the
run stops after 10 ticks, having never executed `f`'s body or the inserted `$halt`. r1 simply
stays at `main`'s own pre-call prologue value, `0x328`, for the whole run. **Confirmed identical
with the old (pre-rebuild) `mcode_run` on the exact same `.obj`** — same `npc=0x2000018`, same
final r1=`0x328`. So this specific check is unaffected by today's rebuild either way — the rebuild
neither fixes nor changes it, because the call never reaches the code path the fix touches.

**2. test_struct / test_spill / test_scalar_full — did 0xa / 0x328 / 0x7ff0 change?**
**No change.** Re-ran each existing `.obj` against the rebuilt `mcode_run`: final r1 =
`0xa` (test_struct), `0x328` (test_spill), `0x7ff0` (test_scalar_full) — identical to the
pre-rebuild baseline. Cross-checked with a clean control (same three `.obj` files run against the
old pre-rebuild `mcode_run`): identical `0xa` / `0x328` / `0x7ff0`. The `Pop_From_Ras()` fix did
not change any of these three results.

**3. test_alu / test_pack sanity check — still pass on the rebuilt binary?**
**Yes, both still pass**, no regressions from the rebuild itself. test_alu: final r1=`0xd` (13),
matches expected, zero errors in the run log. test_pack: final r1=`0xdead`, matches expected, zero
errors in the run log.

### Bottom line
The rebuilt engine changes nothing observable in any of these three checks — test_struct/
test_spill/test_scalar_full remain exactly as wrong as before (still do not trust their results),
test_alu/test_pack remain correct (rebuild introduced no regression), and the `noop_call.c` halt
probe couldn't exercise the fixed code path because the backward `$call f` itself resolves to a
wrong target (`0x2000018`) before execution ever reaches `f`. Stopping here as instructed — no
further hypotheses or investigation this session.

---

## 2026-06-17 — STANDALONE INCIDENT REPORT: nested function calls are broken; six causes checked and ruled out compiler-side; needs simulator source

**This entry is written to be self-contained — readable cold, without the rest of this file or
any conversation history.** It documents one evening's investigation into why `test_struct`,
`test_spill`, and `test_scalar_full` produce wrong results, despite compiling and assembling
without any error.

### The bug, in one sentence
**Any C function called from within another function (not `main` itself) returns a garbage
value instead of its actual return value** — confirmed with the simplest possible repro:
```c
int f(void) { return 6; }
int main() { return f(); }
```
This returns garbage (specifically, whatever `main`'s own prologue happened to leave in
register `$r1` — see below), not `6`.

### Why this matters / scope
Every test in the 19-program suite that has ever passed either has zero function calls beyond
`main`, or only calls compiler intrinsics (`__pack`, `__dot_*`, `__cmov_*`, `__vadd_*`, etc. —
these compile to inline instructions and never emit a real `$call`). `test_struct`,
`test_spill`, and `test_scalar_full` are the ONLY three tests in the entire suite with a real,
user-defined nested function call — which is exactly why this was never caught until today's
full hardware regression. **Do not trust the results of these three tests.** Everything else in
the suite is unaffected (confirmed via multiple zero-regression full-suite re-runs throughout
tonight's work).

### Two real bugs found and fixed along the way (kept — both still valid, just insufficient)
While investigating, found and fixed two genuine, separate bugs in `bundler.py` (the VLIW
instruction-bundling pass). Both are confirmed correct, cause zero regressions on the rest of
the suite, and are worth keeping regardless of the unresolved issue below:

1. **Aligner reorders bundle instructions by type.** `mcode_align` does not preserve program
   order within a bundle — it relocates `$ld`/`$st` to later slots than ALU/`$set`, regardless
   of what order the compiler emitted them in. Proven by diffing unaligned vs aligned mcode for
   a function prologue: `$st [SP+0] OLD_FP` / `+FP=SP` (intent: save OLD fp, then update) became
   `+FP=SP` / `$st [SP+0] FP` (now stores the NEW fp). Fixed by adding `c_mem_reads` tracking in
   `bundler.py`'s `_pack_bundles`: a non-memory instruction writing a register that an
   already-bundled memory instruction reads now forces a bundle split.
2. **Conservative SP+`$call` bundling hazard** (added at explicit request, since the exact
   phase interaction between an SP-modifying instruction and a co-bundled `$call` was unverified
   and the pattern is rare/cheap to avoid): `$call` is now never bundled with an instruction
   that writes `$r27` (SP) — forces a split.

Neither fix resolves the bug described here. Both were verified via full 19(+2)-test regression
— zero behavioral change to any previously-passing test.

### Six things checked and ruled out, in order, each with its own falsifiable test

**1. Bundle padding.** Per the ISA's quirk that any bundle containing a control-transfer
instruction must be padded to a full 8 instructions: checked the `.aligned.mcode` for the
failing bundle directly. It IS correctly padded to 8. Not a padding bug.

**2. Jump-target resolution.** Disassembled the assembled `.obj` (`mcode_disassemble`) and
confirmed `goto f_epilogue` resolves to the exact bundle containing `f_epilogue`'s real first
instruction (the SP-restore `+ $r27 = $r0 + $r26`). Not an addressing/jump-target bug.

**3. Bundle shape / instruction ordering within the failing bundle.** The failing bundle inside
`f` is:
```
- $r27 = $r27 - $r1       (SP -= frame size)
+ $r1  = $r0 + 6            (set return value)
? $r0 == $goto f_epilogue   (unconditional jump)
```
Control experiment: forced `main` itself into this exact same 3-instruction shape via
`int main(){return 6;}` (its own prologue's SP-reduction lands in the same bundle as the
return-value-set and the jump — same shape, same instructions, different label name only).
**This works correctly** (`r1=6` at the end). Diffed the two bundles byte-for-byte — identical
except for the label name (`f_epilogue` vs `main_epilogue`, necessarily different relative
offsets). So the bundle shape itself, and whatever order the aligner puts its three instructions
in, is NOT the problem — it works when this exact shape is `main`'s own code.

**4. Caller-save / restore ordering.** There's a known fix from the 28-register-allocator work:
copy the return value (`$r1`) to its destination temp BEFORE restoring any caller-saved
registers, because restoring might otherwise clobber `$r1`. Checked whether this fails to
trigger for an edge case (callee with no arguments, no other live locals at the call site) by
tracing two minimal cases directly from the generated mcode:
   - `noop_call.c` (`f` takes no args): there is NO caller-save/restore sequence at all in the
     generated mcode — zero `$st`/`$ld` between `$call f` and the return. Nothing is live at
     the call site, so the save list is empty. The "capture" instruction is `+r1=r0+r1`, a
     self-copy, only because the register allocator happened to assign the call's result temp
     to `$r1` itself (the first register in a fresh allocator pool). The ordering fix isn't
     misfiring here — it's simply never invoked.
   - `get_x_repro.c` (struct-pointer argument forces a real caller-save): the register that gets
     saved-and-restored is `$r3` (the pointer being passed as the argument) — NOT `$r1`. The
     capture instruction (`+r4=r0+r1`, reads r1 writes r4) and the restore instruction
     (`$ld r3 [FP-104]`, writes r3) touch completely disjoint registers. No possible conflict
     between them regardless of execution order.
   - In both cases, register-probed `$r1` immediately upon `$call` returning — already wrong in
     both (`0x328`, `0x7fe8` respectively) — confirming the corruption exists before ANY of this
     capture/restore code runs at all.

**5. Is the corruption visible from inside the callee, before `$return` even executes?**
(Hypothesis: if so, that's evidence of a problem visible purely from the callee's own
instruction stream, independent of anything about RAS or the call-return mechanism externally.)
Inserted `$halt` directly inside `f`'s epilogue, immediately after the unconditional jump lands
there, before the `$ld`/`$return` bundle executes. **`$r1 = 0x328` (808) at that exact point** —
already wrong, confirmed before `$return`/RAS-pop runs at all.

Cross-checked the literal values, not just "both look wrong": in `main`, `$r1` immediately
*before* `$call f` = `0x328`. Immediately *after* `$call f` returns = `0x328`. Inside `f`'s
epilogue = `0x328`. **All three are the identical bit pattern** — `$r1` never changes from
`main`'s own prologue-time value (`$set $r1 0 808`, its frame-size constant) anywhere across the
entire call.

(Side-note, fully resolved: `test_spill`'s wrong final answer is *also* exactly `0x328`. Checked
directly — `test_spill`'s `main` independently emits the identical `$set $r1 0 808` in its own
prologue. Both land on 808 because `72 (min stack-frame floor for ~0 declared locals) + 224
(caller-save reserve) + 512 (spill reserve) = 808` is the standard frame constant for any
function with few/no stack-declared locals — true for both, since test_spill's "28 live values"
during its spilling test are register-held temps, not stack-declared locals. Not a coincidental
number; the same underlying failure leaving the same literal residue in two different programs.)

**6. Is the failing write specific to something about how the CALLEE's own epilogue code is
generated — e.g., a scratch register accidentally chosen as `$r1`, clobbering the just-written
return value before `$return` runs?** Specific, falsifiable hypothesis: that the epilogue's SP
restoration might need a `$set <reg> 0 <framesize>` + subtract two-step sequence (since
frame sizes like 808 exceed the ALU's 10-bit signed immediate range, the same reason the
*prologue's* SP reduction needs this two-step pattern) — and that this might pick `$r1` as
its scratch register, clobbering the return value that was just written.

Checked directly from generated mcode, three separate real instances (`noop_call.c`'s `f`,
`get_x_repro.c`'s `get_x`, and `test_spill`'s actual `f01`) — **all three byte-identical**:
```
<name>_epilogue:
||
    + $r27 ($i64) $r0 $r26      ← SP = FP — a plain REGISTER COPY, no constant involved
;
||
    $ld ($i64) $r26 [$r27 + 0]   ← restore old FP
    $return
;
```
No `$set` anywhere in any epilogue, in any of the three. The reason: restoring SP only needs
`SP = FP` (a register-to-register copy), because FP already holds the exact value SP needs to
become — unlike the *prologue's* SP reduction, which needs to load the frame-size *constant*
and therefore needs the two-step `$set`+subtract pattern. There is no constant to load in the
epilogue, hence no scratch register of any kind is ever borrowed there. Cross-checked against
`_gen_IRFuncEnd` in `codegen.py`: it is a fixed, unconditional 3-instruction emission with zero
register-allocation logic, so it cannot produce a different pattern for any frame size, ever.
**Hypothesis not confirmed. No fix applied.**

### What's left standing after all six checks
Padding ✓ clean. Jump-target resolution ✓ clean. Bundle shape/ordering ✓ clean (proven by a
working depth-1 control case with byte-identical code). Caller-save/restore ordering ✓ clean
(checked two different minimal cases). Visible-before-`$return` ✓ confirmed wrong, but not
attributable to anything in the callee's own code (point 6). Epilogue scratch-register clobber
✓ clean (no scratch register exists in any epilogue, of any kind).

The one fact every check above converges on: **identical, byte-for-byte generated code is
correct when executed as the program's first (`apara_start→main`) call, and incorrect when
executed as a nested (`main→anything`) call.** Nothing in the compiler's output differs between
these two cases — only the calling context does. Re-confirmed there is zero special-casing of
the string `"main"` anywhere in `codegen.py`/`ir_gen.py`/`compiler.py`.

### What to check next (needs engine/simulator source — out of reach from the Python/mcode side)
In `MachineRun.cpp` (or wherever `$call`/`$return`/RAS push-pop is actually implemented):
does anything beyond PC save/restore happen across a `$call`/`$return` pair — specifically,
does register-file write-enable, or any register snapshot/restore, get gated by RAS depth in a
way that would make a register write commit correctly at depth 1 (the outermost call) but not
commit at depth ≥2 (any nested call)? That is the precise, narrow question this evening's
investigation has earned: not "is something wrong with calls" (six checks say the Python
compiler's output is correct), but "why does identical code commit a register write differently
depending on how many calls deep it's executing."

### Status
**Unresolved. Pausing here as planned.** Two real, unrelated bugs were found and fixed in
`bundler.py` along the way (kept, zero regressions, see above) — neither is the cause of this
issue. `test_struct`/`test_spill`/`test_scalar_full` remain untrustworthy until the question
above is answered. Everything else in the 19(+2)-program suite is confirmed unaffected and
unchanged by tonight's work.

---

## 2026-06-17 — r1's write fails INSIDE the callee, before $return; but generated code is byte-identical to a working depth-1 case

Two precise checks, no engine source needed:

**Check 1 — is r1 already wrong inside `f`, before `$return` runs?** Inserted `$halt` directly
inside `f`'s epilogue, before `$ld`/`$return` execute (right after the unconditional jump from
`f`'s body lands there). `r1 = 0x328` (808) at that exact point. So `+r1=r0+6` (the return-value
write, in the same bundle as the jump) never took effect — confirmed BEFORE `$return`/RAS-pop
even runs, inside `f`'s own execution.

**Check 2 — literal hex values, not characterizations.** In `main` (`noop_call.c`):
`r1` immediately before `$call f` = `0x328`. `r1` immediately after `$call f` returns = `0x328`.
Inside `f`'s epilogue (check 1) = `0x328`. **All three are the identical bit pattern** — `r1`
never changes from `main`'s own prologue-time value, all the way through the call and back.

**Is test_spill's `0x328` the same value or coincidence?** Checked directly: `test_spill`'s
`main` also emits `$set $r1 0 808` in its own prologue. Traced why both land on 808:
`72 (frame-size floor for ~0 declared locals) + 224 (caller-save) + 512 (spill reserve) = 808` —
the standard frame constant for ANY function with few/no stack-declared locals (test_spill's
main has few real stack locals; its 28 "live values" are register-held temps, not stack slots).
Not a coincidental number — the same underlying failure (a write to r1 not committing) leaving
behind the same literal value because both mains' prologues happen to use the same constant.

**But: ruled out this being specific to `f`'s generated code.** Diffed the exact failing bundle
inside `f` against the identical-shape bundle inside `main_trivial.c`'s `main` (the depth-1
control case that works) — byte-for-byte identical except for the label name (`f_epilogue` vs
`main_epilogue`, necessarily different relative offsets). Also confirmed zero special-casing of
the string `"main"` anywhere in `codegen.py`/`ir_gen.py`/`compiler.py` — `main` and `f` go
through the exact same codegen path. So identical generated code produces a correct result at
depth 1 and an incorrect one at depth 2+, with nothing in the instruction stream itself
differing. This still points at execution context (first `$call` vs a nested one), not at
anything the compiler generated differently — laid out as evidence for review, not asserted as
final, since "identical code, different result" is unusual enough to warrant a second opinion
before fully ruling out something exotic on the generation side.

**Still not fixed. Still do not trust test_struct/test_spill/test_scalar_full.**

---

## 2026-06-17 — Ruled out caller-save/restore ordering bug too — corruption exists the instant $call returns, before ANY post-call codegen runs

Checked the specific hypothesis that the known "copy r1 to dest BEFORE restoring saved registers"
28-register-allocator fix (see [[project_28reg_allocator]]) might not be triggering for a
no-argument/no-other-live-locals callee. Traced directly from the generated mcode for both
minimal repros, no engine source needed:

1. **`noop_call.c`** (`int f(void){return 6;} int main(){return f();}`): there is NO
   caller-save/restore sequence in the generated mcode at all — zero `$st`/`$ld` between
   `$call f` and the return. `saved` is empty because nothing is live at that call site. The
   "capture" is a self-copy (`+r1=r0+r1`) only because the allocator happened to assign the
   call's result temp to `r1` itself. The ordering fix isn't misfiring — it's not even invoked.
2. **`get_x_repro.c`** (argument triggers real caller-save): the saved/restored register is `r3`
   (the pointer being passed as the argument), NOT `r1`. The capture (`+r4=r0+r1`, reads r1
   writes r4) and the restore (`$ld r3 [FP-104]`, reads FP writes r3) touch completely disjoint
   registers — no possible conflict between them regardless of bundle/instruction order.

**Then checked the more fundamental thing directly**: in both cases, register-probed `r1`
immediately upon `$call` returning, before ANY of this capture/restore code executes. **Already
wrong in both** — `0x328` for `noop_call.c`, `0x7fe8` for `get_x_repro.c` (matching prior
findings). So the corruption is present at the exact moment control returns from the call,
before the compiler's own post-call sequencing (capture, restore, or otherwise) has run at all.

**Conclusively rules out**: bundle padding (checked earlier), jump-target resolution (checked
earlier), bundle-shape/ALU-vs-branch ordering (checked earlier via the `main_trivial.c` control
case), and now caller-save/restore ordering (checked here, in both the simplest and the
argument-passing case). The bug is not anywhere in `bundler.py` or `codegen.py`'s call-handling
logic that's been inspected so far — it is specifically about what register state exists the
instant a depth-≥2 `$call` returns. This now needs `MachineRun.cpp`'s `$call`/`$return`/RAS
implementation to go further.

---

## 2026-06-17 — Ruled out compiler-side bundling/addressing bugs for nested calls — isolated to a register-state question at the $call/$return boundary

Two cheap, compiler-side checks before escalating to a hardware question (both came back clean):

1. **Bundle padding**: confirmed the minimal repro's failing bundle (`- $r27=$r27-$r1` / `+$r1=$r0+6`
   / `?goto f_epilogue`) IS correctly padded to a full 8 instructions in the `.aligned.mcode`,
   matching the control-transfer quirk. Not a padding bug.
2. **Jump target resolution**: disassembled the assembled `.obj` and confirmed `goto f_epilogue`
   resolves exactly to the bundle containing `f_epilogue`'s real content
   (`+ $r27 ($i64) $r0 $r26`, the SP-restore instruction) — no address mismatch. Not a compiler
   addressing bug.
3. **test_scalar_full**: confirmed directly — it DOES call `add3`/`max2`/`fact`, real nested
   user-function calls, same shape as test_struct/test_spill. Plausible same root cause but not
   separately proven beyond having the same call shape.

### New, sharper finding: the exact same bundle works at depth 1, fails at depth 2+
Since the two checks ruled out a compiler bug, ran a control experiment: forced `main` itself
into the IDENTICAL failing bundle shape via `int main(){return 6;}` (its own prologue's SP
reduction lands in the same bundle as the return-value-set and the unconditional jump — same
3-instruction shape as the failing case inside `f`). **This works correctly** (`r1=6`). The
*only* difference between the working and failing case is call depth: `apara_start→main` (depth
1) works; `main→f` (depth 2+) doesn't, for an otherwise byte-identical bundle pattern.

Traced register state precisely across the depth-2 call boundary (`main` calling the trivial
`f(){return 6;}`): immediately after `$call f` returns, `r26` (FP) and `r27` (SP) are BOTH
correctly restored to main's pre-call values (`0x7ff8`, `0x7cd0` respectively — verified
correct). But `r1` reads back as `0x328` (808) — exactly the constant `main`'s OWN prologue had
set in `r1` *before* making the call. It's as if `f`'s write to `r1` (its return value) never
propagates back to the caller at all, while FP/SP restoration works perfectly.

### Conclusion: not a compiler bug, narrowed to a specific hardware/simulator question
Padding ✓, jump-target resolution ✓, bundle shape ✓ at depth 1 — the only remaining variable is
nested call depth itself. This is now a precise question for `MachineRun.cpp` (or wherever
`$call`/`$return`/RAS handling lives): does anything beyond PC save/restore happen across a
`$call`/`$return` pair that could cause the return-value register specifically to revert to its
pre-call value, and why would that depend on call depth (works for apara_start→main, fails for
main→anything)? Recommend checking RAS push/pop logic and whether register file state is
snapshotted/restored alongside PC for any depth beyond the outermost call.

**Still not fixed. Still do not trust test_struct/test_spill/test_scalar_full's results.**

---

## 2026-06-17 — Conservative SP+$call bundler fix applied (as requested) — does NOT resolve struct/spill/scalar_full; root cause is deeper than first thought

### The requested fix, applied exactly as scoped
Added a new hazard case to `bundler.py`: never bundle `$call` together with an instruction that
modifies SP (`$r27`) — forces a bundle split between them. Conservative, narrow, only fires near
call sites. **Zero regressions** — re-ran the full 19-test suite (+2 isolated subword tests),
every previously-passing test (`test_alu/array/subword/ldst/branch/vadd/slice/vreduce/pointer/
cast/cmov/dot/pack/subword_i8/subword_i16`) still produces the exact same correct value.
`test_2d`/`test_fsqrt` still crash the aligner, unrelated, untouched (held per instruction).

### But it does NOT fix test_struct / test_spill / test_scalar_full — all three UNCHANGED
| Test | Before this fix | After | Expected |
|---|---|---|---|
| test_struct | 0xa | 0xa (unchanged) | 0 |
| test_spill | 0x328 | 0x328 (unchanged) | 0x1d1 |
| test_scalar_full | 0x7ff0 | 0x7ff0 (unchanged) | 0xc |

### Why: the SP+$call bundling was never the actual root cause — found something deeper
Verified the fix correctly splits the bundle (`$call f` now alone in its own bundle, confirmed in
the generated mcode) — but re-tested the simplest possible nested call
(`int f(int x){return x+1;} int main(){return f(5);}`) and it's STILL wrong after the fix.
Traced further and found an even smaller failing case:
```c
int f(void) { return 6; }
int main() { return f(); }
```
This fails too — `f` returns garbage instead of 6. Traced the corruption to INSIDE `f`, in the
bundle that sets the return value and jumps:
```
- $r27 = $r27 - $r1     (SP -= frame size)
+ $r1  = $r0 + 6          (set return value)
? $r0 == $goto f_epilogue (unconditional jump)
```
Register-traced this directly: by the time control reaches `f_epilogue` (immediately after this
bundle), r1 is ALREADY wrong — the `+r1=6` write did not take effect, even though this is a
plain ALU write with no memory instruction involved at all. This is a DIFFERENT mechanism than
the aligner's ALU-vs-MEM slot reordering (see the previous entry below) — here a register write
co-bundled with an unconditional jump appears not to commit, specifically for a function reached
via a NESTED call (depth ≥ 2: apara_start→main→f). The exact same "+result; goto epilogue"
shape is used by `main`'s own return in literally every passing test (depth 1: apara_start→main)
and works fine there — so it's specific to nested calls, not the pattern in general.

**Not yet fixed. Not yet root-caused to a specific, nameable mechanism** — only empirically
narrowed down to "write + unconditional jump, same bundle, inside a depth-≥2 call". This is now
the third open hardware-semantics question alongside `$nop`'s parse failure and (resolved)
`$pack`'s operand order — needs simulator source inspection, not further guessing from this side.
Recommend checking how `MachineRun.cpp` (or wherever bundle retirement/writeback is implemented)
handles register writes in a bundle that also contains a control-transfer instruction, especially
across a `$call` boundary.

### Bottom line on function calls
**Do not trust any test with a real (non-intrinsic) nested function call yet.** `test_struct`,
`test_spill`, `test_scalar_full` remain the only three tests in the suite with this shape, and
all three are still broken for a reason beyond the two bundler fixes applied so far today.

---

## 2026-06-17 — Major finding: aligner reorders bundle instructions by type (ALU before MEM) — partially fixed, ANY real function call is at risk

### XNOR fix applied, but test_logic blocked by something else
Applied `'~~' → '~^'` in `_APARA_OP` — confirmed correct and necessary on its own. But `test_logic`
still fails identically: `$nop` itself doesn't parse, even in total isolation (a file containing
only `$nop` fails; `$null` in the same harness works). See [[project_nop_parse_bug]]. Unrelated
to XNOR — a second, separate blocker in the same test.

### The big discovery: the EXTERNAL ALIGNER REORDERS instructions within a bundle
While tracing `test_struct` (see [[project_call_phase_hazard]] for full detail), found that
`mcode_align` does not preserve the textual order of instructions within a bundle — it relocates
`$ld`/`$st` instructions to LATER slots than ALU/`$set` instructions, REGARDLESS of which order
the compiler wrote them in. Proven by diffing a function's unaligned vs aligned mcode side by
side: `$st [SP+0] OLD_FP` / `+FP=SP` (intended: save OLD FP, THEN update FP) gets reordered to
`+FP=SP` / `$st [SP+0] FP` (now stores the NEW FP). **This silently breaks the "VLIW reads all
operands before any writes" assumption bundler.py was built on, specifically whenever a memory
instruction is meant to read a register an ALU instruction in the same bundle is about to write.**

**Fixed (partially) in `bundler.py`**: added `c_mem_reads` tracking — a non-memory instruction
writing a register that an already-bundled memory instruction reads now forces a split. Verified
this resolves the specific FP-corruption-on-return case (confirmed via register trace: FP is now
correctly restored to the caller's value after a callee returns). **No regressions** — full 19+2
test suite re-run, all previously-passing tests still pass; `test_vadd`/`test_slice`/`test_cast`/
`test_dot`/`test_vreduce`/`test_subword`/`test_pack` (today's other fixes) all still correct.
`test_scalar_full` improved (`0x3` → `0x7ff0`, an address-shaped value — still wrong, but
different, suggesting partial progress).

### Still broken: the SIMPLEST POSSIBLE function call still fails
```c
int f(int x) { return x + 1; }
int main() { return f(5); }
```
Even with the fix above, this returns `0x328` (808 — main's own frame-size constant, clearly
stale/never-overwritten) instead of `6`. Traced to a bundle where an SP-reducing ALU instruction
and `$call` are packed together: `- $r27 = $r27 - $r1` / `+ $r2 = $r0 + 5` / `$call f`. This is a
DIFFERENT interaction than the memory-phase one just fixed — `$call` itself doesn't count as a
"memory" instruction in the current model, so today's fix doesn't touch it. Whatever the exact
mechanism, the simplest possible nested call is not yet reliable.

**`test_spill` independently confirmed to have the identical bundling pattern**
(`- $r27 = $r27 - $r1` / `$call f01`, same shape) — found by checking its mcode directly, not by
assuming. Consistent with, but not yet proof of, the same root cause. `test_struct` likely the
same (it has nested calls throughout). `test_scalar_full`'s remaining failure not yet pinned to
this specific call site, but its calls (`add3`, `max2`, `fact`) are real function calls too.

### This affects EVERY test with a real (non-intrinsic) function call
Going back through the suite: every test that's passed so far either has no function calls beyond
`main`, or only calls compiler intrinsics (`__pack`, `__dot_*`, `__cmov_*`, etc. — these compile to
inline instructions, never `$call`). `test_struct`/`test_spill`/`test_scalar_full` are the ONLY
tests in the suite with real `$call`-based user function calls — which is exactly why this has
never been caught before. **Not a hypothesis: confirmed directly with the 2-line `f(x)=x+1`
repro above, which has nothing struct/pointer/spill-specific about it at all.**

### Not fixed yet — needs hardware/simulator confirmation before guessing further
This is now squarely a "what does the simulator actually do" question, the same category as the
`$pack` operand-order and `$nop` parse issues. Specifically: when a bundle contains an SP-modifying
ALU instruction together with `$call`, what value of SP does the call/jump mechanism actually use
— the old or the new? And more generally, is there a complete, authoritative description of the
bundle's execution phases (which instruction types execute in which order) anywhere in the
simulator source (e.g. `MachineRun.cpp`, already referenced for the PACK semantics question)?
Guessing further risks another round of "fixed something, still broken for a different reason."

---

## 2026-06-17 — $set-merge bug FIXED; test_subword/test_cast/test_dot/test_vreduce all resolved

### The fix (codegen.py)
`_load_const(reg, value)`: for any value needing more than one 16-bit `$set` field, build the
low 16 bits directly into `reg`, then for each additional chunk — borrow a scratch register via
`_safe_borrow()`, recursively load that chunk into it, shift it into position, OR it into `reg`,
unborrow. Recursion naturally handles arbitrary width (not just 32-bit) since each level peels
off one more 16-bit chunk via `value >> 16`. Also fixes a second latent bug: the old code masked
`hi = (value >> 16) & 0xFFFF`, silently truncating anything beyond 32 bits even if merging had
worked correctly.

`_gen_IRGlobalDecl` (startup global-initializer code) needed a separate, non-recursive version
(`_append_const_lines`, now a static helper) since it runs before any function's register
allocator exists — it can only use the two dedicated init-scratch registers (`$r30`/`$r31`), not
borrow arbitrarily. Iterates chunks with an explicit 64-bit mask instead of recursion (avoids an
infinite loop from Python's sign-extending right-shift on negative values). The DMEM byte-offset
half of that function doesn't need this at all — DMEM is at most 64KB (ISA §1), so a byte offset
always fits one `$set` field; that path now just stays single-chunk and reuses `$r31` directly.

Verified against the original minimal repro (`gi=100100; gi=gi+1; if(gi!=100001)...`) and the
exact `r30` register trace that first proved the bug — now builds correctly and returns success
on hardware.

### Effect: all 4 originally-blocked tests now resolved
| Test | Before | After | Expected |
|---|---|---|---|
| test_subword | -12 (failed check #12) | **0x1 (full pass)** | 1 |
| test_dot | 0xf | **0x5a** | 90 |
| test_vreduce | 0x17 | **0x4c** | 76 |
| test_cast | 0x78ac9ccd (close but wrong) | **0x78ab9bcd** | 0x78ab9bcd |

`test_cast` needed a SECOND fix beyond the $set bug: probing showed `big` (declared `int64_t`,
a typedef) was being stored/loaded via `$ld`/`$st ($i32)` instead of `($i64)`, truncating it.
Root cause: `_type_size`'s `IdentifierType` branch only recognized literal base-type names
("long long", etc.) — pycparser does NOT expand a typedef to its underlying structure at the use
site, so `int64_t x;` arrives as `IdentifierType(names=['int64_t'])`, an unrecognized name,
silently defaulting to 4. This is exactly the "related, not-yet-fixed" half of
[[feedback_elem_size_scalar_bug]], now confirmed as a real bug via this test and fixed: added a
module-level `_TYPEDEF_SIZE` dict in `ir_gen.py`, populated by `visit_Typedef` for every typedef
(`_TYPEDEF_SIZE[node.name] = _type_size(node.type)`), consulted by `_type_size` as a fallback
after the literal base-type dict. Confirmed fix: `big` now uses `$st`/`$ld ($i64)`, and the full
expected value `0x78ab9bcd` comes out correct.

Full regression re-run (19 programs + the 2 isolated subword tests): no regressions, all
previously-passing tests still pass; `test_struct`/`test_spill`/`test_scalar_full` unchanged
(separate causes, not yet investigated); `test_2d`/`test_fsqrt` still crash the aligner
(held, untouched, per explicit instruction).

---

## 2026-06-17 — XNOR mnemonic fixed; test_logic blocked by a SEPARATE, new issue: $nop doesn't parse

`codegen.py`'s `_APARA_OP['~^']` was `'~~'`, now `'~^'` — confirmed correct and necessary (the
literal `~~` symbol has no meaning in the ISA). But `test_logic` still fails to assemble after
this fix, with the exact same error as before: `test_logic.mcode:46:7: expecting ''u'', found
''o''` — at `$nop`. **Isolated with a minimal repro: a file containing only `$nop` (nothing
else) fails the identical way; a file containing only `$null` in the same harness parses fine.**
So this is a real, separate, confirmed assembler-grammar issue with `$nop` specifically — not
something the XNOR fix touches, and not something I should guess at (same category as the
$pack-operand-order and $set-label questions: the ISA doc says `$nop` is valid syntax, but the
parser doesn't accept it, and that gap can only be resolved by checking the parser source like
`McodeParser.cpp`, not by guessing alternate spellings). **`__nop()` is only used by test_logic** —
no other test in the suite touches it. Holding `test_logic` here pending grammar confirmation.

---

## 2026-06-17 — $pack fixed (grammar + bundler hazard); same hazard gap fixed for 6 more instructions

### $pack: two stacked bugs, both now fixed
1. **Grammar/operand order** (user confirmed via `McodeParser.cpp:570`): real order is
   `$pack <packed_nbits> <rd> <word_nbits> <rs2>`, not `<rd> <packed_nbits> <word_nbits> <rs2>`
   like the ISA doc's own example shows. `_gen_IRPack` in `codegen.py` now emits the correct order.
   This alone fixed the `mcode_align`/`mcode_assemble` crash.
2. **Bundler hazard**: `bundler.py`'s `_parse_deps` didn't recognize `$pack` *at all* — it has no
   case for it, so it fell through to "zero reads, zero writes" for hazard-tracking purposes. Since
   `$pack rs2` implicitly reads BOTH `rs2` and `rs2+1` (the consecutive pair, per `_gen_IRPack`'s
   `borrow_pair()`), this caused an undetected RAW hazard whenever an adjacent instruction in the
   same bundle wrote `rs2+1` — exactly what `_gen_IRPack` itself emits (`+ p2 = b` right next to
   `$pack ... p1`). Root-caused empirically: traced r1/r2 via truncated-mcode-plus-$halt probes,
   confirmed they held the correct values right up until the bundle boundary, and confirmed `$pack`
   alone (no co-bundled writes) computes correctly (`0xbeefdead` for `lo=0xbeef,hi=0xdead`) —
   isolating the corruption to the missing pair-read tracking specifically.

   **Fixed** by adding a `$pack` case to `_parse_deps` that returns `reads={rs2, rs2+1}`,
   `writes={rd}`.

   `test_pack` now runs end-to-end correctly: r1=`0xdead`. (The test's own comment expected
   `0xBEEF` — that assumption was backwards per the now-confirmed semantics, "first register →
   upper bits, last → lower bits"; `0xdead` is the actual correct low-16-bits of `0xbeefdead`.
   Not a bug, just a stale comment in the test file.)

### Same hazard-tracking gap found in 6 MORE instructions — also fixed
Auditing why the gap existed for `$pack` revealed `_parse_deps` has NO case at all for
`$cast`/`$fsqrt`/`$cmov`/`$slice`/`$v`/`$dot`/`$vreduce` — every one of them fell through to
"zero reads, zero writes," meaning **none of them had any hazard protection** in the bundler.
Confirmed this directly hit `$cast` too while debugging `test_pack` further (a `$ld` writing `r8`
and a `$cast` reading `r8` were bundled together, same RAW-hazard shape, r9 came out as 0 instead
of the loaded value). Added proper read/write tracking for all of them — see `bundler.py` for the
exact field semantics per instruction (notably: `$cmov`'s `rd` is both read and written;
`$dot $accumulate`'s `rd` is also read).

### Effect on the regression batch
Re-ran `test_subword, test_dot, test_struct, test_spill, test_scalar_full, test_vadd, test_vreduce,
test_slice, test_cast` (the batch from the bundler-memory-hazard fix) plus the full hardware
regression after this second fix:

| Test | Before this fix | After | Status |
|---|---|---|---|
| test_pack | crashed at align/assemble | r1=0xdead | **FIXED** (the explicit ask) |
| test_vadd | 0x0 | 0x4 | **FIXED** (expected 4) |
| test_slice | 0x0 | 0xb7 | **FIXED** (expected 183=0xb7) |
| test_cast | 0x0 | 0x78ab0000 | improved, still wrong (expected 0x78ab9bcd) |
| test_dot | 0x7ff0 | 0xf | improved, still wrong (expected 0x5a=90) |
| test_vreduce | 0x20001 | 0x17 | improved, still wrong (expected 0x4c=76) |
| test_subword | -12 | -12 (unchanged) | still fails check #12 — this is the [[project_set_no_merge_bug|$set merge bug]], untouched by this fix |
| test_struct | 0xa | 0xa (unchanged) | still unexplained |
| test_spill | 0x328 | 0x328 (unchanged) | still unexplained |
| test_scalar_full | 0x3 | 0x3 (unchanged) | still unexplained |

No regressions: test_alu, test_array, test_ldst, test_branch, test_cmov, test_pointer all still
correct. test_2d and test_fsqrt still crash the aligner with the same pre-existing assertion
failure (`Calculate_Pad_For_Alignment`) — unrelated to any of today's fixes, not yet diagnosed.
test_logic (XNOR mnemonic typo) also not yet fixed.

### Status: real progress, more remains
Two real bugs found+fixed today in `bundler.py` (memory hazard, missing-instruction hazard gap)
plus the `$pack` grammar fix in `codegen.py`. 3 of 9 originally-failing tests now fully resolved.
The remaining ones have at least one more distinct cause each: the confirmed `$set`-merge bug
(test_subword, very likely test_cast/test_dot/test_vreduce too, given the "improved but still
wrong" pattern suggests SOME of their computation is now right but multi-field constants are
still corrupted), and fully unexplained issues in test_struct/test_spill/test_scalar_full.

---

## 2026-06-17 — Bundler memory-hazard FIXED; found a second bug ($set doesn't merge)

### Bundler fix (bundler.py)
Added memory-address hazard tracking alongside the existing register RAW/WAW/WAR logic.
`_parse_deps` now also returns `(mem_access, mem_write)` — a `(base_reg, offset)` tuple for any
`$ld`/`$st`, textually matched. `_pack_bundles` tracks `c_mem_writes` (addresses stored-to in the
current bundle) and forces a split if a later instruction's `mem_access` matches an address
already in `c_mem_writes` — i.e. store-then-reload (or store-then-store) of the same `[base+offset]`
no longer lands in one bundle. Load-then-store of the same address is still allowed in one bundle
(WAR-safe, matches the existing register WAR philosophy — VLIW reads all operands before writes).

**Verified the fix is correct in isolation**: `int gi = 100; gi = gi+1; if (gi != 101) return -1;
return 1;` — before the fix this returned -1 (stale reload); after the fix, confirmed on hardware,
returns 1. The store and reload are now in separate bundles (checked directly in the generated
mcode).

### The fix resolves 0 of the 9 originally-failing tests — each has additional, different bugs
Re-ran `test_subword, test_dot, test_struct, test_spill, test_scalar_full, test_vadd, test_vreduce,
test_slice, test_cast` after the fix. **All 9 still produce the same wrong r1 as before.** Checked
each one's bundled mcode for residual same-address store+load pairs — none exist (the fix is
working; these tests were never blocked by *this* hazard, or hit a second bug that masks it).

### New bug found: `$set` does not merge into the register, it overwrites
The compiler's constant-loading logic (`_load_const`, `_emit_set_const_into` in `codegen.py`,
and `_gen_IRGlobalDecl`'s init-value writer) assumes calling `$set rd 0 <lo16>` then
`$set rd 2 <hi16>` accumulates a 32-bit value by writing into two different 16-bit slices of the
same register, leaving the other slice alone — matching a literal reading of the ISA doc's SET
section. **Hardware trace proves this is wrong**: for `gi = 100000` (`lo=0x86a0` at field 0,
`hi=1` at field 2):
```
Info: McodeMachine:: Set_Register(30, 0x86a0)   // after $set r30 0 34464 — correct so far
Info: McodeMachine:: Set_Register(30, 0x10000)  // after $set r30 2 1 — OVERWRITES, should be 0x186a0
```
The second `$set` discards the first one's contribution entirely instead of merging. This breaks
loading ANY constant that needs both a low and a high 16-bit `$set` (i.e. doesn't fit in one
16-bit field). Confirmed root cause for `test_subword`'s one failing check (`gi=100000`) and
`test_cast`'s big constant (`0x12345678ABCDEF`); very likely also explains `test_dot`/`test_vadd`/
`test_vreduce` since they build packed-vector constants via shift expressions
(`2LL<<16`, `3LL<<32`, etc.) that get constant-folded at codegen time into single large literals
exceeding 65535. **Not yet confirmed** for `test_slice` (its only large-looking literal, `0xABCD`,
fits in one 16-bit field — doesn't obviously need the broken merge) or `test_scalar_full`/
`test_spill` (no large constants found at all). **Not fixed yet** — needs a real fix to how
multi-word constants are loaded (e.g. shift-and-OR via ALU ops instead of relying on $set to merge,
or some other mechanism — needs confirmation on what `$set` actually does for ALL field indices
before choosing an approach, this is similar in spirit to the earlier `$set`-label question).

### test_struct: still unexplained
Zero large constants, zero same-address store+load pairs in its bundled mcode, yet still returns
0xa (10) instead of the expected 0 on full success — doesn't match either known bug. Needs its own
investigation.

### Status: in progress, mid-investigation
Two real, hardware-confirmed compiler bugs found and one fixed (bundler hazard). The $set-merge
bug is understood but not fixed. test_struct (and possibly test_slice/test_scalar_full/test_spill)
have unidentified root causes still. Do not assume "bundler fix landed" means these 9 tests are
close to passing — they are not, for unrelated reasons.

---

## 2026-06-17 — Full hardware regression (19 programs) on updated engine_isp

User pulled the i32-fixed `engine_isp` binaries into `assembler/bin/`. Ran the complete
align→assemble→run pipeline (not just Python/mcode generation) on all 19 held/safe tests.
**Genuine, on-hardware results — replaces all earlier "compiles clean" / "placeholder" claims.**

### PASS (6) — confirmed correct final r1 on real hardware
| Test | r1 | Expected |
|---|---|---|
| test_alu | 0xd (13) | 13 |
| test_array | 0x96 (150) | 150 |
| test_ldst | 0x3e8 (1000) | 1000 |
| test_branch | 0x1 | 1 (a<b branch taken) |
| test_cmov | 0x258 (600) | 100+200+300 |
| test_pointer | 0xf (15) | 1+2+3+4+5 |

### PIPELINE-LEVEL FAILURES (4) — never reached execution; assembler/aligner rejected or crashed
These are tool/codegen bugs unrelated to today's i32 work (mcode for test_2d was byte-identical
to before today; the other three's relevant instructions are untouched by the elem_bytes/_atype
change).
- **test_logic**: `mcode_align` parse error. Root cause **found**: `codegen.py`'s `_APARA_OP` dict
  maps XNOR (`~^`) to the literal mnemonic `'~~'` — should be `'~^'` per ISA doc §5.1 (opcode
  0xD). One-line typo, not yet fixed (holding for direction).
- **test_pack**: `mcode_align` parse error — `expecting UINTEGER, found '$r7'` on the `$pack $r7
  32 16 $r1` line. The ISA doc's own example (`$pack $r5 32 16 $r1`) shows this exact operand
  order, so either the assembler grammar differs from the doc, or there's a missing token. Don't
  know the actual grammar — same category of unknown as the earlier function-pointer `$set`
  question. Needs your input, not a guess.
- **test_2d**, **test_fsqrt**: both crash `mcode_align`/`mcode_assemble` with the identical
  assertion: `McodeInstructionBundle::Calculate_Pad_For_Alignment: Assertion '0' failed`. Same
  failure family, two different features (2D arrays, fsqrt) — looks like an aligner-side edge
  case in bundle padding, not something visible from the Python side. Not investigated further.

### WRONG COMPUTED VALUE (9) — ran to completion, final r1 incorrect
| Test | actual r1 | expected | 
|---|---|---|
| test_subword | -12 (fails check #12 only — global `int` increment) | 1 (all 12 checks pass) |
| test_dot | 0x7ff0 | 0x5a (90) |
| test_struct | 0xa (10) | 0 |
| test_spill | 0x328 (808) | 0x1d1 (465) |
| test_scalar_full | 0x3 | 0xc (12) |
| test_vadd | 0x0 | 0x4 (4) |
| test_vreduce | 0x20001 | 0x4c (76) |
| test_slice | 0x0 | 0xb7 (183) |
| test_cast | 0x0 | 0x78ab9bcd |

### ROOT CAUSE FOUND for at least one of these, likely explains most: bundler memory hazard
Isolated with a minimal repro (`gi = gi + 1; if (gi != 100001) return -1;` — nothing else in the
program). Generated mcode:
```
||
    $st ($i32) [$r28 + 0] $r2     // store gi+1
    $ld ($i32) $r3 [$r28 + 0]     // reload gi — SAME bundle, SAME address
    $set $r4 0 34465
;
```
**The store and the reload of the same address are packed into the same VLIW bundle.** Per the
ISA, instructions in one bundle execute in parallel — the load does not see the store that's
"simultaneously" in flight, so it reads the stale pre-increment value. Confirmed by hardware run:
r1 = -1 (the failure branch), proving the reload got the old value. This is a **missing
memory-aliasing hazard check in `bundler.py`** — it tracks register RAW/WAW/WAR hazards (per
earlier audit) but evidently not "don't bundle a load with a same-address store still in
flight." This is a pre-existing bundler bug, **not introduced by today's i32 work** — it would
equally affect any `($i64)` store-then-reload of the same variable; today's i32 test just happened
to trigger it. Store→load-same-address is an extremely common pattern (any "increment a global
and check it" idiom), so this is the prime suspect for most of the other 8 wrong-value failures
above too, though each hasn't been individually traced to this same mechanism yet.

**`test_subword` detail**: checks 1-11 (char locals/arrays/globals, short locals/arrays/globals,
int locals/arrays) all passed — only check #12 (the global `int` scalar increment-and-reread,
the exact bundler-hazard pattern above) failed. This means **the core i8/i16/i32 sub-word
load/store feature itself is solid** — confirmed independently by `test_subword_i8.c` and
`test_subword_i16.c` (char-only and short-only, no other variables in the frame) both returning
r1=1 (full pass) on hardware. The one combined-test failure is the bundler hazard, not the
sub-word feature.

### Bottom line
i8/i16/i32 sub-word load/store: **hardware-confirmed working** in isolation
(`test_subword_i8`, `test_subword_i16`) and via `test_array`/`test_cmov`/`test_branch`/
`test_pointer` exercising `$i32` in other shapes. The failures above are four separate
pre-existing issues (XNOR mnemonic typo, `$pack` grammar mismatch, aligner assertion crash,
bundler memory hazard) uncovered by this regression, none caused by today's work. **None of these
four have been fixed yet** — flagged for explicit direction on priority/approach before touching
any of them, given the XNOR fix is a confident one-liner but the other three need either your
grammar knowledge or non-trivial bundler work.

---

## 2026-06-17 — Sub-word load/store implemented: i32,i32 / i32,i16 / i8,i8

### Why now
The `$ld ($i32)` engine bug (always read bits[63:32] regardless of byte offset — see the
"Vector support" entry below and [[apara_dmem_alignment]] memory) is fixed in the upstream VM.
i4/u4 confirmed by the user to have **no LOAD/STORE form at all** — minimum memory transfer
granularity is `$i8`; i4 is arithmetic-only. Not implemented, not attempted.

### What changed
1. **Audited every `IRLoad`/`IRStore`/`IRGlobalLoad`/`IRGlobalStore`/`IRGlobalDecl` construction
   site in `ir_gen.py`** (~20 sites). About half relied on the `elem_bytes=4` constructor default
   in `ir.py` even for values that are actually 8 bytes (long long/double/pointer values, struct
   fields, generic local scalar load/store, pointer dereference, 2D-array base-pointer loads) —
   harmless only because `_atype()` in `codegen.py` ignored `elem_bytes` and always emitted
   `($i64)`. Every site now passes the correct width explicitly:
   - Plain scalars (locals + params): new `_local_elem_bytes` dict in `ir_gen.py`, populated by
     `_alloc_local`, looked up by `_load_var`/`_store_var`.
   - Struct fields: `fdmem` from `_structref_base_and_total_off` (always 8 for scalar leaf fields).
   - Pointer dereference (`*p`) and pointer-value loads: hardcoded `8` — pointers are still
     stride=8 universally (see `_record_ptr`); pointer-to-narrow-type is a separate, NOT-yet-done
     feature.
   - Array/pointer indexing fallback paths: reuse the existing `_get_esz()` helper.
2. **Removed the `elem_bytes=4` default in `ir.py`** — now a required (or keyword-only, for
   `IRGlobalLoad` where `offset=None` already occupies the "has a default" slot) argument, so any
   future missed call site throws immediately instead of silently defaulting to 4.
3. **Regression checkpoint**: recompiled all 18 existing tests after steps 1-2 — mcode byte-for-byte
   identical to pre-change baseline (expected: `_atype` still hardcoded `($i64)` at this point).
4. **`_atype()` now maps elem_bytes → type tag**: `1→($i8)`, `2→($i16)`, `4→($i32)`, `8→($i64)`
   (was: always `($i64)`).
5. **New test**: `new_isa_tests/test_subword.c` — char/short/int locals+globals+arrays, all three
   pairs in one file, Python/IR/mcode-level verified only (see below). Generated mcode confirmed
   correct: `$i8` for all char access, `$i16` for all short, `$i32` for all int, struct
   fields/pointers/prologue·epilogue still `$i64`.

### IMPORTANT — hardware verification is partially blocked, READ BEFORE RUNNING ANYTHING
The user's local `engine_isp` checkout **still has the old $i32 sub-word bug** — the VM fix
hasn't been pulled yet there. Consequences:
- `new_isa_tests/test_subword.c` mixes all three widths — **do not hardware-run this one yet**
  (its i32 section would hit the still-present bug).
- Split out **`new_isa_tests/test_subword_i8.c`** and **`new_isa_tests/test_subword_i16.c`** —
  each deliberately contains zero `int`/`short`-the-other-one locals/globals, confirmed via grep
  that their generated mcode contains only `($i8)`/`($i64)` and `($i16)`/`($i64)` respectively,
  no `($i32)` anywhere. **These are safe to hardware-verify right now.**
- **12 of the 18 pre-existing tests now also emit `$i32`** (because they use `int` somewhere) and
  are therefore now ALSO affected by the still-present bug, not just the new test:
  `test_array, test_cast, test_cmov, test_dot, test_fsqrt, test_logic, test_pack, test_pointer,
  test_scalar_full, test_slice, test_vadd, test_vreduce`. **Do not hardware-verify these against
  the current unpatched engine_isp — they would now fail where they previously passed**, purely
  because `int` access changed from `($i64)` to `($i32)`, not because of a new compiler bug.
  Safe/unaffected (no `int` anywhere, zero mcode diff from before this change):
  `test_alu, test_2d, test_struct, test_branch, test_ldst, test_spill`.
- **Once the updated `engine_isp` is pulled**: re-run the full 18-test + 3-new-test suite on
  hardware as the real confirmation. Until then, only Python/IR/mcode-level verification has been
  done for i32.

---

## 2026-06-17 — Vector support verified + by-value arg-passing bug fixed

### Vectors: ISA-level codegen confirmed correct
Cross-checked `_gen_IRVecArith`, `_gen_IRVecDot`, `_gen_IRVecReduce`, `_gen_IRPack` in `codegen.py`
against `AparaReference.pdf` §5.3-5.5, §8.4. Operand order/semantics match exactly:
`$v <op> <rd> (<type>) <rs1> <rs2> [$replicate]`, `$dot <rd> (<type>) <rs1> <rs2> [$accumulate]`,
`$vreduce <rd> (<type>) <rs1>`, `$pack <rd> <result_nbits> <src_nbits> <rs2>`. Confirmed by
compiling `__vadd_vi32`/`__dot_vi4`/`__vreduce_vi4`/`__pack` intrinsic calls through the Python
pipeline (mcode text only, no assembler invoked) — generated mcode lines match ISA syntax exactly.

**Caveat — only the "manually packed 64-bit register" style is supported.** The ISA sample
program (ISA doc Ch.9) loads true 256-bit/128-bit vectors directly from memory via
`$ld ($u256)`/`$ld ($u128)` (4 or 2 registers at once). `grep -n "u256\|u128"` across
codegen.py/ir_gen.py/ir.py returns nothing — there is no wide-vector load/store support.
`_atype()` in codegen.py always returns `($i64)` regardless of `elem_bytes`. So vector ops here
only work on values a C program has already packed into a single 64-bit register (via `__pack` or
manual shifts), not on real array data loaded in bulk. This is a real gap vs. the ISA's vector
capability, not yet attempted.

### Bug found + fixed: by-value call args silently passed by address for char/short/long long/double
**Symptom:** compiling `long long add_ll(long long a, long long b) { return a + b; }` and calling
it with local `long long` arguments produced IR that passed the arguments' **stack addresses**
into the call, never loading their values — e.g. `_t13 = add_ll(_t11, _t12)` where `_t11`/`_t12`
were `&stack[FP-offset]`, not loaded values. `int`-typed calls were unaffected.

**Root cause:** `ir_gen.py`'s `_elem_size(node)` defaulted to `4` for any non-array, non-pointer
scalar type — wrong for `long long`/`double` (8 bytes) and `char`/`short` (1/2 bytes). `int`/
`float` happened to be 4 bytes already, so they never tripped the bug. `_alloc_local` then saw
`elem_bytes(4) != total_bytes(8 or other)` and concluded "must be an array," registering the
variable in `_array_elem`. `_call`'s argument-building loop checks `_array_elem` to decide
"raw array → pass address of first element instead of value" — which silently misfired for any
`long long`/`double`/`char`/`short` local or parameter passed by value into a call. This is how
the vector intrinsics (which pass `long long`-packed values across calls) were first noticed to
be broken — the bug is general-purpose, not vector-specific.

**Fix** (one line):
```python
def _elem_size(node):
    if isinstance(node, A.ArrayDecl): return _type_size(node.type)
    if isinstance(node, A.PtrDecl):   return 8
    return _type_size(node)   # was: return 4
```
Traced every call site (global/local alloc, struct/2D-array overrides which run after and
override anyway, array-param decay) — only scalar `char`/`short`/`long long`/`double` behavior
changes; arrays, pointers, structs, `int`/`float` are unaffected. Also incidentally fixes
over-allocation of uninitialized global scalars of these types (was allocating 2x DMEM).

**Verification:**
- `add_ll(l1, l2)` now correctly loads values before the call.
- Vector intrinsic test (`__pack`/`__vadd_vi32`/`__dot_vi4`/`__vreduce_vi4` via wrapper functions)
  now produces correct IR and matching mcode.
- Regression smoke test covering 1D int array param + loop, pointer deref/write, struct field
  read/write, `char`/`short`/`double` locals — all still IR-correct after the fix.
- All checks done via the Python text-generation pipeline only; **not yet run on hardware.**

### Known pre-existing limitation (not fixed, not today's scope)
Float/double constant literals (e.g. `double d = 2.5;`) are not parsed — `_visit_expr` for
`A.Constant` tries `int(raw, 0)` and falls back to `Const(0)` on failure, silently zeroing any
non-integer literal. Unrelated to the bug above; flagging for whenever float support is tackled.

---

## 2026-06-17 — Function Pointers: BLOCKED at assembler level

### Status: PAUSED — not a compiler bug, cannot be fixed in compiler.py/ir.py/ir_gen.py/codegen.py

### What already exists (from earlier work, found while investigating)
`ir.py`, `ir_gen.py`, and `codegen.py` already had substantial function-pointer scaffolding in place:
- `IRFuncAddr` (dest = address of named function) and `IRIndirectCall` (dest = call through a
  register) IR nodes — `ir.py:199-210`.
- `_func_names` pre-collected from all `FuncDef`s so forward references resolve; `&funcname`,
  `fp = funcname`, `fp(args)`, `(*fp)(args)` all correctly emit `IRFuncAddr`/`IRIndirectCall` —
  `ir_gen.py` (`_load_var`, `_unary '&'`, `_call`).
- `_gen_IRFuncAddr` / `_gen_IRIndirectCall` codegen, fully spill/caller-save aware, mirroring the
  direct-call path — `codegen.py:807-877`.

### The blocker
Cross-checked against `AparaReference.pdf` (ISA doc) and the assembler's own parser grammar:

1. **`$call $rN` (register-indirect call) is valid hardware/ISA behavior.** ISA §6.2: "the target
   address is in the bottom 32-bits of the specified register." So `_gen_IRIndirectCall`'s
   `$call {RET}` is correct — this half of the feature works.
2. **There is no instruction that can load a function's absolute address into a register.**
   `$call <label>` is a 25-bit **PC-relative offset**, not an absolute address, so it can't be
   repurposed. The only other candidate, `$set`, was checked directly against the assembler's
   parser source: its immediate field grammar only accepts `UINTEGER` / `NINTEGER` / `HEXADECIMAL`
   tokens — a label name in that position throws `NoViableAltException` (hard parse failure).
   The assembler has **no label-resolution mechanism** outside of control-transfer instructions.

### Conclusion
`_gen_IRFuncAddr`'s current `$set {dest} 0 {ir.func_name}` will hard-crash `mcode_assemble`.
Function pointers cannot be implemented until the assembler gains some way to materialize a
label's absolute address into a general-purpose register (new instruction, new `$set` grammar
rule, or a post-assembly relocation/patch mechanism). This is outside the Python compiler's
control. **Do not re-attempt without first confirming the assembler side has changed.**
`IRIndirectCall` codegen is sound as-is and can be reused immediately once `IRFuncAddr` has a
working implementation.

---

## 2026-06-17 — Register Spilling

### Done today — register spilling when >28 temps are simultaneously live

#### Problem
APARA has only 32 physical registers. The compiler uses a pool of 28 (`$r1`–`$r25`, `$r29`–`$r31`).
If a C expression keeps more than 28 temps simultaneously live, the old code raised `RuntimeError`.

#### Trigger pattern
Right-nested function calls force spilling: `f01() + (f02() + (f03() + ... + f30()))`.  
`ir_gen` visits LEFT before RIGHT, so `_t1` (result of f01) stays live in a register while ALL
inner calls are evaluated. By the time f29() is being processed, `_t1`..`_t28` are all live
simultaneously — 28 registers full — and f29()'s return value needs a 29th. Spill fires.

#### Implementation (codegen.py v6)

| Component | Description |
|-----------|-------------|
| `MAX_SPILL_SLOTS = 64` | 64 × 8 = 512 bytes reserved in every function frame |
| `SPILL_RESERVE = 512` | Added to `fs = ir.frame_size + CALLER_SAVE_BYTES + SPILL_RESERVE` |
| `_spill_map` | `{temp_name → FP_offset}` — which temps are currently evicted to RAM |
| `_spill_counter` | monotonically assigns spill slot indices (reused across calls via `_get_spill_slot`) |
| `_get_spill_slot(name)` | allocates slot `-(frame + CALLER_SAVE_BYTES + 8 + idx*8)` on first call |
| `_spill_evict(protect)` | picks unprotected live temp, emits `$st [FP+slot] reg`, frees register |
| `_alloc_reg(temp, protect)` | unified: fast-path if in reg; reload-from-spill if evicted; fresh-allocate otherwise |
| `_safe_borrow(protect)` | spills before borrow if pool empty |

#### Key bugs found and fixed

1. **`_alloc_reg` spill reload**: the "previously spilled" branch unconditionally called
   `_spill_evict()` even when the pool had free slots.  With only 2 live temps and all in
   the protect set this caused a deadlock.  **Fix**: guard with `if not self._ra.has_free()`.

2. **Post-call stale register**: after `$call`, callee may clobber pool registers.  Cannot
   call `_spill_evict()` at that point — it would store the wrong (clobbered) value.
   **Fix**: when pool is full after the call, load the victim's CORRECT value from its
   caller-save slot, then write to spill, before freeing the register.

#### Spill slot layout (FP-relative)

```
FP - 0           prologue save of old FP  ($r26)
FP - 8..(-8-n*8)  local vars from ir_gen  (frame_size bytes)
FP - (fs+8)..(fs+224)   caller-save area  (28 × 8 = 224 bytes)
FP - (fs+232)..(fs+743)  spill area        (64 × 8 = 512 bytes)
```

#### Verification — test_spill.c

30 functions f01..f30 each return their index. `main` computes the sum via right-nested
addition (see `new_isa_tests/test_spill.c`).  Hardware-expected result: `r1 = 465 = 0x1d1`.

Spill instructions confirmed in generated mcode at offsets `[$r26 + -304]`, `-312`, `-320`
(beyond the caller-save area that ends at `-296`).

All 18 tests pass after the fix.

---

## 2026-06-17 — Struct Member Access (Latest)

### Done today — structs

| Feature | Status | Notes |
|---|---|---|
| `struct Foo { ... };` standalone definition | Done | pycparser: `Decl(name=None, type=Struct(...))` |
| `struct Foo var;` local + global | Done | 8 bytes per field (APARA alignment) |
| `var.field` read | Done | `IRLoad(base_addr, field_offset)` |
| `var.field` write | Done | `IRStore(base_addr, field_offset, val)` |
| `ptr->field` read | Done | pointer value is the base |
| `ptr->field` write | Done | same |
| `typedef struct {...} Name;` | Done | anonymous struct gets typedef name |
| Nested struct: `struct A { struct B b; }` | Done | field offsets accumulate recursively |
| Chained access: `ptr->outer.inner` | Done | recursive `_structref_base_and_total_off` |
| `&s.field`, `&p->field` | Done | address = base + field_offset |
| `struct Foo *p` param | Done | pointer-to-struct correctly 8 bytes |
| Struct initializer: `s.x = ...; s.y = ...;` | Done | field-by-field assignment |

Fixes required along the way:
- Standalone struct `Decl(name=None, type=Struct(...))` was previously ignored (type=Struct, NOT TypeDecl(Struct))
- `_elem_size(PtrDecl)` was returning pointed-to struct size (40 bytes for `struct Line *`) — changed to always return 8
- `_record_struct_var` not called in param loop — added

Test: `array/test_struct.c` — 8 sub-tests: local/global field access, pointer (`->`), pass-by-pointer, nested struct, chained access. All compile clean.

---

## 2026-06-17 — 2D Arrays

### Done today — 2D array support

| Feature | Status | Notes |
|---|---|---|
| `type mat[R][C]` global declaration | Done | rows×cols×8 bytes in DMEM |
| `type mat[R][C]` local (stack) | Done | same layout on frame |
| `mat[i][j]` read | Done | offset = i×(C×8) + j×8 |
| `mat[i][j]` write | Done | same offset |
| 2D array param `f(type A[R][C])` | Done | decays to pointer; inner dims tracked |
| Array name decay in call args | Done | name alone → passes base address |
| Verified: `gMat[1][2]`→offset 40 (3×3) | ✓ | 1×24 + 2×8 = 40 |
| Verified: `loc[1][1]`→offset 40 (2×4 local) | ✓ | 1×32 + 1×8 = 40 |
| matmul2 test (2×2 triple-nested loop) | Compiles ✓ | hardware run pending |

Test: `array/test_2d.c` — 6 sub-tests covering global/local 2D write/read, row-major isolation, 2×2 matmul, read/write via function param.

---

## 2026-06-17 — Pointer Support

### Done — pointer arithmetic

| Feature | Status | Notes |
|---|---|---|
| `long long *p` declaration | Done | PtrDecl detected, stride=8 recorded |
| `p = &x` pointer to local | Done | IRLoadAddr gives address of local |
| `p = &arr[i]` pointer to array element | Done | base + i*stride computed |
| `*p` dereference read | Done | IRLoad from pointer value |
| `*p = val` dereference write | Done | IRStore through pointer |
| `p + n`, `p - n` | Done | n scaled by stride=8 before add |
| `p++`, `p--`, `++p`, `--p` | Done | increment/decrement by stride=8 |
| `p += n`, `p -= n` | Done | n scaled by stride=8 |
| `p[i]` pointer indexing | Done | loads pointer value, uses as base |
| `&arr[i]` address of element | Done | `_unary '&'` now handles ArrayRef |

All 15 tests pass (14 prior + test_pointer.c with 10 sub-tests).

---

## 2026-06-17 — 28-register allocator

### Done today

#### 1. Full 28-Register Dynamic Allocator (v5)

**Before**: 11 registers permanently wasted as fixed/scratch.
**After**: only 2 registers are fixed forever — `r0` (hardware ZERO) and `r28` (GBASE).

| Register | Before | After |
|----------|--------|-------|
| r0 | ZERO (fixed) | ZERO (fixed — hardware) |
| r1 | RET (fixed) | **Pool** — recycled between calls |
| r2–r5 | ARG (fixed) | **Pool** — recycled between call sites |
| r6–r25 | GEN pool (20) | **Pool** (same) |
| r26 | FP (fixed) | Reserved per-function (frame pointer) |
| r27 | SP (fixed) | Reserved per-function (stack pointer) |
| r28 | GBASE (fixed) | GBASE (fixed — global base address) |
| r29 | ONE=1 (fixed) | **Pool** — eliminated, no longer needed |
| r30 | SCR (fixed) | **Pool** — borrowed dynamically as scratch |
| r31 | SCIDX (fixed) | **Pool** — borrowed dynamically as scratch |

**Pool size**: 28 registers (`$r1`–`$r25`, `$r29`–`$r31`).

#### 2. Key Algorithmic Changes

- **Unconditional jump**: was `? $r29 > $goto label` (requires ONE=1).
  Now: `? ($i64) $r0 == $goto label` (0==0 is always true — no dedicated register needed).
- **Scratch registers**: `borrow()` / `unborrow()` dynamically pop/push from the free pool
  for each intermediate computation (address computation, subtraction for compare, etc.).
- **`$pack` consecutive pair**: `borrow_pair()` scans the free list for any two
  physically consecutive register numbers at emit time — works with all 28 pool regs.
- **Call site aliasing fix**: arguments always read from the saved stack slots (not live regs)
  before being written to `r2–r5`, preventing register-aliasing bugs.
- **Return value capture order**: `r1` (return value) copied to `dest` BEFORE restoring
  saved registers, because restoring may clobber `r1` if it held a live temp.
- **Always-on preprocessing**: `gcc -E -P` now runs unconditionally, stripping comments
  and `#define`/`#include` — no longer requires `--preprocess` flag.

#### 3. Verification

All 14 test programs compile and produce correct results:

| Program | Status | Bundle reduction |
|---------|--------|-----------------|
| test_alu | [OK] | 48% |
| test_branch | [OK] | 30% |
| test_array | [OK] | 36% |
| test_ldst | [OK] | 50% |
| test_cast | [OK] | 48% |
| test_cmov | [OK] | 53% |
| test_dot | [OK] | 55% |
| test_fsqrt | [OK] | 54% |
| test_logic | [OK] | 44% |
| test_pack | [OK] | 56% |
| test_scalar_full | [OK] | 39% |
| test_slice | [OK] | 54% |
| test_vadd | [OK] | 55% |
| test_vreduce | [OK] | 56% |

Register proof: `test_scalar_full` uses **all 32 registers** (`$r0`–`$r31`).

---

## 2026-06-16

### Done
- Expanded GEN register pool from 17 → 20 registers (r6–r25)
- Fixed constant-vs-constant comparison bug in `_emit_cond_branch`
- Compiled and verified comprehensive scalar test (`test_scalar_full.c`)

### Register layout (at that time — now superseded by v5 above)
```
r6–r25 = GEN (20)  r29=ONE  r30=SCR  r31=SCIDX
```

---

## 2026-06-15

### Done
- Implemented ALL missing ISA instructions (9 new IR node types + codegen):
  - `$nop`, `~|` NOR, `~&` NAND, `~~` XNOR
  - `$fsqrt`, `$cmov`, `$slice`, `$pack`, `$cast`
  - `$v +/-/*` vector arithmetic, `$dot/$dot $accumulate`, `$vreduce`
- Fixed 3 bugs in `ir_gen.py`:
  - Hex literal `0x0F` stripping 'F' → parsed as 0
  - Function declarations creating false DMEM globals
  - Standalone call statements not dispatching to `_call()` handler
- Created 10 test programs in `new_isa_tests/`; all produce correct mcode

---

## 2026-06-14 (earlier)

### Done
- Verified load/store on hardware: `test_ldst.c` — all 5 PostConditions passed
- Verified all 6 branch comparisons on hardware: `test_branch.c` — all pass
- Implemented VLIW bundle optimizer (`bundler.py`) — RAW/WAW hazard detection,
  greedy packing up to 8 instructions/bundle
- Hardware verified bundled mcode — ALU, LDST, branch, array all pass

### Bundle reduction results (hardware verified)
| Program | Before | After | Reduction | Hardware |
|---|---|---|---|---|
| test_alu | 82 | 41 | 50% | All 11 PostConditions ✓ |
| test_ldst | 52 | 25 | 51% | All 6 PostConditions ✓ |
| test_branch | 106 | 76 | 28% | r1=0x1 correct ✓ |
| test_array | 55 | 34 | 38% | r1=0x96 correct ✓ |

---

## ISA Instruction Coverage (100% opcodes)

| # | Instruction | How exposed in C |
|---|-------------|-----------------|
| 1–4 | `+ - * /` | `a+b`, `a-b`, `a*b`, `a/b` |
| 5 | `\|` | `a\|b` |
| 6 | `&` | `a&b` |
| 7 | `^` | `a^b` |
| 8 | `~\|` | `__nor(a,b)` |
| 9 | `~&` | `__nand(a,b)` |
| 10 | `~~` | `__xnor(a,b)` |
| 11 | `<<` | `a<<n` |
| 12 | `>>` | `a>>n` |
| 13 | `$fsqrt` | `__fsqrt_f32(x)` etc. |
| 14–16 | `$v +/-/*` | `__vadd/vsub/vmul_vi32()` etc. |
| 17 | `$dot` | `__dot_vi16(a,b)` |
| 18 | `$dot $accumulate` | `__dot_acc_vi16(acc,a,b)` |
| 19 | `$vreduce` | `__vreduce_vi32(v)` |
| 20–25 | `? ==,!=,>,>=,<,<=` | if/while/for conditions |
| 26 | `$call` | function call |
| 27 | `$return` | return statement |
| 28 | `$cmov` | `__cmov_gt/lt/eq/ge/le/ne(check,t,f)` |
| 29 | `$ld` | variable/array read |
| 30 | `$st` | variable/array write |
| 31 | `$set` | large constant loading |
| 32 | `$slice` | `__slice(val, hi, lo)` |
| 33 | `$cast` | `(int8_t)x`, `(int16_t)x` etc. |
| 34 | `$pack` | `__pack(a, b, rbits, sbits)` |
| 35 | `$nop` | `__nop()` |
| 36 | `$null` | bundle padding (internal) |
| 37 | `$halt` | `halt()` or program end |

---

## Compiler feature status

| Feature | Status | Notes |
|---|---|---|
| C parsing (always preprocessed via gcc -E) | Done | No flag needed |
| AST → Three-Address IR | Done | All C operators, all control flow |
| IR → APARA mcode | Done | |
| Register allocation | **Done — 28 regs, fully dynamic** | All 32 registers used |
| Global variables | Done | Hardware verified |
| ALU (12 ops: + - * / % & \| ^ ~ << >> + synthetic %) | Done | Hardware verified |
| NOR / NAND / XNOR | Done | via intrinsics |
| Load / Store ($i64) | Done | Hardware verified |
| If/else, all 6 comparisons | Done | Hardware verified |
| While / for / do-while loops | Done | Hardware verified |
| Switch / case / break | Done | |
| Compound assignments (+=, -=, *=, etc.) | Done | |
| Ternary operator ?: | Done | |
| Logical &&, \|\|, ! | Done | |
| Pre/post increment ++ / -- | Done | |
| 1D arrays (global + local) | Done | Hardware verified |
| Local variables (stack frame) | Done | Hardware verified |
| Function calls (up to 4 args) | Done | |
| Multiple functions | Done | |
| Recursion | Done | |
| data.map generation | Done | |
| result file generation | Done | |
| run.sh generation | Done | |
| VLIW bundling optimizer | Done | Hardware verified, 30–56% reduction |
| $fsqrt (f4/f8/f16/f32/f64) | Done | via intrinsics |
| $cmov (all 6 conditions) | Done | via intrinsics |
| $slice | Done | via intrinsic |
| $cast (scalar int/float) | Done | via C cast syntax |
| $pack (dynamic consecutive pair) | Done | via intrinsic |
| $v +/-/* (vector arithmetic) | Done | via intrinsics |
| $dot / $dot $accumulate | Done | via intrinsics |
| $vreduce | Done | via intrinsics |
| Const-vs-const comparison folding | Done | |
| Sub-word LD/ST ($i32,$i16,$i8) | Blocked | Engine hardware bug |
| Register spilling (>28 live vars) | **Done** | 64-slot spill area; hardware-pending |
| Struct member access (`s.x`, `p->x`, nested) | **Done** | 8B/field, recursive chain |
| Function pointers | **Done** | compile-time linker pass (2026-07-18); fn01–04 vs gcc |
| Pointer arithmetic (all ops) | **Done** | stride=8 APARA alignment |
| 2D arrays (global + local + params) | **Done** | row-major, array decay |
| Float arithmetic (+,-,*,/) | **Done** | f32+f64 arith/cmp/casts/vars/arrays/params, fp01–07 vs gcc |
| String literals | Partial | address-of only |
| Variadic functions | **Done** | stack-passed extras + __va_start intrinsic (2026-07-18); va01–03 vs gcc |

## Remaining work (priority order)

| # | Feature | Effort | Blocker |
|---|---------|--------|---------|
| 1 | **Register spilling** (>28 live vars) | Done ✓ | — |
| 2 | **Function pointers** | Done ✓ | compile-time linker pass, 2026-07-18 |
| 3 | **Float arithmetic** (+,-,*,/) | Done ✓ | full scalar f32/f64 subset, 2026-07-18 |
| 4 | **Sub-word LD/ST** ($i32/$i16/$i8) | Low | **Hardware engine bug** |
| 5 | **Variadic functions** | Done ✓ | stack-passed extras, 2026-07-18 |
| 6 | **>4 named args** | Done ✓ | unified with variadic stack-passing, 2026-07-18 |
| — | Real strings | Dropped | not needed for the APARA accelerator (2026-07-18) |

**Overall compiler completeness: ~96% of a basic C compiler** (remaining: only deliberately-dropped items)

---

## Directory structure

```
cmp_wd/
├── compiler/               ← compiler source
│   ├── compiler.py        ← entry point + preprocessing + data.map + result file
│   ├── ir.py              ← 37 IR node class definitions
│   ├── ir_gen.py          ← C AST → Three-Address IR (pycparser NodeVisitor)
│   ├── codegen.py         ← IR → APARA mcode  (v6: 28-reg dynamic allocator + spilling)
│   ├── bundler.py         ← VLIW bundle optimizer (RAW/WAW hazard detection)
│   └── STATUS.md          ← this file
├── alu/                    ← test_alu (hardware ✓)
├── array/                  ← test_array (hardware ✓)
├── branch/                 ← test_branch (hardware ✓)
├── ldst/                   ← test_ldst (hardware ✓)
└── new_isa_tests/          ← 14 ISA instruction tests (all compile ✓)
```

---

## Optimization pipeline & roadmap (updated 2026-07-21)

Work on branch `optimization-infrastructure` (main left pristine). Goal: a
general-purpose optimizer built on **reusable shared analyses**, not
benchmark-specific passes.

### Current pipeline order

```
IVSR → strength-reduce → LICM → loop_reg → [CopyProp → Global DCE] → scheduling → bundling
                                            └── next milestone ──┘
```
Each IR transform runs on pristine `ir_gen.instructions`, builds fresh IR
objects (verification IR stays untouched), and is tiered with a no-spill
fallback in `compiler.py`. A/B knobs: `APARA_NO_IVSR`, `APARA_NO_STRENGTH_REDUCE`,
`APARA_NO_LOOPOPT` (and planned `APARA_NO_COPYPROP`); debug `APARA_IVSR_DEBUG`.

### Done (committed 6617883)

- **`parallelism_profile.py`** — read-only per-loop ILP profiler: N (instrs/iter),
  B (bundles/iter = achieved II), critical path (current vs after-renaming),
  RecMII (true loop-carried recurrence, register vs stack-slot tagged), ResMII
  (lane floor + driver: mem-lanes / 8-wide / div), MII, B/MII headroom, register
  free-count. Corpus `--rank` mode. Drives every decision below.
- **`strength_reduce.py`** — power-of-two SR: `*2^n→<<`, and (only when unsigned)
  `/2^n→>>`, `%2^n→&`. Signed div/mod correctly refused. *Measured throughput-
  marginal on this ISA* (mul and shift are equal-cost ALU ops); kept as hygiene.
- **`ivsr.py`** — induction-variable / pointer strength reduction. Maintains each
  `base + invariant + iv*stride` address in one incrementing pointer (preheader
  init + `p += step`); function-scoped dead-temp elimination removes the orphaned
  arithmetic. Single-def-map tracing; reuses loop_reg-style clean-slot/escape
  analysis; profitability gate protects small loops. **Verified: matmul inner
  loop N 27→17; executed instructions −32.7% (12×12), −35.3% (16×16), gcc-golden
  PASS.** Zero regressions (43/45 verifiable tests; 2 pre-existing failures).

### Profiler-driven bottleneck analysis (post-IVSR)

- Dominant remaining cost is **compiler-generated add-zero copies**
  (`+ rd ($i64) $r0 rs`, emitted by `codegen._gen_IRAssign` for every temp copy,
  never coalesced). **5 of 17 instrs (29%) in the matmul hot loop**; 22/175
  program-wide (mm12); 46/486 (conv). Sources: loop_reg promotion moves +
  value-copy IRAssigns.
- These copies **inflate recurrences**: matmul hot loop shows RecMII=3 but the
  true recurrences (acc+=prod, k+=1, pointer+=stride) are each RecMII=**1** — so
  it is only *falsely* recurrence-bound. Conv inner loop RecMII=10 is a genuine
  **float** accumulator (not safely breakable → needs `$dot` vectorization).
- Conclusion: the next pass is **not** loop unrolling (matmul falsely
  recurrence-bound, conv truly float-recurrence-bound). It is a universal
  cleanup: copy propagation + coalescing + global DCE.

### Next pass — Copy Propagation + Copy Coalescing + Global DCE (planned)

General, IR-analysis-based, architecture-independent; benefits arithmetic, DSP,
image, graph, networking, string, sorting, crypto, ML code alike.

Files: **NEW** `ir_analysis.py` (first shared-analysis module), **NEW**
`copyprop.py`; **MOD** `ivsr.py` (consume the shared module), `compiler.py`
(pipeline wiring). Placement: after loop_reg (the copy producer), before
scheduling, via a `finish(ir)=global_dce(copyprop(ir))` wrapper on every tier.

Algorithm (all guarded by `DefUse`; bail on any ambiguity):
- **Forward copy propagation.** Rule A (whole-function): both `dst`,`src`
  single-def → replace uses of `dst` with `src`. Rule B (intra-block): `dst`
  single-def, `src` multi-def (e.g. loop_reg's loop-carried vreg) → replace
  same-block uses with no intervening redef of `src`.
- **Copy coalescing (backward).** `dst=src` with `src` single-def and sole-use →
  retarget `src`'s defining instruction to write `dst`, delete the copy.
- **Global DCE.** Promote the function-scoped dead-temp elimination out of
  `ivsr.py` into the shared module; run after copy-prop (and every pass that can
  create dead code).

Worked example (accumulator): `r12=0+r5; r13=r12+r10; r5=0+r13` →
`r5 = r5 + r10` (one instruction; recurrence 3→1).

Correctness: value-preserving renames guarded by single-def / no-intervening-
redef; DCE removes only pure, unread instructions (loads are pure in this
fault-free VM); conservative bail on doubt; final safety net is the existing
no-spill tier fallback.

### Shared analysis framework — incremental plan

Introduced **now** in `ir_analysis.py` (per-function-slice scoped, so the
recurring cross-function temp-name-collision bug is structurally impossible):

- leaf helpers `dest_names` / `src_names` / `jump_targets` / `func_slices` /
  `enclosing_func` (today duplicated across licm / loop_reg / ivsr),
- **`DefUse`** — single-definition map + multi-def set + **def-use chains**
  (first reusable module; shared immediately by copy-prop and IVSR),
- **`basic_blocks`** — straight-line partition only (no edges),
- **global DCE**.

Postponed to later milestones (with reason):

| Analysis | Deferred because |
|---|---|
| Full CFG (edges) | not needed for intra-block / single-def rules; required for SCCP reachability + GVN availability |
| Dominator / post-dominator trees | needed for cross-block prop, SCCP, GVN, safe motion; copy-prop's conservative rules avoid it |
| Loop tree / nest info | back-edges detected ad hoc today; unify for unroll / pipeline / interchange |
| Shared liveness | codegen's own suffices now |
| Alias analysis | licm's storage-class version suffices; GVN load-elim will need a shared one |

Future passes reuse the same framework without rewrite: **SCCP** (DefUse + future
CFG), **GVN** (DefUse + basic_blocks + future dominance), **loop unswitching /
unrolling / software pipelining** (func_slices + future loop tree). The framework
grows by addition, not rewrite. Design philosophy: optimize the compiler
architecture, not the current benchmark corpus.

### Post-implementation metrics to report (on/off, whole corpus)

Full regression suite (pass/fail; new vs pre-existing failures), instruction
count (N), bundle count (B), register pressure (peak/free), spill count,
executed-instruction count — both improvements and regressions, small loops
watched.

---

## R2.1 — Reusable IR Dependence Graph Infrastructure  (2026-07-25) ✅ DONE

**Analysis-only.** No IR / assembly / bundle / tier-selection change; bundler,
`LoopUnroll`, and every pass frozen. Full report: `loopopt/R2_1_DELIVERY.md`.

- **Added** `loopopt/depgraph.py` — `DependenceGraph` (+`DepNode`/`DepEdge`) over
  IR instructions, per function slice. Register RAW/WAR/WAW; memory RAW/WAR/WAW
  via the **M2** `_access_key` classifier + `AliasSummary.may_alias` oracle;
  minimal control ordering (terminator pinned last); loop-carried **recurrence**
  edges via **LoopInfo**, held separately (`carried` flag + loop header) from
  intra-iteration edges. API: nodes/edges, pred/succ (filterable), Tarjan
  `sccs()`, `recurrences()`, `topo_order()`/`is_acyclic()`, `validate()`,
  `dump()`, `to_dot()`; `build_function_graphs()` for whole modules.
- **Reused (no duplication):** `ir_utils` primitives, `DefUse`, `CFG`,
  `Dominators`, `LoopInfo`, and M2 `analysis_mem` (`_access_key`, `AliasSummary`).
- **Modified:** `loopopt/__init__.py` — additive export only.
- **may-precede fix (soundness):** a single-block loop body is on a CFG cycle, so
  same-block pairs `i>j` may precede around the back edge — required to capture
  accumulator/IV recurrences internal to one block (e.g. `s += p[i]`).
- **Validation:** `_r2_1_test.py` 40/40; `depgraph_corpus.py` 124 progs / 194
  functions → 194 graphs, 0 build errors, 0 validate failures, **0 IR mutations**,
  124/124 compile+bundle (23782 edges: reg 6156 / mem 13019 / ctl 4607; 2544
  carried; 81 recurrences); `pipeline_crosscheck.py` 124/124 identical per-tier
  IR + code + selected tier, 0 rollbacks, **both** LICM gate states.
- **Not done (by design):** not wired into any pass; no scheduling / list
  scheduling / SWP / modulo scheduling; alias precision left conservative
  (computed = alias-all, calls = full barrier) for R2.2 disambiguation to sharpen.

---

## R2.2 — Memory Dependence Disambiguation  (2026-07-25) ✅ DONE

**Analysis-only, precision-only.** No IR / assembly / bundle / tier / behaviour
change; DependenceGraph, LoopTransform, bundler, LoopUnroll frozen. Full report:
`loopopt/R2_2_DELIVERY.md`.

- **Added** `loopopt/depgraph_disambig.py` — `MemoryDisambiguator` plugged into
  the R2.1 graph via the optional `disambiguator=` hook. `classify(i,j,carried)`
  → disjoint / proven / conservative. Rules (each provably safe under M2's model):
  (1) clean-slot — a non-escaping local slot can't be aliased by computed/global/
  call; (2) distinct-local-objects — `&a` vs `&b` non-overlapping; (3) same-base
  SIV — same base value + offsets affine in the same IV with equal scale: intra
  disjoint iff const parts differ, carried disjoint unless const-diff is a nonzero
  multiple of stride=scale·step (needs known nonzero step; `a[i]` vs `a[i]` has no
  carry). Everything else conservative; genuine edges tagged `proven`.
- **Reused (no duplication):** M1 `basic_ivs`/`iv_terms`, M2 `clean_slots`/
  `written_keys`/`_access_key`/`may_alias`, DefUse `single_defs`, M0 `discover`.
- **Modified (additive):** `depgraph.py` — `DepEdge.proven/reason`, optional
  `disambiguator=` + eliminated counters, inline memory intra/carried emission
  (no-disambiguator path byte-identical to R2.1, verified mem=13019), query
  helpers. `__init__.py` exports. Default R2.1 output & tests unchanged.
- **Results (R2.1→R2.2):** memory edges 13019→8271 (**4748 eliminated, 36.5%**);
  loop-carried 2544→1977 (567 false carried pruned); surviving 2088 proven /
  6183 conservative. Elim by reason: clean-slot-vs-global 2691, clean-slot-vs-
  computed 1375, clean-slot-vs-call 449, distinct-const-offset 162, distinct-
  local-objects 69, siv-self-index-no-carry 2.
- **Soundness:** R2.2 memory edges are a strict SUBSET of R2.1's (only drops,
  never adds/redirects); register/control edges identical — verified per-function
  across all 194 functions (0 subset violations, 0 rc diffs).
- **Validation:** `_r2_2_test.py` 39/39; `depgraph_r22_corpus.py` 124/194 0
  violations/0 mutations/124-124 compile+bundle; R2.1 40/40 + corpus + pipeline
  124/124 0 rollbacks all unchanged.
- **Not done (by design):** graph not consumed by any pass; no scheduling /
  critical-path / SWP / modulo; `a[i+1]`-style ±const-in-index + MIV + symbolic
  base disambiguation left conservative for a future step. R2.3 not started.

---

## R2.3 — Dependence-Aware IR Scheduler  (2026-07-25) ✅ DONE

**First optimisation that consumes the DependenceGraph.** Reorders IR *within
basic blocks only*, semantics-preserving. Standalone pass (NOT wired into
production compiler.py — shipped output frozen, pipeline crosscheck 124/124
identical). Full report: `loopopt/R2_3_DELIVERY.md`.

- **Added** `loopopt/schedule.py` — `schedule_module()` critical-path list
  scheduler over the R2.2 graph. Per block: pin leading label/func-begin +
  trailing terminator; constraints = NON-carried intra-block edges (RAW/WAR/WAW/
  MEM/CONTROL, all low→high so DAG acyclic); priority = dependency height
  (critical path), tie-break smallest original index (deterministic). Carried
  edges respected BY CONSTRUCTION (intra-block reorder can't cross the back-edge;
  adding them would cycle with intra partners). Guarded by internal topo verifier
  + `ir_interp.differential` per-function, rollback on mismatch.
- **Reused:** R2.1 DependenceGraph, R2.2 MemoryDisambiguator (fewer false mem
  edges → more freedom), ir_interp differential oracle, func_slices.
- **Modified (additive):** `__init__.py` exports only.
- **Corpus:** 124 progs / 194 fns (128 changed), 1064 blocks (349 reordered),
  2591 instrs reordered; 0 verifier-fail / 0 rollbacks / 0 compile-fail / 0
  behaviour-mismatch; 119 differentially verified + 75 unsupported(legal-by-constr).
- **Perf (R2.2→R2.3):** bundler-ON (production) bundles 6498→6218 (−280, −4.3%),
  IPB 1.829→1.915; bundler-OFF (isolates scheduler) bundles 7858→7102 (−9.6%),
  IPB 1.512→1.677 (+10.9%). Static +24 (reg-alloc), spills 0→0. FIRST R2 IPB gain
  (R1.x unroll was flat) — scheduling targets the M11 ILP-exposure bottleneck.
- **Not done (by design):** not pipeline-wired; no cross-block/global sched, no
  SWP/modulo/trace/superblock; latency-weighted height + reg-pressure tie-break
  are R2.4+. `_r2_3_test.py` 34/34; R2.1/R2.2 + pipeline all unchanged.

---

## R2.4 — Scheduler Quality Improvements  (2026-07-25) ✅ DONE

**Additive quality upgrades to the R2.3 block scheduler** (no redesign; still
standalone/not production-wired; pipeline crosscheck 124/124 identical). Full
report: `loopopt/R2_4_DELIVERY.md`.

- **Modified (additive)** `loopopt/schedule.py` — `SchedPolicy` (R23 reproduces
  R2.3 exactly; R24 = new default), plus 4 features:
  (F1) **latency-aware** critical path: `_latency` ISA-conservative (load/mul 3,
  div/%/fsqrt 8, call 5, vec 2–4, else 1); height = latency+max(succ).
  (F2) **register-pressure** tie-break BELOW height (never sacrifices critical
  path): prefer ready node with max deaths−births (frees > defines); reuses
  `compute_liveness` live-out + in-block use counts.
  (F3) **bundle-aware** tie-break: light lane-cap model (≤4 ld/st, ≤1 div/sqrt,
  8 total) mirrors bundler limits (knowledge, not code); informs tie-break +
  utilisation estimate only.
  (F4) **statistics** on ScheduleStats: crit-path, avg ready size, pressure peak,
  movement (sum/max), est bundle util, metriced blocks.
  Determinism preserved (final −index tie-break). `__init__.py` exports SchedPolicy.
- **Reused:** R2.1/R2.2 graph (unchanged), compute_liveness, ir_utils def/use,
  ir_interp differential, bundler's documented lane caps.
- **Corpus (baseline→R2.3→R2.4, bundler ON):** static 11885→11909(+24)→11889(+4)
  — pressure work cut codegen bloat +24→+4; bundles 6498→6218→**6188** (R2.4 −30
  vs R2.3); IPB 1.829→1.915→**1.921**; spills 0 throughout. 132 fns changed
  (vs 128), 2858 instrs moved (vs 2591); 0 rollbacks/0 structural/0 mismatch both
  policies. Scheduling time 0.39s→0.48s (liveness+richer priority; trivial).
- **Validation:** `_r2_4_test.py` 33/33; R2.1/R2.2/R2.3 unit+corpus + pipeline all
  unchanged (R2.3 tests pass under R2.4 default — not weakened).
- **Not done (by design):** not production-wired; no cross-block/SWP/modulo;
  hardware-accurate latency table + pressure-ceiling + slack-based priority are
  future local-quality work; SWP/modulo (uses R2.1 recurrence edges) is the next
  architectural milestone, not started.

---

## R2.5 — Software Pipelining (Modulo Scheduling)  (2026-07-26) ✅ DONE

**First optimisation that schedules across loop iterations.** Standalone (NOT
production-wired; pipeline crosscheck 124/124 identical). Correctness mandatory:
analysis phases mutation-free; generation gated by structural + multi-seed
differential + compile, rollback otherwise. Full report: `loopopt/R2_5_DELIVERY.md`.

- **Added** `loopopt/modulo.py` (5 phases): eligibility (one innermost top-tested
  counted loop, single preheader/latch/exit, clean IV, no calls) + kernel model;
  **Phase 1** RecMII (Bellman-Ford min-II no-positive-cycle over recurrence edges,
  distance 1) / ResMII (caps 8/4/1) / MII; **Phase 2** iterative modulo scheduler
  + ReservationTable + verify_schedule; **Phase 3** realise known-trip schedule by
  linearising over T iterations (instance at it*II+cycle, per-iteration temp
  banks = MVE by renaming, shared memory slots self-serialise recurrences) →
  prologue/kernel/epilogue; **Phase 4** gate; **Phase 5** ModuloStats.
- **Reused:** M0-M3 descriptors, R2.1 graph+recurrence edges, R2.2 disambig, R2.4
  latency/class/caps, ir_interp differential. `__init__.py` exports.
- **CRITICAL BUG FIXED mid-dev:** pipeline_module first rebuilt output from
  func_slices only → DROPPED global decls (before first IRFuncBegin) → other
  functions read 0 for globals → 3 corpus behaviour mismatches. Fix: preserve
  non-function regions (globals/inter-fn) via prev_end tracking. The in-gate
  differential validated the correctly-spliced `new`; only final reassembly was
  broken. Lesson: rebuilding a module from func_slices loses out-of-function code.
- **Corpus:** Phase 1: 45/79 eligible, **42 RecMII-bound vs 2 ResMII** (memory
  IV/accumulator recurrences ≈5 dominate), MII hist {5:34,...}. Phase 3-4: 12
  pipelined / 26 declined / 7 rolled-back (trip-not-known 21, diff-rollback 7,
  single-stage 2, trip-too-small 3); **0 mismatches / 0 compile failures**; avg
  stages 2.3.
- **Perf (baseline→R2.3→R2.4→R2.5, bundler ON):** static 11885→11889→**13188**,
  bundles 6498→6188→**6759**, IPB 1.829→1.921→**1.951** (highest), spills 0→**2**.
  Honest tradeoff: full-unroll realisation trades CODE SIZE (static up) for ILP
  (IPB highest; isolated sum loop 1.36→2.9) + fewer dynamic iterations; compact
  kernel-loop MVE = future work.
- **Not done (by design):** not production-wired; no compact kernel loop / no
  symbolic-trip / no register-promotion of recurrences (the RecMII enabler) / no
  cross-BB/trace/superblock/hyperblock/speculation. `_r2_5_test.py` 30/30; R2.1-4
  + pipeline all unchanged.

---

## R2.6 — Loop Register Promotion  (2026-07-26) ✅ DONE

**Attacks R2.5's RecMII bottleneck: memory-backed loop-carried recurrences →
register recurrences.** Runs BEFORE R2.5, never modifies it. Standalone (NOT
production-wired; pipeline crosscheck 124/124 identical). Full report:
`loopopt/R2_6_DELIVERY.md`.

- **Added** `loopopt/loop_promote.py`. Promotable = CLEAN slot (M2 clean_slots =
  no alias/escape), loaded + EXACTLY ONE store, one width, in a single-preheader/
  latch/exit loop with no calls. Transform = the codegen-safe IRAssign-move shape
  PROVEN on hardware by production loop_reg: preheader `P=load(&X)`, body load
  `t=*(&X)`→`t=P`, body store `*(&X)=v`→`P=v`, exit `store(&X)=P`; drop dead
  loadaddrs. Promotes IV + accumulators (sum/prod/min/max). RecMII before/after +
  mem-recurrence-removed computed from the R2.1 graph (graph-based so it works
  after the IV loses its memory form).
- **Reused:** M0-M3 descriptors + M2 clean_slots (the correctness PROOF), R2.1
  graph/R2.2 disambig, R2.5 modulo (rec_mii/KernelModel/_compiles, consumed not
  modified), ir_interp. `__init__.py` exports.
- **Correctness:** register promotion of a CLEAN slot is sound by M2 escape
  analysis (nothing but the promoted load/store touches it). Gate = structural +
  clean-slot-respecting multi-seed differential (pointers kept NON-NEGATIVE so
  they can't fabricate impossible aliasing with the negative clean slot -- the
  false-reject bug I hit) + compile; 'unsupported' (call outside loop aborts
  interp) accepted on the clean-slot proof (like loop_reg), only 'mismatch'
  rolls back. Determinism needs `_rp_n` reset per driver call (ivsr._iv_n lesson).
- **Corpus:** 124 progs/57 loops, 48 promotable, **46 promoted** (33 diff-verified
  + 13 proof-only), 2 rollbacks, **0 mismatches**, **198 mem recurrences removed**,
  **RecMII 5.48→3.67**, 98 fewer loop-body memory ops. `_r2_6_test.py` 24/24.
- **Perf (baseline→R2.3→R2.4→R2.5→R2.6):** static 11885→...→11983, bundles 6498→
  6527, IPB 1.829→1.836, spills 0. STATIC ~flat (register promotion is a DYNAMIC
  win = fewer executed loads/iter, not static bundles).
- **KEY LIMITATION (honest):** R2.6→R2.5 does NOT compose in the GENERATOR —
  R2.5's build_kernel needs a MEMORY-slot counted IV (M1 is memory-based);
  promoting the IV removes it → R2.5 declines promoted loops (pipeline coverage
  12→2). RecMII win realised in ANALYSIS + for local R2.3/R2.4 scheduler, not
  R2.5's generator. NEXT = teach R2.5/M1 register IVs (out of scope, R2.5 frozen).
- **Not done (by design):** no SSA reconstruction / mem2reg / speculative /
  alias-speculation / rotating-regs / MVE / production-integration / prior-pass
  changes.

---

## R2.7 — Register-Aware Software Pipelining  (2026-07-26) ✅ DONE

**Integrates R2.6 into R2.5: extends ONLY R2.5's recognition layer so the modulo
scheduler pipelines register-recurrence loops.** Consumes R2.5/R2.6 unmodified;
no MVE/rotating regs/scheduler redesign. Standalone (not production-wired;
pipeline crosscheck 124/124 identical). Full report: `loopopt/R2_7_DELIVERY.md`.

- **Added** `loopopt/pipeline_regaware.py`. KEY: R2.5's ONLY memory-IV assumption
  is build_kernel eligibility `primary_iv is None or trip==UNKNOWN`; everything
  else structural, and the graph already has register recurrence edges. R2.7
  recognition: analyse ORIGINAL (M1 trip T) → R2.6 promote → NORMALISE promoted
  desc (trip_count=KNOWN(T) carried over, sentinel primary_iv) → feed R2.5
  build_kernel/modulo_schedule UNCHANGED (RecMII 5→3, II 5→3). `LoopRecurrence`
  canonical abstraction (memory/register, from carried edges).
- **CRITICAL realization fix:** R2.5 generate_pipeline renames ALL temps per
  iteration → BREAKS register recurrence (each iter accumulates into independent
  reg → sum=0). Memory recurrences survive because the SLOT is shared/unrenamed.
  Fix = keep loop-carried registers SHARED (identity across banks) via
  `realize_register_pipeline` that PRE-SEEDS R2.5's `_clone_op` cache so
  recurrence temps (resources of carried RAW edges) map to themselves; everything
  else expanded per-iter. Reuses _clone_op EXACTLY (one clone routine, only cache
  seed differs mem vs reg). NOT MVE (no rotating regs/compact kernel — shared
  loop-carried storage of a full unroll, same as R2.5 does for memory slots).
- **Reused:** R2.5 build_kernel/min_ii/modulo_schedule/_clone_op/generate_pipeline
  (memory form)/PipelineResult/_compiles, R2.6 promote_function/_promote_diff
  (clean-slot-respecting gate). Determinism = reset loop_promote._rp_n per driver.
- **Gate:** structural + clean-slot multiseed diff (promoted-vs-pipelined +
  end-to-end original-vs-pipelined) + compile, rollback on mismatch. Prefer
  register form (lower II); fall back to memory form (Case A) same code path.
- **COVERAGE (headline):** R2.5 alone 12 → R2.6→R2.5 **2** (the regression) →
  **R2.7 17** (13 register + 4 memory). 0 mismatches, 1 rollback. Recovers AND
  EXCEEDS R2.5 (lower register II makes more loops profitable, stages≥2).
- **PERF (baseline→R2.4→R2.5→R2.7):** static 11885→13651, bundles 6498→6894, IPB
  1.829→1.921→1.951→**1.980 (HIGHEST of series)**, spills 0→2→3. Costs: more
  full-unroll code + 3 spills (honest code-size-for-ILP trade). `_r2_7_test.py`
  20/20.
- **Not done (by design):** full-unroll only (compact kernel loop needs MVE, out
  of scope), symbolic-trip declined, no prior-pass/scheduler/regalloc/bundler
  redesign, no production integration.

---

## R2.8 — Modulo Variable Expansion / Compact Rotating Kernel  (2026-07-26) ✅ DONE

**Replaces ONLY the realisation strategy of R2.5/R2.7: the O(T) full unroll becomes
a compact prologue → kernel-loop → epilogue of O(S).** The modulo scheduler,
register promotion, the dependence graph, the recurrence abstraction, the bundler,
register allocation and LoopInfo are all consumed UNCHANGED — no hardware rotating
registers. Standalone (not production-wired; pipeline crosscheck 124/124 identical,
0 rollbacks). Full report: `loopopt/R2_8_DELIVERY.md`.

- **Added** `loopopt/pipeline_mve.py` (window emission with the MVE rename,
  `realize_mve_kernel`, the codegen live-range invariant, the
  register→memory / compact→full-unroll driver, `MVEStats`/`MVEReport`).
  `loopopt/__init__.py` additive exports only.
- **MVE algorithm:** the linearised R2.5/R2.7 schedule places instance
  *(iteration `it`, op `o`)* in window `W = it + stage(o)`,
  `stage(o) = cycle(o)//II ∈ [0,S-1]`; in steady state S iterations are in flight.
  Rolling the windows into one loop would make a value live across back-edges, so
  every per-iteration temp is renamed into bank `b = it mod U` with **`U = S`**.
  Max live span `≤ S-1 < S = U` → the rename is CONFLICT-FREE, and the S in-flight
  iterations occupy S distinct banks = a rotating register file of constant
  footprint, entirely in IR. Loop-carried recurrence registers are the one thing
  kept **SHARED** across banks (exactly as R2.5 keeps a memory slot shared).
- **Kernel generation (known trip T):** `prologue = windows [0..S-2]` (seeds every
  rotating reg) → `kernel loop = windows [S-1..S-2+U]` emitted ONCE and run
  `K = (T-S+1)//U` times → `remainder` → `epilogue = windows [T..T+S-2]`. The loop
  is a do-while over a fresh counter (`_mvk = K; … if _mvk>0 goto head`). Static
  size is O(S), independent of T — a 64-iteration reduction (II=3, S=2, U=2) goes
  **640 → 44 instructions**, K=31, 5 rotating regs.
- **KEY correctness (the one thing the IR differential CANNOT see):** register
  allocation. Certified by an explicit invariant computed with codegen's OWN
  `_compute_last_uses` — every rotating register the body reads-before-writes must
  have its live range extended to the back-edge (true iff defined before the
  header, which the prologue guarantees). If not, the compact form is DECLINED.
- **Gate:** structural + clean-slot multiseed differential + compile + the codegen
  live-range invariant. Requires `T ≥ 2S-1` and that the compact form is actually
  smaller; otherwise falls back to R2.7's proven full unroll, so **coverage never
  regresses**.
- **CORPUS:** coverage **17 = R2.7** (10 compact kernel + 7 full-unroll fallback),
  0 rollbacks, 5 declined, **0 mismatches**; avg II/stages 4.24/2.35; compact avg
  bank size/rotating regs 2.20/4.10; static IR on compacted loops **1038 → 474
  (−54.3%)**. `_r2_8_test.py` 32/32.
- **PERF (baseline→R2.4→R2.5→R2.7→R2.8):** static 11885→11889→13188→13651→**12661**,
  bundles 6498→6188→6759→6894→**6583**, IPB 1.829→1.921→1.951→1.980→**1.923**,
  spills 0→0→2→3→**1**. vs R2.7: static **−990 (−7.3%)**, bundles −311, spills −2.
- **IPB (honest):** dips 1.980 → 1.923 — the INVERSE of R2.7's trade. R2.7 spent
  code size to raise static density; a real kernel loop cannot expose the same flat
  ILP (loop-carried deps + counter/branch), so some density is given back. The
  **schedule and II are identical**, so DYNAMIC per-iteration throughput and memory
  ops are unchanged from R2.7 — only the static footprint (and the pressure/spills
  it caused) shrinks. Still above baseline 1.829 and R2.4 1.921.
- **Not done (by design):** known trip counts only (symbolic declined cleanly);
  `U = S` banks, not the minimal `MaxLive+1`; IR-level validation (no hardware
  sim), mitigated by the codegen invariant; no redesign of scheduler / promotion /
  depgraph / bundler / allocator; no production integration.

---

## R3.0 — Oracle ILP Bound Analyzer  (2026-07-26) ✅ DONE

**ANALYSIS ONLY — the decision tool for every future optimization.** Computes the
theoretical ILP of every innermost loop and quantifies WHY the scheduler cannot
reach it. Mutates no IR; changes no scheduling, bundling, allocation or generated
code: proven **byte-identical 124/124**. Full report: `loopopt/R3_0_DELIVERY.md`.

- **Added** `loopopt/oracle_ilp.py` (oracle DAG, critical-path/MII metrics,
  ready-set simulation, the three IPB numbers, limiter classification, opportunity
  ranking, `LoopILP`), `oracle_report.py` (per-loop + per-module view, CLI),
  `oracle_corpus.py`. `__init__.py` additive exports only.
- **Reused (no analysis duplicated):** R2.1 DependenceGraph + R2.2
  MemoryDisambiguator (the typed DAG); `analysis_profile` (M3 / the M11 statistics
  framework), which itself reuses `parallelism_profile`'s `_critical_path` /
  `_rec_mii` / `_res_mii_detail` / `_reg_pressure`; `schedule.py`
  `_latency`/`_iclass`/`_CAP`/`_BUNDLE_MAX` and `modulo.py` `_edge_latency`. The
  ONLY new code is the ready-set list-scheduling simulation (a measurement, not a
  transform) plus the classification/opportunity logic.
- **The analytical core — three IPB numbers:**
  `theoretical_ipb = min(N/MII, 8)`, `MII = max(RecMII, ResMII)` — the ceiling no
  scheduler can beat (perfect SWP + infinite regs, but REAL caps 8/4/1/1 and REAL
  recurrence latency); `local_ideal_ipb` — best LOCAL schedule of one iteration,
  true-deps only, infinite regs; `achieved_ipb` — the current in-order greedy pack
  with ALL deps. `utilization = achieved/theoretical`, and
  `total gap = pipelining_gap (theo−local) + scheduler/renaming_gap (local−ach)`.
- **CORPUS (124 programs, 65 innermost loops):** theoretical **5.22**, local-ideal
  3.70, achieved **1.64**, mean utilization **32%** (real measured aggregate IPB
  1.83, cross-checks the 1.64 model). Bottlenecks: resource/issue-width-bound 48
  (74%), control-bound 14 (22%), recurrence-bound (memory) 3 (5%). RecMII is 3 on
  61 of 65 loops. Ready-set: **70 cycles expose 8+ ready instructions**, 106 expose
  only 1. Ranked opportunity: software-pipelining 74%, vectorization 17%,
  register-renaming 6%, reassociation 2%, register-promotion 2%.
- **THE VERDICT (drives R3.1 and R4.x):** the gap is NOT the dependence structure
  (ceiling averages 5.22; 74% of loops are merely issue-width-bound) and NOT the
  register file (pressure never binding, RecMII ~3 everywhere). It IS
  **exposed-but-unexploited ILP** — 1.64 achieved against a 5.22 ceiling, with 70
  cycles' worth of 8+ ready instructions left on the table. Highest-value lever =
  **software pipelining wired into production** (74%), then **vectorization** (17%).
  The machine has 8 lanes, the loops contain ~5 IPB, the compiler manufactures ~1.6.
- **Validation:** the corpus harness snapshots, runs the oracle and re-generates —
  byte-identical 124/124 (IR repr and mcode); ceiling contract asserted per loop
  (`theoretical == min(N/MII,8)`, `MII == max(RecMII,ResMII)`, `theoretical ≥
  achieved`, `utilization ≤ 1`). `_r3_0_test.py` 31/31.

---

## R3.1 — Production Software Pipelining Integration  (2026-07-26) ✅ DONE

**FIRST wiring of the frozen R2.5–R2.8 pipeline into production
`compile_c_to_mcode()`.** No new scheduler — pure integration + profitability +
validation + rollback; the depgraph, disambiguator, R2.5–R2.8, the oracle, the
bundler, the allocator and the spill-tier fallback are all CONSUMED unmodified.
`APARA_NO_SWP=1` reverts instantly. Full report: `R3_1_DELIVERY.md`.

- **Added** `production_swp.py` (oracle-gated candidate selection, invoke R2.8,
  per-function differential + zero-spill validation, splice into the production IR,
  per-function rollback; `ProfitabilityRecord`, `SWPSummary`,
  `apply_production_swp`, `format_profitability`), `swp_prod_corpus.py`.
  **Modified** `compiler.py`: capture the selected production IR (`_sel_ir`), then
  ONE guarded SWP block — ~20 lines, additive.
- **Where it sits:** the production optimizer picks the best non-spilling tier →
  `_sel_ir` (today's output). Then `oracle profitability → R2.8 pipeline →
  per-function differential ('match') → splice into _sel_ir → whole-program
  ZERO-spill check → accept | rollback`. SWP runs on the raw memory-backed `_ir0`
  (R2.5/R2.6 need memory-slot IVs, exactly as validated standalone) and each
  committed function slice REPLACES that function in `_sel_ir`.
- **Profitability = the R3.0 oracle, no new cost model:** keep a function when an
  innermost loop's top opportunity is software-pipelining with estimated IPB gain
  **≥ 0.5** (`APARA_SWP_THRESHOLD`).
- **Gate (all must hold, else rollback to the proven slice):** R2.8 commits a
  validated pipeline; the clean-slot multiseed differential returns a **definite
  'match'** — STRICTER than the standalone gate, which also accepts a clean-slot
  `unsupported` proof, so a function the interpreter cannot execute is
  conservatively left un-pipelined; and the whole spliced program still compiles
  with `cg.spilled == False`. Candidates are applied greedily, highest expected
  gain first, each re-checked, so one spilling function never blocks another. The
  whole block is wrapped so any unexpected error keeps the proven `body`
  byte-for-byte — correctness preserved BY CONSTRUCTION.
- **CORPUS (124 programs):** oracle SWP recommendations 48 loops; standalone R2.8
  coverage 17; **PRODUCTION pipelined 10**; rollbacks 4 (ALL
  differential-unsupported, **0 spill-driven**); utilization 10/17 of R2.8-eligible
  (**59%**); **0 behaviour mismatches**; 1.21 s total = **9.8 ms/program**.
- **PERF (production SWP off → on):** static 11139→11678 (+539, +4.8%), bundles
  5986→6075 (+89, +1.5%), IPB **1.861 → 1.922 (+3.3%)**, programs that spill 0→0.
  The added bundles are DENSER — IPB rises even though bundle count rises slightly.
  All 6 success criteria met. `_r3_1_test.py` 16/16; `pipeline_crosscheck` 124/124.
- **Status:** DEFAULT-ON but **IR-verified only** — a simulator pass is recommended
  before hardware deployment; the kill-switch is ready.
- **Not done (by design):** no new scheduler, no pass/bundler/allocator changes.

---

## R3.2 — Superblock / Trace Scheduling  (2026-07-27) ✅ DONE

**A region-FORMATION pass, not a new scheduler:** it enlarges scheduling regions
beyond basic blocks so the existing R2.4 scheduler and the bundler pack across
them. Trace scheduling **without speculation and without duplication**. The CFG,
LoopInfo, Dominators, DependenceGraph, disambiguator, `loopopt/schedule.py`, the
bundler, the oracle and the existing validator/rollback are all CONSUMED
unmodified. `APARA_NO_SUPERBLOCK=1` disables it. Full report: `R3_2_DELIVERY.md`.

- **Added** `superblock.py` (CFG-based merging of single-entry/single-exit
  straight-line chains; `RegionStats`, `form_superblocks`, `superblock_module`),
  `trace_scheduler.py` (driver + oracle gate + spill/bundle-safe acceptance;
  `apply_superblock_scheduling`, `format_superblock`), `superblock_corpus.py`.
  **Modified** `compiler.py`: a guarded block after the R3.1 SWP block, tracking
  `_prod_ir` — ~20 lines, additive.
- **What the merge is:** block `B` merges into layout-predecessor `P` iff `B` has
  exactly one predecessor (`P`), `P` has exactly one successor (`B`), `B` is
  adjacent, and the `P→B` edge is a fall-through or a redundant `goto B`. Dropping
  that boundary (the now-single-predecessor label + any redundant goto) is a **PURE
  NO-OP** — control already flowed `P→B` unconditionally. Nothing is hoisted above
  a branch, nothing is copied onto an off-trace path. Multi-entry regions,
  conditional side exits and irreducible control flow are left unmerged.
- **PRIME TARGET:** a counted loop whose body and IV-increment the front end split
  with a **DEAD label** (nothing branches to it — the back-edge targets the
  header). Merging lets the scheduler overlap body with increment and lets the
  bundler pack across what was a hard label barrier. No compensation code is ever
  needed because control flow is preserved exactly.
- **Gate:** attempted only when the R3.0 oracle reports scheduling headroom
  (theoretical − achieved ≥ 0.5, `APARA_SUPERBLOCK_THRESHOLD`); region formation is
  semantics-preserving by construction (non-control instruction multiset unchanged
  = no duplication); the scheduler self-validates each function with the
  differential and rolls back; **production acceptance requires ZERO spills AND the
  bundle count NOT to increase**, else the proven R3.1 `body` is kept.
- **GOTCHA (important, cost real debugging time):** 5 apparent mismatches were
  **PRE-EXISTING ir0-vs-optimized gaps in the ir_interp oracle** (division /
  sub-word / bit-manipulation), present in R3.1 and earlier and unrelated to region
  enlargement. R3.2 is correctly validated against **its input** (the R3.1 output)
  at the same optimization level → **0 mismatches**.
- **CORPUS (124 programs, R3.1 → R3.2):** oracle attempted 33, accepted 33,
  rollbacks 1, **0 behaviour mismatches vs R3.1**; avg scheduling region **7.18 →
  8.36 blocks (+17%)**; 5.0 ms/program.
- **PERF (R3.1 → R3.2):** static 11678→11686 (flat), bundles 6075→**5916 (−159)**,
  IPB 1.922→**1.975 (+2.8%)** — denser packing at flat static size. Cumulative
  production IPB: **baseline 1.861 → R3.1 1.922 → R3.2 1.975**. All 6 criteria met.
  `_r3_2_test.py` 23/23; `pipeline_crosscheck` 124/124.
- **Not done (by design):** straight-line region enlargement only — no speculation,
  no duplication, no scheduler/bundler/allocator/SWP/oracle changes.

---

## R4.0 — APARA Vector Infrastructure & Capability Framework  (2026-07-27) ✅ DONE

**The vector equivalent of R3.0: the production FOUNDATION for all future vector
optimization.** NOT a vectorizer — emits no vector instructions and changes no
scalar code (byte-identical **124/124**). Every ISA fact is determined DIRECTLY
from the production implementation, never assumed. Full report: `R4_0_DELIVERY.md`.

- **Added** `vector_capability_db.py` (the ground-truth ISA database),
  `vector_capability.py` (the reusable query layer — the single API future passes
  consult), `vector_legality.py`, `vector_profitability.py`, `kernel_detector.py`
  (6 idiom classes, structure only), `vector_validation.py` (the vector
  differential oracle), `vector_corpus.py`. **Modified** `compiler.py`: ONE opt-in,
  print-only diagnostic block (`APARA_VECTOR_REPORT` + verbose) that never touches
  `body`/`mcode`.
- **CAPABILITY MAP — extracted from codegen's emitters + ir_gen's intrinsic
  lowering + the no-bias `golden_stubs.h` reference, NOT from the ISA document:**
  lane model = `64/element_bits` packed lanes per 64-bit register (vi8 = 8 lanes,
  vi16 = 4, vi32 = 2); `$v` (VALU) element-wise `+ - *` plus `$replicate`;
  **`$dot` / `$dot $accumulate` for vi8/vu8/vi16/vu16 only — NO 32-bit dot**;
  `$dot128` = 16×vu8; **`$vreduce +` SIGNED ONLY** (unsigned sign-extends — a
  confirmed simulator bug), `$vreduce $max` all types, other reduce ops return 0;
  wide `$ld/$st` (`$u128`/`$u256`) aligned contiguous 2/4-word moves;
  `$slice`/`$pack`/`$fsqrt`.
  **CONFIRMED BROKEN, never to be emitted:** 4-bit lanes (vi4/vu4), unsigned
  `$vreduce` sum, `$vreduce` min/mul/or/xor/and, native `$abs/$max/$min`, 32-bit dot.
- **THE KEY METHODOLOGY PIECE:** `ir_interp` raises `Unsupported` on vector IR, so
  `vector_validation.py` **EXTENDS it without modifying it** with a `VectorInterp`
  executing `IRVecArith/Dot/Dot128/Reduce/LoadWide/StoreWide` per `golden_stubs.h`
  — **faithful to the hardware INCLUDING ITS BUGS**, so the oracle would catch a
  pass that wrongly used an unreliable op. `differential_vector(scalar, vector, …)`
  is the vector equivalent of the scalar differential that gated R2/R3, and is
  validated against real intrinsic-produced programs (reproduces `__dot_vi8` → 36
  exactly, and catches an injected mismatch).
- **Legality** grounds every decision in the capability layer: innermost counted
  loop, single exit, no calls, supported+reliable element type, ISA-supported
  operation, affine accesses, and — via the R2.2 disambiguator — no loop-carried
  memory dependence except the clean scalar IV/accumulator slots.
- **CORPUS (124 programs):** scalar code UNCHANGED 124/124. Detected 40 kernels
  (sum-reduction 28, vector-add 7, dot-product 2, matmul 2, saxpy 1); **legal 12**;
  **profitable 6** (avg 2.0 lanes / 2.0× / 47%). Rejections: unsupported element
  type 11, unproven aliasing 5, call in body 5, trip-count unknown 4, no-32bit-dot
  2, unsigned-vreduce-buggy 1. Oracle executes 6/6 real vector-intrinsic programs.
- **HEADLINE VALUE = the REJECTIONS.** The framework refuses the unsigned-byte-sum
  and 32-bit-dot kernels **the hardware cannot do correctly**, while accepting the
  8-lane signed-byte kernels (8×) — no assumptions, all traceable to
  `golden_stubs.h`/STATUS.md. All 8 success criteria met. `_r4_0_test.py` 30/30;
  `pipeline_crosscheck` 124/124.
- **Not done (by mandate):** no automatic vectorization, no vector emission, no
  scalar redesign. matmul/convolution are recognised structurally but their 2-D
  compound indices are not yet proven affine (reported, not vectorized); symbolic
  trips, non-unit strides and pointer-aliased arrays rejected pending later
  milestones. R4.1 (dot/reduction), R4.2 (elementwise), R4.3 (matmul), R4.4
  (general) build on this foundation.

---

## R4.1 — Automatic Dot & Reduction Vectorization  (2026-07-27) ✅ DONE

**The FIRST production vector transform — real `$dot` and `$vreduce` instructions
now appear in generated mcode.** Two kernels only: dot product and sum reduction.
Vectorization runs FIRST, so the vectorized IR flows through the existing scalar
optimizer, scheduler, bundler, allocator and backend UNCHANGED; a function with no
committed kernel is byte-identical to today. `APARA_NO_VECTORIZE` disables it.
Full report: `R4_1_DELIVERY.md`.

- **Added** `dot_vectorizer.py`, `reduction_vectorizer.py`, `vector_lowering.py`
  (whose `PackedVectorInterp` subclasses the R4.0 validator), `vectorize_corpus.py`.
  **Modified** `compiler.py`: the vectorize-first block ahead of scalar opt.
- **THE CRUX:** APARA stores ordinary arrays **one element per 8-byte word (stride
  8)**, so ONLY the packed typedef markers (`vu8_t`/`vi8_t`/`vu16_t`/`vi16_t`/
  `vu32_t`/`vi32_t`) are vectorizable at all — anything else would need an
  expensive gather. This single fact bounds the whole milestone's coverage.
- **Lowering:** chunks unrolled as packed 64-bit loads (a marker gather) feeding
  `$dot` accumulation, or `$vreduce` + a scalar accumulator, followed by a scalar
  remainder loop — and the remainder loop is **dropped entirely when rem = 0** by
  setting the IV slot to N.
- **Gate (any failure → rollback to scalar):** legality (packed + reliable type +
  ISA-supported op + no alias) + profitability (lanes ≥ 2, trip ≥ 2·lanes, counted
  on **DYNAMIC not static** ops) + lowering + `differential_packed` (6 seeds over
  the **FULL byte/half-word range**, so sign/zero-extension and overflow divergence
  surface) + spill-free compile + a dynamic-operation reduction.
- **GOTCHA that makes the oracle LOAD-BEARING:** a dot with a **narrow 32-bit
  accumulator DIVERGES** — the vector form accumulates 8 lanes in 64 bits while the
  scalar form wraps per iteration — and the differential **automatically rolls it
  back**. Caught, not mis-compiled. The differential is not ceremony here.
- **CORPUS — dedicated packed-kernel suite (10 kernels):** vectorized **7/10** (dot
  vi8/vu8/vi16, reduction vi8/vi16/vi32, + remainder), **0 behaviour mismatches**
  (100% differential validation), dynamic operations **5892 → 326 (−94%)**, real
  `$dot`/`$vreduce` in the mcode (2–8 per kernel). Correctly rejected: vi32-dot (no
  ISA support), narrow-accumulator (rollback), unpacked arrays.
- **CORPUS — full 124 programs:** vectorized 0 (the general corpus has no packed
  arrays) and scalar output **BYTE-IDENTICAL on/off 124/124 — no regression**. All
  7 success criteria met. `_r4_1_test.py` 28/28; `pipeline_crosscheck` 124/124;
  R2.5–R4.0 suites all pass.
- **Honest limitations:** packed arrays only, so general-corpus coverage is ~0 —
  the value is demonstrated on the dedicated suite. **Wide accumulator required**
  (narrow ones are rolled back — correct, if conservative). **Static size may
  grow** (chunks are unrolled); the win is dynamic (−94% ops), and large N would
  benefit from a compact vector loop. Validation is the packed IR oracle modelling
  `golden_stubs.h` semantics (no hardware simulation, per policy); a
  simulator-backed gate remains available.
- **Not done (by mandate):** R4.2 elementwise, R4.3 matmul, R4.4 general
  vectorization, convolution.

---

## R4.2 — Generic Vectorization Framework & Elementwise Vectorization  (2026-07-31) ✅ DONE

**Two deliverables: the R4.1 driver becomes reusable production infrastructure,
and elementwise vectorization becomes its first new client.** The framework half
is a REFACTOR — R4.1's dot/reduction output is proven byte-identical through it —
and the elementwise half adds ZERO pipeline logic. There is exactly ONE production
vectorization pipeline. `APARA_NO_VECTORIZE` disables all of it. Full report:
`R4_2_DELIVERY.md`.

- **Added** `vector_pipeline.py` (the generic pipeline: `VectorTransform`,
  `MatchResult`, `DynamicModel`, `run_module`, `format_reports`),
  `elementwise_vectorizer.py` (the client), `vector_elementwise_lowering.py`
  (pattern match + lowering), `vector_elementwise_corpus.py`, `_r4_2_test.py`.
  **Modified** `dot_vectorizer.py` (driver REMOVED — now just
  `DotReductionTransform` + the unchanged entry points + `vectorize_all_module`;
  205→92 lines), `vector_lowering.py` (`PackedVectorInterp` gains the packed
  STORE — additive, inert for R4.1), `compiler.py` (the hook now calls
  `vectorize_all_module`; same guard, same kill-switch, same position).
- **THE FRAMEWORK CONTRACT:** a client supplies `kinds` + `match()` + `lower()` +
  `dynamic_model()` (+ an optional `validate()`, defaulting to the packed
  differential). EVERYTHING else is shared and unskippable — function slicing with
  globals preserved, loop discovery + M1/M2/M3 annotation, the R2.1/R2.2 graph,
  the R4.0 legality/profitability calls, **the gate ORDER**, the real-backend
  spill/bundle probe, reporting, statistics, determinism resets, rollback.
  **A client cannot skip a gate because it never sees the pipeline.**
- **That genericity is TESTED, not asserted:** `_ToyTransform` is a client the
  framework has never seen; the suite drives it through every hook, then forces it
  to fail at match / lower / validate / dynamic_model in turn and checks each time
  that nothing commits and the scalar IR comes back untouched.
- **REUSE (headline):** 217 lines of shared pipeline serve clients of 58
  (dot-reduction) and 50 (elementwise) lines. Adding a vectorizer adds no pipeline
  logic.
- **Elementwise scope — exactly four shapes, everything else rejected:**
  `A[i]=B[i]`, `A[i]=B[i]+C[i]`, `A[i]=B[i]-C[i]`, `A[i]=B[i]*C[i]`. Enforced:
  packed arrays (stride == elem size), affine accesses, one known trip count,
  contiguous access, exactly one array store, no other array traffic. The `$v` op
  must be supported+reliable for the type — asked of the R4.0 capability layer,
  never hardcoded.
- **GOTCHA the detector forces:** `kernel_detector` calls a stored value with a
  multiply 'saxpy', so `A[i]=B[i]*C[i]` arrives as saxpy. The client claims BOTH
  'vector-add' and 'saxpy' as a PRE-FILTER and then does its own exact analysis —
  a real saxpy (`a*x[i]`) fails because its operand is not an array load. R4.0's
  detector is consumed unchanged.
- **CHECKED, NOT ASSUMED:** a displaced index cannot masquerade as contiguous.
  `analysis_iv.iv_terms` only recognises `IV` or `IV*const` — a constant
  displacement is not representable — so `a[i+1]` is not an affine term and is
  rejected at match time. A wrong answer here would have read the wrong elements.
- **WHAT ELEMENTWISE ADDS OVER R4.1 = the packed STORE.** R4.1 only ever READ
  packed data and reduced it to a scalar; elementwise writes `lanes` results back
  contiguously. On hardware an ordinary 64-bit store; the `_vec_pack` marker exists
  only so the oracle models the scatter, truncating each lane EXACTLY as the scalar
  store would (`_trunc(v, eb, unsigned=False)`) — so the two forms must leave
  byte-identical memory, which is what the differential checks.
- **Lowering:** per chunk `packed load(s) → $v <op> → packed store`, then a scalar
  remainder loop (dropped when rem == 0). A COPY needs no VALU at all — the loaded
  packed register is stored straight back and zero `$v` are emitted.
- **CORPUS (20-case suite, one pipeline, both clients):** vectorized **14/14
  expected**, **0 mismatches**, 1 rollback (narrow accumulator — the R4.1 oracle
  still bites), dynamic ops **9464 → 558 (−94.1%)**, static bundles 336→354 (+18,
  the unroll trade), 71.8 ms/kernel. Committed via dot-reduction 4, via elementwise
  10. Correctly rejected: unpacked, saxpy `a*x`, divide, `a[i+1]`, trip<2·lanes,
  narrow acc.
- **COVERAGE vs R4.1 on the same suite:** R4.1 client set alone **4/20** → R4.2
  full client set **14/20**. Conversion check: dot/reduction IDENTICAL 4/4.
- **PRODUCTION:** verified end-to-end — the elementwise kernel vectorizes, flows
  through the scalar optimizer + R3.1 SWP + R3.2 superblock unchanged, and reaches
  the mcode as 4 real `$v + $rN ($vi8) $rX $rY` (one per chunk); 0 with the
  kill-switch. Full corpus **124/124 scalar BYTE-IDENTICAL**. All 7 criteria met.
  `_r4_2_test.py` 79/79; `pipeline_crosscheck` 124/124; R3.1–R4.1 suites pass.
- **Honest limitations:** packed arrays only (general coverage 0, same binding
  constraint as R4.1); static size GROWS (+18 bundles — narrow types at 8 chunks
  pay most; a compact vector loop is the natural follow-on, as R2.8 was to R2.5);
  the remainder can dominate a small trip (N=20 at 8 lanes = 440→102, far weaker
  than a clean multiple); two operands max (`d[i]=a[i]+b[i]+c[i]` rejected, not
  decomposed); a copy emits no `$v` at all; validation is the packed IR oracle
  (no hardware sim, per policy).
- **Not done (by mandate):** matrix multiplication (R4.3), convolution, general
  loop vectorization (R4.4).

---

## R4.2.5 — Compact Vector Loop Generation  (2026-07-31) ✅ DONE

**The vector analogue of R2.8: replace fully-unrolled vector chunks with a compact
vector loop wherever that is actually smaller.** A code-generation QUALITY
milestone — not matmul, not a general vectorizer. **ONLY LOWERING CHANGED**:
`vector_pipeline.py` is byte-for-byte untouched and the client contract is
unchanged (a test asserts the interface, the `lower()` signature, and that the
pipeline source never mentions a realisation). Full report: `R4_2_5_DELIVERY.md`.

- **Added** `vector_compact_loop.py` (packed load/store at a REGISTER offset,
  `build_compact_chunk_loop`, the selector `choose_smaller`, `realisation_of`),
  `vector_dynamic.py` (the realisation-aware dynamic model, shared so the two
  clients cannot drift), `vector_compact_corpus.py`, `_r4_2_5_test.py`.
  **Modified** `vector_lowering.py` + `vector_elementwise_lowering.py` (build BOTH
  realisations, keep the smaller), both clients (`match()` now builds the plan;
  `global_base` arrives via the CONSTRUCTOR — which is why the pipeline needed no
  new parameter).
- **THE COMPACT FORM:** `for (i=0; i<chunks*lanes; i+=lanes) <packed body at
  register offset i*eb>` then the original scalar loop for the remainder. Static
  size becomes **O(1) in the trip count** instead of O(chunks).
- **THREE DESIGN DECISIONS, each reusing machinery instead of adding any:**
  (1) the loop reuses the kernel's OWN IV slot and exits with it holding exactly
  `chunks*lanes`, so **the scalar remainder needs NO modification** — it just
  resumes (the unrolled form must rewrite the IV init to `chunks*lanes`; compact
  leaves it at 0). (2) emitted in the front end's CANONICAL counted-loop shape
  with a MEMORY-slot IV — not cosmetic, because M1 IV analysis is memory-based
  (R2.7's hard-won lesson) and a register counter would be invisible; it works —
  in production **R3.2 superblock merges 5 regions inside the compact-loop program
  and still cuts bundles 71→66, 0 spills**. (3) loop-carried values stay in
  MEMORY, never in a register across the back edge, which sidesteps the entire
  R2.8 `_codegen_keeps_alive` class of bug; R2.6 can promote later.
- **CHOICE IS MEASURED, NOT ASSUMED** — both candidates compile through the real
  backend (`vector_pipeline._bundles`, the same probe the pipeline's compile gate
  uses) and the smaller wins. **Ties go to UNROLLED**: at equal IMEM the loop is
  strictly slower, so compact must EARN the switch.
  `APARA_VECTOR_REALISATION=compact|unrolled` forces either form.
  **Assuming "compact is always better" would have been WRONG** — measured
  crossover: 4 chunks → unrolled, 6 chunks → unrolled, **8 chunks → compact**.
- **CORPUS (20-case suite), R4.2 forced-unrolled → R4.2.5 measured choice:**
  coverage **14 → 14** (preserved), static bundles **354 → 320 (−9.6%)**, code
  size **24201 → 19871 chars (−17.9%)**, mismatches 0, rollbacks 1 (narrow acc,
  still caught), pipeline time 1.35→1.46 s. Scalar baseline on these kernels is
  336 — R4.2 sat ABOVE it (354), R4.2.5 sits BELOW it (320).
- **END-TO-END (14 whole kernel programs, full production optimizer): 473 → 427
  bundles (−9.7%)**, wins of −7/−13/−13/−13 on the four compacted kernels and 0
  elsewhere. Full corpus **124/124 scalar byte-identical**. 79/79 unit;
  crosscheck 124/124; R3.1–R4.2 suites pass.
- **HONEST TRADE (criterion 3 is only PARTLY met, say so):** a compact loop pays a
  compare + branch + IV update on EVERY chunk. Corpus dynamic reduction falls
  **94.1% → 89.6%**; on a compacted kernel individually it is much larger
  (`add vi16` 736→56 unrolled vs 736→155 compact). R4.2.5 buys −9.6% static for
  +76% dynamic ops ON THE KERNELS IT COMPACTS. Right trade for this machine —
  IMEM overflow is a real failure mode here (577 bundles > 0x800 words) — but it
  IS a trade, reversible with the env knob.
- **KNOWN GATE WEAKNESS (measured, not hypothetical):** the selector measures the
  vectorized IR ALONE, before the scalar optimizer / SWP / superblock run, and
  those favour straight-line code. The ranking can flip afterwards — a 4-loop
  program with one 8-chunk vectorized loop ends at **67 bundles unrolled vs 69
  compact**, even though compact has 34 FEWER instructions (119 vs 153). Net
  across the suite is still clearly positive (−46). An exact gate would need the
  production optimizer inside lowering (circular); the honest fix is a
  post-optimizer re-check, deferred.
- **Other limitations:** the crossover is EMPIRICAL (this ISA, this bundler — a
  different lane/issue width moves it), which is why it is measured per kernel
  and not hardcoded; remainder-heavy kernels stay the weak case (N=20 at 8 lanes
  = 2 chunks + 4-iteration tail, 30 bundles vs 23 scalar — nothing to compact,
  peeling/predication is the real fix); validation remains the packed IR oracle.
- **Not done (by mandate):** matrix multiplication, convolution, expression-tree
  vectorization, general loop vectorization.

---

## R4.2.6 — Post-Optimizer Size Gate & Remainder Peeling  (2026-07-31) ✅ DONE

**Closes the two weaknesses R4.2.5 documented, before R4.3.** Only lowering and
realisation SELECTION changed; `vector_pipeline.py` stays byte-for-byte untouched
(asserted by test). Both fixes were driven by measurement — and one of them
**refuted the hypothesis it started from**. Full report: `R4_2_6_DELIVERY.md`.

- **Added** `vector_size_probe.py` (measures a candidate AS PRODUCTION WOULD BUILD
  IT: tier-1 scalar optimizer + R3.2 superblock, then bundling; every pass
  imported from the same module compiler.py imports it from so it cannot drift;
  `APARA_VECTOR_FAST_PROBE=1` reverts), `vector_remainder_peel.py` (`PeelTemplate`
  + `build_peeled_tail` + `splice_peeled`), `_r4_2_6_test.py`. **Modified**
  `vector_compact_loop.py` (post-optimizer probe + acceptance MARGIN + peel-aware
  `realisation_of`), both lowerings (capture a PeelTemplate; offer
  `unrolled+peeled` and `compact+peeled` candidates), `vector_dynamic.py`
  (peel-aware), `vector_compact_corpus.py` (post-optimizer headline).
- **FIX A — the probe measures what actually ships.** R4.2.5 measured the
  vectorized IR alone, before the scalar optimizer/SWP/superblock, so the ranking
  could invert after lowering had committed. The documented case (4-loop program,
  one 8-chunk vector loop) picked compact at 69 bundles when unrolled was 67; it
  now picks **67**. Globally the post-optimizer view is very different: scalar
  baseline **276** (not 336), R4.2 **319** (not 354), R4.2.6 **273** — the
  vectorized code is now genuinely BELOW the scalar baseline.
- **FIX B — an acceptance MARGIN.** The better probe immediately exposed
  "smallest wins" as flawed: on `vector add vi8` (4 chunks) compact became smaller
  by **1 bundle of 31 (−3%)** while costing **+47 executed ops (+168%)**. A
  challenger must now beat the incumbent (always unrolled — dynamically fastest)
  by **≥10%** (`APARA_VECTOR_COMPACT_MARGIN`). Keeps the −30% 8-chunk wins,
  rejects the rounding-error ones, and restores dynamic reduction 87.1% → 89.6%.
- **FIX C — remainder peeling, AND THE HYPOTHESIS IT REFUTED.** Expected: deleting
  the tail loop removes compare+branch+IV so BOTH size and speed improve. Measured:
  at remainder 4 the peeled tail is 4 body copies (~29 instrs) where the tail LOOP
  was ~10 skeleton + one body — peeling is dynamically faster (~29 ops vs ~88) but
  statically **LARGER**. It is the MIRROR IMAGE of compaction, not an exception,
  so it clears the same margin. `add vi8 N=20` post-opt: scalar 19, unrolled 21,
  unrolled+peeled 29, compact 30, compact+peeled 25 → unrolled correctly kept.
  Peeling wins on **2 of 6** remainder kernels (reduction vi16 33→27 −18%,
  mul vi16 29→22 −24%).
- **CORRECTION TO THE R4.2.5 REPORT:** it called `add vi8 N=20` a weak case at
  "30 bundles vs 23 scalar". Both were PRE-OPTIMIZER artifacts; post-optimizer it
  is **21 vs 19** — vectorizing costs 2 bundles, not 7. Real, but far milder.
- **WHY PEELING IS SAFE:** the tail is NOT re-derived from source (that would risk
  integer-promotion / sub-word-truncation bugs — the class that made R4.1's
  narrow accumulator diverge). Each planner records a `PeelTemplate` holding the
  ORIGINAL instructions' `elem_bytes`/`unsigned`/opcode, and peeling replays those
  at constant offsets; the differential validates it like any other lowering.
- **CORPUS (post-optimizer):** coverage 14→14, bundles **319 → 273 (−14.4%)**,
  code size 24201→19871 chars (−17.9%), 0 mismatches, 1 rollback, dynamic
  reduction 89.6%. **END-TO-END (20 whole programs incl. 6 remainder): 698 → 639
  (−8.5%)** — 6 programs improved (−7/−13/−13/−13 compaction, −7/−6 peeling), 14
  unchanged. Full corpus **124/124 byte-identical**. 51/51 unit (32 realisations
  validated, 0 mismatches); crosscheck 124/124; R3.1–R4.2.5 suites pass.
- **Honest limitations:** the probe is a good predictor, NOT exact — it models
  tier 1 + superblock but not R3.1 SWP, and production may pick another tier under
  spill pressure; compile time +10% (1.40→1.53 s) since up to four candidates are
  each fully optimized. Peeling helps a minority (2/6). Small-trip remainder
  kernels remain a MILD static loss (21 vs 19) that no realisation fixes — with 2
  chunks there is nothing to compact; declining to vectorize would fix size at the
  cost of a 4.3× dynamic win, a policy call left open.

---

## R4.2.8 — Affine Access Recognition  (2026-07-31) ✅ DONE

**The affine infrastructure the vector roadmap requires — determined by SURVEY,
then built to exactly that envelope.** ANALYSIS ONLY in the R3.0/R4.0 mould:
mutates nothing, generated code byte-identical **124/124**. Nothing is wired into
the vector clients yet. Full report: `R4_2_8_DELIVERY.md`.

- **THE QUESTION:** is `invariant_base + IV*constant` sufficient for the roadmap?
  **ANSWER: NO** — four reasons, each MEASURED on emitted IR, not reasoned about:
  1. **For `elem_bytes ≥ 2` the scale is applied AFTER the index sum.**
     `C[i*8+j]` on `vi16_t` emits `(i*8 + j) * 2` = `(invariant + IV) * const`,
     NOT `invariant + IV*const`. A matcher for the latter matches NONE of the
     vi16/vi32 kernels — it would appear to work on vi8 (where ×1 is elided) then
     silently fail to generalize. Worst possible failure mode.
  2. **Operand order varies with loop order** — conv1d emits `(inv + IV*1)` with
     taps innermost and `(IV*1 + inv)` with outputs innermost.
  3. **Expressions nest** — conv2d's `in[(i+r)*8+(j+s)]` hides the IV TWO levels
     down inside `(j+s)`.
  4. **Invariance is a VALUE property, not syntactic** — the invariant
     subexpressions (`i*8`) are RECOMPUTED INSIDE the innermost body (LICM has not
     run), so they look local. Deciding by position rejected EVERY 2-D kernel in
     the first prototype. Must ask M2's question: is the slot written in the loop?
     (Same class as R1.4's value-invariant fix.)
  Plus: **`coeff == 0` must be first-class** — identifying the loop-invariant
  operand is exactly what recognises AXPY's `$replicate` scalar and GEMM row-dot's
  accumulator.
- **THE MINIMUM SUFFICIENT EXTENSION:** a bounded affine normalizer resolving
  `offset == coeff*IV + invariant` (coeff a compile-time constant) over `{+,-,*}`
  with constant folding, recursive, against ONE induction variable. Classification:
  `coeff == elem_bytes` → CONTIGUOUS, `coeff == 0` → INVARIANT, other const →
  STRIDED (stride reported), else UNKNOWN. **Subsumes** `invariant + IV*const`.
- **WHY NOT OVER-GENERAL — the key argument:** multiple varying IVs cannot arise
  *by construction*, because vectorization always targets the INNERMOST loop, so
  every enclosing IV is invariant w.r.t. it. Also rejected: symbolic coefficients
  (`B[k*N+j]` with runtime N — correctly, that IS the column-strided case),
  division/modulo/shifts/min/max, gathers. No SCEV, no polyhedral machinery.
- **SOUNDNESS BUG FOUND AND FIXED IN VALIDATION:** the first implementation
  classified `a[idx[k]]` (a GATHER) as INVARIANT, because the invariance test
  checked only the load's base slot (`idx`, never written in the loop) and ignored
  whether its OFFSET moved with the IV. That would hand a gather to a vectorizer
  as a scalar. Fixed; regression test asserts it is neither contiguous NOR
  invariant.
- **Added** `vector_affine.py` (`LoopAffineContext`, `resolve_offset`,
  `classify_access`, `classify_loop`), `_r4_2_8_test.py`, `affine_corpus.py`.
  Nothing modified.
- **RESULTS:** all **10/10** roadmap kernel shapes RESOLVED (R4.1 dot/reduction,
  R4.2 elementwise vi8+vi16, AXPY vi8+vi16, GEMM row-dot, conv1d both orders,
  conv2d); all **4/4** out-of-envelope forms rejected with reasons (column-strided
  → stride 8 NAMED, symbolic stride, gather, unpacked int). **0 disagreements**
  with today's recognizer on currently-vectorized kernels. Corpus 124 programs /
  65 innermost loops, 47 fully resolved, 15 contiguous + 493 invariant vs 26
  strided + 3 unknown accesses, generated code **identical 124/124**. 39/39 unit;
  crosscheck 124/124; R3.1–R4.2.6 suites pass.
- **Honest limitations:** NOTHING IS WIRED IN — the clients still use their own
  `_packed_array_access`; adopting this would WIDEN what they accept (row-wise
  kernels), a behaviour change belonging to the milestone that needs it. The
  corpus barely exercises it (15 contiguous accesses — almost no packed arrays),
  so the roadmap table is the evidence, not the corpus. Conservative on multi-def
  temps. `varies()` is not memoized across accesses (fine at 65 loops). Recursion
  bounded at depth 16.

---

## R4.3 — Automatic AXPY Vectorization  (2026-07-31) ✅ DONE

**The first production client of `vector_affine` (R4.2.8).** Not matrix
multiplication — that was shown impossible on this architecture; AXPY is the
transformation the affine analysis actually unlocks. `vector_pipeline`,
`vector_affine`, legality, profitability, validation, scheduler, bundler and
backend all consumed UNMODIFIED. Full report: `R4_3_DELIVERY.md`.

- **Added** `axpy_lowering.py` (`plan_axpy` + `lower_axpy`), `axpy_vectorizer.py`
  (`AxpyTransform`), `axpy_corpus.py`, `_r4_3_test.py`. **Modified**
  `elementwise_vectorizer.py` (kinds drops 'saxpy'; its standalone entry point
  registers both clients so its contract is unchanged), `dot_vectorizer.py`
  (`vectorize_all_module` registers AXPY).
- **LOWERING:** per chunk `packed load X → $v * with $replicate(a) → packed load Y
  → $v + → packed store Y`. **No new vector instruction.** `$replicate` broadcasts
  **src2**, so the scalar is passed as src2 of the multiply. The coefficient is
  materialised ONCE ahead of the vector body (slot load, or `IRAssign` for a
  literal). Both realisations are built and the R4.2.6 post-optimizer probe keeps
  the smaller — **compact wins on 8 of 10** kernels.
- **`vector_affine` IS THE ONLY AFFINE ANALYSIS** — `plan_axpy` never reads
  `desc.iv_terms` (asserted by test); store and Y/X reloads must be CONTIGUOUS,
  the coefficient INVARIANT, all via `classify_access`.
- **CORRECTION THE IMPLEMENTATION FORCED:** `classify_access` describes the
  ADDRESS pattern, not the loaded VALUE. A load of the IV's own slot sits at a
  constant offset and so looks address-invariant while its value changes every
  iteration — `Y[i] += i*X[i]` initially MATCHED and was caught only by the
  differential. The coefficient test now also requires `ctx.varies(...)` false, and
  it is rejected at match time. **A usage lesson, NOT a vector_affine limitation**
  — both facts were already exposed; the client asked the wrong one.
- **DESIGN CONSEQUENCE:** `kernel_detector` labels ANY multiply-bearing stored
  value 'saxpy' — both real AXPY and R4.2's `C[i]=A[i]*B[i]`. The pipeline
  dispatches ONE client per kind, so only one can own it. `AxpyTransform` owns
  'saxpy' and **falls back to the R4.2 elementwise planner/lowering** when its own
  match fails, so every R4.2 shape still vectorizes by the same code (R4.2 suite +
  corpus pass unchanged). No pipeline change, no detector change.
- **CORPUS (13 cases):** vectorized **10/10 expected**, **0 mismatches, 0
  rollbacks**; bundles 200→212 (the 8 exact-multiple kernels are FLAT at 20→20,
  the 2 remainder kernels grow to 22 and 30); code 14131→17110 chars; dynamic ops
  **13664 → 1804 (−86.8%)**; 104 ms/kernel. **vs R4.2.6 on the same kernels:
  0/13 → 10/13 (+10)**. Full corpus **124/124 scalar byte-identical**.
- **vi32 is SUPPORTED, not rejected** (the spec anticipated a possible rejection):
  `$v` covers vi32 — only `$dot` lacks a 32-bit form — so AXPY vi32 vectorizes at
  2 lanes.
- All 7 criteria met. 59/59 unit; crosscheck 124/124; R3.1–R4.2.8 suites and all
  corpora pass.
- **Honest limitations:** static size grows on the two remainder kernels (the
  exact-multiple ones stay flat); **no peeled remainder for AXPY** — `PeelTemplate`
  cannot express `Y[i] += a*X[i]` (two loads + invariant scalar + two ops) and
  extending it speculatively was out of scope, so the scalar tail loop is kept as
  in R4.1/R4.2 (the obvious follow-on); `a` is materialised once per vector body,
  which for the compact form is once per loop iteration (LICM may hoist it — not
  verified); validation remains the packed IR oracle.
- **NO `vector_affine` LIMITATION BLOCKED AXPY.**

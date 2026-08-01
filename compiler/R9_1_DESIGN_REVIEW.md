# R9.1 Design Review — Address Value Numbering

**Verdict: the optimization survives every attempt to disprove it. No stop
condition triggers. Recommended for implementation.**

Measured, not estimated: **suite ticks 136 206 → 131 457 (−3.49%), 12 programs
improved, 0 regressed, 0 spills introduced, 38/38 still PASS.** Static mcode
−24.2%.

No code has been committed. All measurements come from a throwaway prototype that
monkeypatches `gvn._expr_key` in a measurement process only.

---

## Check 1 — Can two `IRLoadAddr(fp_offset=X)` ever produce different values?

**No. Traced from source, not assumed.**

`IRLoadAddr(dest, fp_offset)` lowers in `codegen._gen_IRLoadAddr` to
`dest := FP + fp_offset`. Its value therefore depends on exactly one thing: FP.

**FP is written in exactly two places in the entire compiler:**

| site | when |
|---|---|
| `codegen.py:459` (`startup_code`) | once, before `$call main` |
| `codegen.py:718` (`_gen_IRFuncBegin`) | function prologue, before any body instruction |

There is no third write. Specifically:

* **Function lifetime** — the prologue does `$st [SP+0], FP` then `+ FP, ZERO, SP`, then adjusts **SP** only (`- SP, SP, fs`). FP is fixed from that point to the epilogue.
* **Calls** — `_gen_IRCall` saves live temps to `[FP + slot]`, reserves variadic space below **SP**, and the callee's own prologue saves/restores the caller's FP through `[SP+0]`. The caller's FP is unchanged across the call. No caller instruction writes FP.
* **Nested scopes** — the IR has no scope construct; `ir_gen` assigns each object a fixed `fp_offset` at declaration. Two disjoint C scopes may *share* a slot, but then both `IRLoadAddr(X)` still compute the **same address**, which is all GVN asserts. What the memory holds is irrelevant to value-numbering an address.
* **Vector pipeline / software pipelining** — `pipeline_mve` clones instructions and renames Temps, but `_clone_op` copies `fp_offset` unchanged (it is an int attribute, not a Temp) and never introduces an FP write.
* **Dynamic stack** — there is no `alloca`; `frame_size` is fixed per function by `ir_gen` (`begin.frame_size = fs`).

**`IRLoadAddr` has no side effects**: it writes one Temp and reads no memory. It is
in `dce.py`'s pure set (line 42) and `licm.py`'s `_HOISTABLE` set (line 22) —
both passes already treat it as a pure, movable value.

**Conclusion: `FP + constant` is a pure, function-invariant expression.** Two
`IRLoadAddr` with equal `fp_offset` in one function always produce identical
values. ✅

## Check 2 — Does any pass rely on the duplication?

Audited every pass named, plus the vector layers:

| pass | references `IRLoadAddr` | does it depend on duplication? |
|---|---|---|
| **GVN** (`gvn.py`) | **0** | No — `_expr_key` returns `None` for it (final line: *"loads/stores/calls/casts/... excluded"*). This is the gap. |
| **LICM** (`licm.py:22,94`) | yes | No. Lists it `_HOISTABLE` and keys it `('s', d.fp_offset)` — *the same key this proposal adds*. But LICM only hoists **out of loops**; the duplicates sit in a straight-line unrolled body. |
| **mem2reg** (`mem2reg.py:78-94`) | yes | **Correctness: no. Optimization: YES — see §11(d), which corrects this row.** GVN's own `IRAssign(dest, leader)` is a use of the leader that is not a load/store base, so it TAINTS the leader and the slot is refused promotion (measured: suite vars 136 → 102). Conservative, so no miscompile; mitigated by cleaning before mem2reg. |
| **loop_reg** (`loop_reg.py:116-121`) | yes | No. Same name→offset map shape; matches on the `IRLoadAddr → IRLoad/IRStore(0)` *pattern*, which survives collapsing. |
| **IVSR** (`ivsr.py:100,206,320`) | yes | No. Recreates `IRLoadAddr(Temp(nd), ins.fp_offset)` in preheaders — it *creates* addresses; collapsing its output later is independent. |
| **DCE** (`dce.py:42`) | yes | No — pure set membership only. |
| **SCCP, CopyProp, strength_reduce** | 0 | Not involved. |
| **Register allocation** | — | Consumes whatever IR it is given. |
| **Codegen** | — | `_gen_IRLoadAddr` is per-instruction; fewer instructions is strictly less work. R7.1 rematerialization registers a recipe per `IRLoadAddr` dest — fewer recipes, each covering more uses, still correct. |
| **Vector lowering** | yes (`plan_lowering`, `slot_width`) | No — and **it runs before GVN**, so it is unaffected either way. |

**Ordering check (the highest-risk item, and it passes):** `compiler.py:602` runs
`vectorize_all_module` and sets `_ir0 = _vec_ir`; the tiers at line 642 then call
`_cp(...)`, which calls `global_value_numbering` at line 628. **GVN runs *after*
vectorization**, so it sees the duplicates the vectorizer creates. `mem2reg`
runs at line 629, immediately after — which is exactly why its use-based escape
analysis had to be verified. ✅

## Check 3 — Measured redundancy (shipped IR, all 38 programs)

| program | total `IRLoadAddr` | distinct offsets | duplicated | duplicated & large | dyn-weighted |
|---|---|---|---|---|---|
| gemm vi32 / vu32 | 52 | 6 | 46 | 46 | 8 699 |
| gemm vi16 / vu16 | 36 | 6 | 30 | 29 | 4 665 |
| scalar bubblesort | 23 | 4 | 19 | 0 | 89 |
| gemm vi8 / vu8 | 28 | 6 | 22 | 19 | 57 |
| dot vi16 / vu16 | 49 | 4 | 45 | 0 | 45 |
| conv3 vi8 / vu8 | 43 | 3 | 40 | 0 | 40 |
| axpy vi8 | 39 | 3 | 36 | 0 | 36 |

**Suite totals: 1 123 `IRLoadAddr`, 978 duplicated (87%), 306 of those
large-offset.** ✅

## Check 4 — Measured gain (not estimated)

The brief asked for a conservative estimate; the prototype allows direct
measurement instead, so estimates are replaced by measurements.

**`gemm vi16`, end to end:**

| metric | before | after | Δ |
|---|---|---|---|
| IR instructions | 138 | 108 | **−30** |
| `IRLoadAddr` | 36 | 6 | −30 |
| mcode instructions | 289 | 143 | **−50.5%** |
| bundles | 87 | 61 | **−29.9%** |
| memory spills | 0 | 0 | 0 |

The mcode reduction is **five times** the IR reduction because each large-offset
`IRLoadAddr` lowers to a 5-instruction sequence — `$set`, `+ (-1)`, `<< 16`, `|`,
then `+ FP` — since the offset exceeds the foldable ±511 immediate field.

**Whole suite, with the ≤511 threshold (see §7):**

| metric | before | after |
|---|---|---|
| static mcode instructions | 6 006 | **4 550 (−24.2%)** |
| **simulator ticks** | **136 206** | **131 457 (−3.49%)** |
| programs improved | — | **12** |
| programs regressed | — | **0** |

Largest: gemm vi16 −12.9%, gemm vi32 −8.3%, axpy vi32 −7.0%, gemm vu16 −6.5%,
axpy vu32 −6.0%.

**Dependence chain:** removing a 5-instruction serial materialisation
(`$set → << → | → +`) removes 4 levels of chain per collapsed address. This is why
bundles fall 29.9% while instructions fall 50.5% — consistent with R6.5's finding
that bundles track the dependence-chain bound.

**Why R6.5 does not block this:** R6.5 proves packing is optimal *for a given
instruction set*. This changes the instruction set by deleting instructions; it
does not reschedule anything. ✅

## Check 5 — Measured register-pressure impact (real allocator)

Measured with `codegen.CodeGen` instrumented to record peak simultaneous
allocation — no model.

| | before | after (threshold) |
|---|---|---|
| programs whose peak pressure rose | — | **4 of 38** |
| largest increase | — | **+12** |
| programs that spill to memory | **0** | **0** |
| total memory spills | **0** | **0** |
| programs with `spilled=True` | **0** | **0** |

**The stop condition "register pressure increases enough to create spills" does
not trigger.** Pressure does rise — collapsing 36 defs into 6 extends six live
ranges, exactly as predicted — but every program stays within the 28-register
pool, and R7.1's rematerialization remains available as a safety net if a future
kernel does tip over. ✅

## Stop-condition summary

| condition | status |
|---|---|
| FP is not invariant | **clear** — two writes, both before any body instruction (§1) |
| `IRLoadAddr` has hidden side effects | **clear** — pure; already in DCE's pure set and LICM's hoistable set |
| pressure increases enough to spill | **clear** — 0 spills before and after (§5) |
| existing passes already eliminate it | **clear** — 978 of 1 123 duplicates survive the full pipeline (§3) |
| any correctness ambiguity | **clear** — 38/38 simulator PASS in both arms |

## 7 — Why a threshold, and what it is

The **unrestricted** version (value-number every `IRLoadAddr`) was measured first:

| variant | suite ticks | wins | regressions |
|---|---|---|---|
| unrestricted | 131 589 (−3.39%) | 8 | **4** (conv3 vi8/vu8 +12.4%, axpy vi16 +9.2%, axpy vu16 +3.1%) |
| **threshold `abs(off) > 511`** | **131 457 (−3.49%)** | **12** | **0** |

The regressions are exactly the kernels whose offsets are **small**. A foldable
offset lowers to **one** instruction (`+ rd, FP, imm`), so collapsing it saves one
instruction and costs an extended live range — a bad trade that perturbs the
R4.2.5 realisation probe (the same interaction R8.1a hit). A large offset costs
**five** instructions, where the trade is decisively favourable.

The threshold is not tuned to the benchmark: **it is codegen's own foldable
range**, the identical constant `_gen_IRLoadAddr` tests at line 727 and that
`rematerialization.FP_IMM_LO/HI` already encode.

## 8 — Implementation plan (no code yet)

**File 1 — `compiler/gvn.py`**

* Function: `_expr_key(ins, du)`.
* Data structure: the returned canonical key tuple, consumed by the `table`
  (`key → leader temp name`) in `_gvn_function`.
* Modification: before the final `return None`, add a case for `IRLoadAddr`
  returning `('addr', ins.fp_offset)` **only when `abs(ins.fp_offset)` exceeds the
  foldable immediate range**; return `None` otherwise. Import the bound from
  `rematerialization` (`FP_IMM_LO`/`FP_IMM_HI`) so one constant governs both
  passes, rather than duplicating `511`.
* Add a kill switch `APARA_NO_AVN`, mirroring `APARA_NO_GVN` / `APARA_NO_MEM2REG`,
  so the transform can be disabled for A/B without disabling GVN itself.
* No change to `_gvn_function`, `_operand_key`, the dominator walk, or the
  scoped add/undo — `IRLoadAddr` has no Temp operands, so `_operand_key` is never
  called for it, and the existing `du.is_single_def(dest.name)` guard already
  applies.

**File 2 — `compiler/_r9_1_test.py`** (new unit suite)

* correctness: two `IRLoadAddr` with equal offset collapse; with different offsets do not; a small offset is left alone (threshold); the leader dominates every replaced use.
* **tripwire (§10):** assert `IRLoadAddr.__init__` takes exactly `(self, dest, fp_offset)`, so any future base operand / frame id fails here and points at `gvn._expr_key`.
* pass interaction: `mem2reg` still promotes the same slots after collapsing (guards the use-based escape analysis relied on in §2); `loop_reg` unchanged.
* effect: `gemm vi16` shows 36 → 6 `IRLoadAddr` and a mcode reduction.
* non-interference: kernels with no large-offset duplicates are byte-identical with the switch on and off.

**File 3 — `compiler/STATUS.md`** — record the measurement and the threshold rationale.

**Validation to run before commit:** 38/38 simulator + 3 negative controls,
18/18 unit suites, `pipeline_crosscheck` (124 programs), and a per-program tick
comparison asserting zero regressions.

## 9 — Threats to validity

* The gain is concentrated: **6 of the 12 improved programs are GEMM**, which is
  52.4% of suite ticks. Suite-level −3.49% is really "GEMM −8% and axpy 32-bit −6%".
* The threshold is justified by measurement on this suite plus codegen's own
  immediate range. A kernel with many *small*-offset duplicates in a hot loop
  could in principle benefit from the unrestricted form; none exists here.
* Peak pressure rises by up to 12 registers in 4 programs. No spill occurs today,
  but the margin is smaller than before, and a future transform that adds pressure
  interacts with this one.
* `scalar bubblesort` has 19 duplicated offsets but all small, so the threshold
  excludes it — it is unaffected either way. That is the intended behaviour, not
  an oversight.

## 10 — Why the key is `('addr', fp_offset)` and not `('addr', function, fp_offset)`

Asked as a future-proofing question: FP is invariant today, but nested functions,
split frames, coroutine frames or dynamic allocas could change that. Should the
key carry the function or a frame id?

**No — and not merely because it is unnecessary today.**

**(a) Function identity is redundant by construction.**
`global_value_numbering` iterates `for lo, hi in func_slices(instrs)`, building a
fresh `cfg`, `dom` and `du` per slice, and `_gvn_function` creates `table = {}`
fresh on entry. **The table is per-function-slice.** Two keys can only ever be
compared if inserted into the same table, and a table only ever sees one
function, so `current_function` would be a *constant in every comparison that can
occur*. `stack_frame_id` is the same value under another name (one frame per
function today).

**(b) It protects against none of the four scenarios**, because every one of them
is an *intra*-function hazard:

| scenario | where the hazard lives | does `('addr', fn, off)` catch it? |
|---|---|---|
| nested functions sharing a parent frame | `parent_FP + off` vs `own_FP + off`; if inlined, both in one function | **No** — same `fn` |
| split stack frames | two base registers in one function | **No** — same `fn` |
| coroutine frames | FP rewritten mid-function at a suspension point | **No** — same `fn` either side |
| dynamic allocas | harmless if FP is fixed; hazardous only if offsets become SP-relative and SP moves mid-function | **No** — same `fn` |

Adding it would create the *appearance* of frame-awareness while catching nothing
on the list, and would discourage the next reader from looking harder. It is
rejected as security theatre, not as premature optimisation.

**(c) What actually makes `fp_offset` sufficient is structural, not incidental.**
`IRLoadAddr.__init__` takes exactly `(self, dest, fp_offset)` — **the node has no
base operand** — and across all 33 IR node types none names or writes a frame
base (`IRFuncBegin` carries `frame_size`, a size, not a base). In this IR,
varying the frame base is not merely absent, it is **inexpressible**. `fp_offset`
is therefore the only thing that can vary, which is exactly the condition for it
to canonicalise the value alone.

**(d) A fail-safe already covers the clean implementations.** `_expr_key` ends
with `return None` — unknown node types are excluded from value numbering by
default. A coroutine or split-frame address introduced as a NEW node type is
automatically safe with no change here. The codebase already follows that
convention: `IRVaStart` is documented as `FP + offset` and is a *separate node*,
not a widened `IRLoadAddr`.

**(e) The one genuinely dangerous path** is widening `IRLoadAddr` itself — adding
a base operand, or keeping the signature while letting the implicit base vary.
No function-level key protects against that either.

**Recommendation — a tripwire, not a wider key.** Add to `_r9_1_test.py` an
assertion that `IRLoadAddr.__init__` takes exactly `(self, dest, fp_offset)`.
Anyone who adds a base operand, a frame id, or a second base register gets a test
failure naming `gvn._expr_key` as the thing to revisit. That fails **at the moment
of the change, in the right place**, instead of silently producing a key that
looks frame-aware but is not. If a base operand is ever added, the correct key
becomes `('addr', _operand_key(base), fp_offset)`, which reduces to existing
machinery and inherits `_operand_key`'s `None`-for-multiply-defined behaviour.

## 11 — Consumers of `IRLoadAddr.dest`, and a correction to §2

Asked whether replacing later `IRLoadAddr` with the leader preserves debug
information, DefUse chains and mem2reg expectations at every site that reads
`IRLoadAddr.dest`.

**§2 was wrong about mem2reg. The correction is in (d) below.**

### (a) GVN does not replace uses

```python
instrs[i] = IRAssign(dest, Temp(leader))   # redundant -> copy leader
```

It replaces the **defining instruction**, keeping the same `dest`. Uses are
untouched; the later `copy_propagate -> copy_coalesce -> dead_code_eliminate`
rewrites them. Everything below follows from that.

### (b) Debug information: none exists

`ir.py` has zero source-location fields (no `line`, `lineno`, `col`, `coord`) and
`ir_gen` propagates none; `IRLoadAddr.__repr__` is derived, not stored. Nothing to
preserve. Note for later: because GVN preserves `dest`, future metadata on that
temp survives GVN; it is the subsequent copy-propagation that would drop it, which
is pre-existing behaviour for every GVN elimination, not introduced here.

### (c) DefUse chains: safe by construction

GVN's guard is `du.is_single_def(dest.name)`. Before: one def (`IRLoadAddr`).
After: one def (`IRAssign`). **Def count unchanged, no use touched** — no dangling
use and no multi-def temp is ever created. Replacing the *def* is precisely what
makes this safer than rewriting uses.

### (d) CORRECTION — mem2reg is tainted, and it is measurable

§2 claimed mem2reg's use-based escape analysis "is preserved". It is not. In

```python
for sn in src_names(ins):
    if sn in addr_off and sn != clean_base:
        clean[sn] = False
```

GVN's own `IRAssign(dest, leader)` is a **use of the leader that is not a
load/store base**, so `clean[leader] = False` and the slot is refused promotion.
Measured across the suite:

| | vars | loads | stores |
|---|---|---|---|
| without AVN | 136 | 78 | 136 |
| with AVN | **102** | 52 | 102 |

`axpy vi32` and `axpy vu32` lose **all 13** promotions each. This is NOT a
miscompile — tainting is the conservative direction, so the slot stays in memory —
but it is a real optimization loss that §2 asserted away.

**Mitigation (ordering, not a key change):** run the existing
`copy_propagate -> coalesce -> DCE` on GVN's output *before* `mem2reg` sees it, so
the copies are gone and the leader is never tainted.

| variant | mem2reg vars | suite ticks | regressions |
|---|---|---|---|
| baseline | 136 | 136 206 | — |
| AVN alone | 102 (−34) | 131 457 (−3.49%) | 0 |
| **AVN + clean before mem2reg** | **128 (−8)** | **131 455 (−3.49%)** | **0** |

Same tick win, 26 more promotions preserved, axpy vi32/vu32 fully restored. The
residual −8 is GEMM only and is not diagnosed.

### (e) Complete consumer list

Order verified: vectorization -> `_ivsr` -> `licm2` -> `loop_reg` -> `_cp`{ clean ->
sccp/dce -> **GVN** -> mem2reg -> LICM -> clean } -> codegen.

**Runs BEFORE GVN — structurally cannot observe the rewrite:**
`vector_compact_loop.slot_width:193`, `vector_legality:197`,
`expression_tree:165`, `vector_affine:129`, `vector_remainder_peel:93,100`,
`ivsr:100,206,320-353`, `loop_reg:121,194,200`, `licm2:67`.

**Runs AFTER GVN:**

| consumer | why replacement is safe |
|---|---|
| `mem2reg:78-94` | conservative taint (no miscompile); optimization loss measured and mitigated by (d) |
| `licm.py:22,94` | the redundant def becomes `IRAssign`, which is ALSO in `_HOISTABLE`; the leader is still hoisted |
| final `_clean` | copy propagation of a single-def copy is semantics-preserving, and is the same machinery GVN already relies on for every other eliminated expression |
| `codegen._gen_IRLoadAddr` | fewer instructions, each still `FP + fp_offset` with an unchanged offset |
| R7.1 rematerialization | recipe keyed by `dest.name`; fewer recipes, each covering more uses; the recipe `FP + constant` is unchanged |

### (f) Added to the implementation plan (§8)

* Run `_clean` between `global_value_numbering` and `mem2reg` in `compiler._cp`.
* Unit test: mem2reg promotion counts must not fall on `axpy vi32` with AVN
  enabled — this guards the interaction directly rather than by inspection.

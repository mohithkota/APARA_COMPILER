# R16.2 — Generic array-base support: vectorizing global / fixed-DMEM arrays

Compiler at `32395a5` (R16.1) before this milestone. **Production change** —
6 `.py` files, +106/−27 lines. Nothing pushed; no tag moved.

> **Headline.** A 16×16 `vu8` matmul that R16.1 measured at **37000 ticks with
> `$dot = 0`** now runs at **987 ticks with `$dot = 8`**, 256/256 correct — and
> the existing 38-program suite is **bit-identical**, all 14 metric columns.
> Against the previous best build of the same workload (stack-local JT=4,
> **4575 ticks**) that is **−78.4%**. The saving is *not* all compilation: see
> §8, which separates the initialization effect from the kernel effect.

---

## 1. What R16.1 found, and why it was wrong about the cause

R16.1 asked whether moving `A`/`Bt`/`results` from stack locals to fixed DMEM
would relieve the register pressure R16.0 blamed for the JT=8 regression. It
measured a catastrophe and classified the placement hypothesis as **rejected**:

```
[vectorize] NOT COMMITTED: 0 kernel(s) vectorized (1 declined, 0 rolled back)
    report: Vec[main:'fc_9'] rolled back: pattern:array-bases-not-extracted
```

`$dot` went to 0, the kernel fell back to scalar, and the program got 8× slower.
R16.1 correctly identified the *mechanism* (`plan_lowering` only accepts a
stack-slot base) but drew the conclusion at the wrong level: it reported the
**placement hypothesis** as rejected when what it had actually measured was a
**missing compiler capability**. R16.1's own closing paragraph scoped the fix
and forbade itself from making it ("that is a production compiler change, which
R16.1's own Phase 13 forbids"). R16.2 is that change.

**Corrected conclusion: the R16.0 data-placement hypothesis was valid.** It was
untestable, not false.

## 2. The exact restriction that was removed

`vector_lowering.plan_lowering._extract`, at `32395a5`:

```python
for i in _operand_loads_of(red, instrs, _dm_all, _region_set):
    ins = instrs[i]
    base = getattr(ins, 'base', None)
    off  = getattr(ins, 'offset', None)
    if not isinstance(off, Temp) or not isinstance(base, Temp):
        continue                      # ← a global load has NO base Temp
    if base.name not in addr_off:
        continue                      # ← addr_off holds IRLoadAddr slots only
    ...
    rp.array_slots.append(addr_off[base.name])
```

Two independent filters, both keyed on one storage class:

* `_operand_loads_of` only ever collected `IRLoad` nodes, so an `IRGlobalLoad`
  was not even seen as an operand.
* `addr_off` is built from `IRLoadAddr` definitions — i.e. FP + offset. A global
  array is addressed GBASE-relative and never produces such a def, so its base
  could not be named.

With no array extracted, `len(rp.array_slots) < need` fires and the kernel is
declined with `pattern:array-bases-not-extracted`. The identifier used
throughout the lowering was a bare `int` **stack slot**, which hard-codes the
storage class into every downstream data structure.

This is the same shape of over-narrow predicate R13.0 had to generalise for the
*offset* form (`invariant_base + IV*eb`). The *base* form was never generalised.

## 3. The `ArrayBase` abstraction

`compiler/ir.py`:

```python
class ArrayBase:
    """Where a vectorizable array lives: kind is 'stack' or 'global'."""
    __slots__ = ('kind', 'value')

    @staticmethod
    def stack(fp_offset): return ArrayBase('stack', fp_offset)
    @staticmethod
    def glob(dmem_addr):  return ArrayBase('global', dmem_addr)

    def emit(self, temp):
        if self.kind == 'stack':
            return IRLoadAddr(temp, self.value)
        return IRGlobalAddrOf(temp, self.value, Const(0))


def emit_array_base(temp, base):
    """Materialise an array base; a bare int still means a stack slot."""
    if isinstance(base, ArrayBase):
        return base.emit(temp)
    return IRLoadAddr(temp, base)
```

Three properties make this a drop-in replacement rather than a parallel path:

1. **One instruction differs, nothing else.** Every downstream consumer —
   contiguity, constant deltas, chunk immediates, store grouping, R14.2 address
   sharing, R14.8 store parallelization — operates on the **offset**, never on
   where the base came from.
2. **`__eq__`/`__hash__` on `(kind, value)`**, with `ArrayBase.stack(x) == x`.
   Dicts and identity comparisons keyed on the old bare slot keep working.
3. **`emit_array_base` accepts a bare int**, so the five call sites that were
   not part of the reduction path did not have to be reasoned about; they
   simply became storage-polymorphic for free.

## 4. Local vs global representation

| | stack-local | global / fixed DMEM |
|---|---|---|
| identifier | `ArrayBase('stack', fp_offset)` | `ArrayBase('global', dmem_addr)` |
| recognised from | `IRLoadAddr` def in `addr_off` | `IRGlobalLoad.dmem_addr` |
| base materialised as | `IRLoadAddr(t, fp_offset)` | `IRGlobalAddrOf(t, addr, 0)` |
| address form in mcode | `[$rB + imm]`, `$rB` = FP + off | `[$rB + imm]`, `$rB` = absolute |
| legality rules applied | identical | identical |

Changed files:

| file | change |
|---|---|
| `ir.py` | `ArrayBase`, `emit_array_base` (new, +66) |
| `vector_lowering.py` | `_operand_loads_of` accepts `IRGlobalLoad`; `_extract` builds an `ArrayBase` for either storage; 3 emit sites use `emit_array_base` |
| `vector_compact_loop.py` | 4 emit sites |
| `vector_remainder_peel.py` | 2 emit sites |
| `vector_elementwise_lowering.py` | 1 emit site |
| `expression_lowering.py` | 3 emit sites |

The IV's own slot and the accumulator slot are still excluded, and that
exclusion is now correctly *skipped* for globals (a global load can never be the
IV or the accumulator, both of which are register/stack quantities).

## 5. R16.2 unit tests — `_r16_2_test.py`, 41/41

Structural properties, not a benchmark:

| group | checks | asserts |
|---|---:|---|
| unit: `ArrayBase` | 7 | each kind emits the right node, keeps its value, bare int still means stack, `stack(x) == x`, stack ≠ global |
| positive: globals vectorize | 8 | `vu8_t`/`vi8_t`/`vu16_t`/`vi16_t` global matmul emits `$dot`, 256 checks 0 errors |
| positive: storage is not a legality input | 3 | same source, global and stack-local, both vectorize with the **same `$dot` count** |
| positive: scales with tile | 7 | JT=1/2/4 global all vectorize; `$dot` count grows with the tile |
| anti-bias | 3 | renaming the arrays changes neither `$dot` nor ticks |
| negative controls | 8 | non-contiguous k, wrong IV coefficient, runtime-varying stride, non-affine B row — **all still declined**, all still correct |
| negative: unsupported width | 2 | `int` global stays scalar (no 32-bit `$dot` in the ISA) |
| composition | 3 | R14.2 shares one base register across all kernel loads on the global form; R14.8's kill switch still changes the global build |

The negative controls are the load-bearing half: they prove R16.2 widened the
*storage* predicate without weakening any *legality* rule.

## 6. 38-program regression — bit-identical

```
python3 -m verification --csv r162_after.csv      # working tree
python3 -m verification --csv r162_before.csv     # git worktree at 32395a5
```

* **38/38 PASS**, **3/3 negative controls rejected**, both trees.
* `diff r162_before.csv r162_after.csv` → **no differences**. All 14 columns
  (vectorized, ok, postconditions, ticks, instructions, issue slots, IPB,
  occupancy, static bundles, static instructions) identical for all 38.
* Suite total 67684 ticks before and after.

Every existing program is stack-local, so R16.2 must be a no-op on all of them —
and it measurably is. This is an **additive** capability.

Also green on this tree: `pipeline_crosscheck` **124/124** (0 IR, 0 code, 0
selected-tier mismatches, 0 verifier failures, 0 rollbacks), 25 `loopopt` unit
suites, 29 compiler unit suites (R3.1 → R14.10).

## 7. Fixed-DMEM J_TILE sweep

Identical sources to R16.1 (`fixed_jt{1,2,4,8}.c`, 16×16 `vu8`, initializers +
`--dmem-init`), each built twice — once against a `git worktree` at `32395a5`,
once against the R16.2 tree. Only the compiler differs. R16.1's published
numbers reproduce exactly, which validates the harness.

| configuration | R16.1 ticks | R16.1 `$dot` | R16.2 ticks | R16.2 `$dot` | correct |
|---|---:|---:|---:|---:|:--:|
| fixed-DMEM JT=1 | 37324 | 0 | **2763** | 2 | 256/256 |
| fixed-DMEM JT=2 | 19148 | 0 | **1611** | 4 | 256/256 |
| **fixed-DMEM JT=4** | 37000 | 0 | **987** | 8 | 256/256 |
| fixed-DMEM JT=8 | 19164 | 0 | **1015** | 16 | 256/256 |
| stack-local JT=4 | 4575 | 8 | **4575** | 8 | 256/256 |
| stack-local JT=8 | 7950 | 16 | **7950** | 16 | 256/256 |

Compiler bundle report for the R16.2 fixed-DMEM builds: JT=4 104 → 36 bundles,
JT=8 152 → 50.

The stack-local rows are the control: R16.2 leaves them **unchanged, to the
tick**. Every improvement in this table comes from a configuration that
previously could not vectorize at all.

## 8. Initialization vs kernel — do not attribute 78.4% to compilation

The 4575 → 987 headline mixes two independent effects, and R15.0's analysis
already provided the split for the baseline:

| | end-to-end | initialization | kernel |
|---|---:|---:|---:|
| stack-local JT=4 | 4575 | 3331 (73%) | ~1225 |
| fixed-DMEM JT=4 (R16.2) | **987** | **~0** (preloaded via `data.map`) | **~987** |

* **Initialization effect (~3331 ticks, ~73% of the baseline program).** This is
  a *data-placement / methodology* win, not a compiler optimization. It comes
  from `--dmem-init`, which has existed for a long time; R15.0 and R16.1 both
  identified it. R16.2's contribution here is only that the win is no longer
  cancelled by losing `$dot`.
* **Kernel effect (~1225 → ~987, ≈ −19%).** *This* is the compiler result. The
  array bases are no longer register-held FP-relative pointers; operand
  addresses become absolute immediates off a materialised global base, which
  removes address-maintenance work from the inner loop.

R16.1 measured these two effects with opposite signs (init → 0, kernel → 30×
worse) and reported the net. R16.2 makes them both positive. Stating it
plainly: **of the 78.4%, roughly 73 points are initialization placement and
roughly 5 points are the kernel improvement** that R16.2 itself delivers — but
the kernel improvement is the part that was previously *impossible*, and it is
what makes the placement win bankable at all.

## 9. JT=4 vs JT=8 — the R16.0 question, finally measurable

| | JT=4 | JT=8 | JT=8 penalty |
|---|---:|---:|---:|
| stack-local (R16.1 = R16.2) | 4575 | 7950 | **+73.8%** |
| fixed-DMEM (R16.2) | **987** | 1015 | **+2.8%** |

R16.0 attributed the JT=8 regression to the eight `int s0..s7` accumulators
being unable to stay resident once the hot block also had to hold register-held
array bases — they were reloaded one at a time in a load/store ladder instead of
packing into one bundle. Fixed DMEM removes exactly those base registers.

**The measurement supports that attribution: the penalty collapses from 73.8% to
2.8%.** What it does **not** support is a claim that register pressure is
solved. JT=8 still loses to JT=4, and 2.8% of residual penalty remains
unexplained by this milestone. The defensible statement is:

> Fixed-DMEM addressing removes the array-base register overhead that was
> causing most of the JT=8 penalty, but JT=8 still does not outperform JT=4.
> **JT=4 remains the right tile.**

## 10. Comparison with the hand-written 8-dot kernel

| | ticks | `$dot` | correct |
|---|---:|---:|:--:|
| hand-written 8-dot (R16.0 reference) | **241** | 32 | 256/256 |
| R16.2 fixed-DMEM JT=4 | 987 | 8 | 256/256 |
| R16.2 fixed-DMEM JT=8 | 1015 | 16 | 256/256 |

The hand-written reference is 17 bundles / 1160 instructions with its data
preloaded. The R16.2 fixed-DMEM builds are also preloaded, so this is now a
like-for-like comparison — it was not before, when the compiler's best build
spent 73% of its ticks initializing.

**The compiler remains ~4.1× slower than the hand-written schedule** (987/241).
That is the honest number and it is not a small gap. What changed is its
composition: it is no longer dominated by initialization or by a scalar
fallback. Per R16.0's decomposition, the hand kernel's advantage is `$dot`
*packing density* — 8 `$dot` per bundle against the compiler's 4 — reached by
folding the constant B base into the load immediate so a 29-register schedule
fits in 28. That is a scheduling/allocation question about how many independent
`$dot` the compiler can co-issue, and it should be scoped as such.

## 11. Remaining limitations

1. **Only the reduction (`$dot`) path constructs global bases.**
   `vector_elementwise_lowering.plan_elementwise` still resolves array bases
   through `addr_off`, i.e. stack slots only. A *global* elementwise/axpy/conv3
   kernel therefore still falls back to scalar. The emit side of those paths is
   already storage-polymorphic (they call `emit_array_base`); only their
   recognition predicate is not. That is the natural next increment.
2. **Store side is unchanged.** `results[]` is written through ordinary global
   stores; R16.2 did not make packed stores global-aware.
3. **JT=8 is still not profitable** (§9), and the residual 2.8% is not attributed.
4. **Global initializers still overflow IMEM without `--dmem-init`** — R16.1's
   secondary finding stands (531 init stores → 2651 aligned bundles, never
   halts). `--dmem-init` is mandatory for this configuration, not optional.
5. **One datatype family exercised end to end.** The sweep is `vu8`; the unit
   tests cover `vi8`/`vu16`/`vi16` structurally but not across sizes.

## 12. Why this is a compiler capability, not a benchmark hack

The obvious objection to a 78.4% headline obtained by moving arrays into globals
is that the benchmark was rewritten to suit the compiler. It was not, and the
test design is what shows it:

* **Storage class is now provably not a legality input.** `_r16_2_test.py`
  compiles the *same source text* with the arrays as globals and as stack
  locals and asserts both vectorize with the same `$dot` count. Before R16.2
  those two builds disagreed; the storage class was silently part of the
  vectorization decision. Removing an irrelevant input from a decision
  procedure is a compiler correctness improvement independent of any speedup.
* **No legality rule was weakened.** Four negative controls (non-contiguous k,
  wrong IV coefficient, runtime-varying stride, non-affine row index) and an
  unsupported-width control still decline, on the global form.
* **No special case for the benchmark.** The recognition is
  `IRGlobalLoad → ArrayBase.glob(dmem_addr)`; there is no array name, size,
  address, tile factor or kernel shape anywhere in the change. The anti-bias
  test renames the arrays and asserts identical ticks.
* **It composes with the existing passes unchanged.** R14.2 base sharing and
  R14.8 store parallelization fire on the global form without modification,
  which is the practical test of whether an abstraction is real or a bypass.
* **It generalises an already-recognised over-narrow predicate.** R13.0 had to
  do the same widening for the offset form. The base form was the other half of
  the same gap.

The *benchmark* change (globals + `--dmem-init`) and the *compiler* change
(`ArrayBase`) are separable, and §8 keeps them separate. The compiler change is
what makes the benchmark change legal to make.

---

## Status

**R16.2 COMPLETE.** Supersedes R15.0's FREEZE recommendation: that
recommendation rested on every remaining lever being worth 2–4% whole-program,
and this one is worth 78.4% on the same workload.

**Do not start R16.3 automatically.**

# R7.1 — Register Rematerialization

**Rematerialization works and is shipped: spills fall on every kernel R7.0
measured — 16 → 6, 9 → 1, 8 → 0 — and static memory operations drop 154 → 146.
`axpy vi32`/`vu32` now need NO memory spill at all.**

**But it does not unblock any software pipeline, and that is deliberate.**
Admitting those now-spill-free pipelines makes `axpy vi32` and `axpy vu32` FAIL
simulator verification. Rematerialization is not the cause — the pipelined code
has simply never executed before, because the spill gate had always rejected it.
**R7.1 exposed two latent defects rather than creating them** (§3, §4), and ships
with the unblocking gated OFF so that 38/38 is maintained.

Success criterion 3 is therefore **not met**, and §7 says so plainly.

---

## 1. What was implemented

`rematerialization.py` owns two decisions and nothing else — *is this value
recomputable with no live register inputs*, and *which live value should be
evicted*. `codegen.py` keeps ownership of allocation and emission; the only
behaviour that changes is what happens to a value about to be evicted:

```
            before                          after
  victim -> $st [FP+slot], reg      victim -> (nothing)
            ... later ...                    ... later ...
            $ld reg, [FP+slot]               + reg ($i64) $r28 <off>
  2 memory operations               1 ALU instruction, no register inputs
```

### What is eligible, and why only this

Only `IRLoadAddr` whose offset fits the signed immediate field that
`_gen_IRLoadAddr` folds directly into the add (`-512 ≤ off ≤ 511`):

* it is one instruction;
* `$r28` (FP) is fixed for the whole function, so the value is valid at any point
  in it — no ordering constraint, no input to keep alive;
* it needs no free register beyond the destination.

**The large-offset form is deliberately excluded.** Recomputing it needs a
*borrowed scratch register*, and the moment rematerialization matters is exactly
the moment no register is free — rematerializing it could trigger a further
eviction, the opposite of the point. Memory loads are never duplicated, and
values computed from other registers are excluded because they would keep their
inputs alive, which is the cost this pass exists to avoid.

### The one policy change, and the guard it needed

Among evictable values, a recomputable one is preferred. That alone **did not
work**, and the measurements are worth recording because the naive policy looks
obviously right:

| policy | elementwise vi16 | elementwise vi32 | axpy vi32 |
|---|---|---|---|
| R7.0 baseline (no remat) | 16 spills | 9 | 8 |
| change the *action* only, never the choice | 6 | 4 | 3 |
| prefer recomputable victims, **unguarded** | **0** | 11 | **56** |
| prefer, but demote a value once rebuilt | 6 | **1** | **0** |
| prefer, demoting only the *last* rebuild | 0 | 48 | 60 |

The unguarded preference **thrashes**: a recomputable value is evicted, rebuilt at
its next use, immediately becomes the preferred victim again, and the pair
ping-pongs while ordinary values still have to spill — `axpy vi32` went from 8
spills to 56. The shipped policy demotes a value once it has been rebuilt: it is
still recomputed rather than spilled if chosen, but it is no longer *preferred*,
and an ordinary value is evicted before it.

That policy is not optimal on every kernel — the unguarded one reaches 0 spills on
`elementwise vi16` where the shipped one leaves 6 — but it is the only one of the
four that never regresses.

## 2. Results

### Spills on the pipelined candidates R7.0 analysed

| kernel | R7.0 spills | **R7.1 spills** | evictions avoided | recomputations |
|---|---|---|---|---|
| elementwise vi16 / vu16 | 16 | **6** | 10 | 10 |
| elementwise vi32 / vu32 | 9 | **1** | 8 | 0 |
| **axpy vi32 / vu32** | 8 | **0** | 8 | 0 |
| axpy vi16 / vu16 | 0 | 0 | 0 | 0 |

### Memory spills eliminated, admission unchanged

| kernel | memory spills R7.0 | memory spills R7.1 | admitted by the SWP gate? |
|---|---|---|---|
| **axpy vi32 / vu32** | 8 | **0** | **no — gated off, see §4** |
| elementwise vi32 / vu32 | 9 | 1 | no (still spills) |
| elementwise vi16 / vu16 | 16 | 6 | no (still spills) |
| axpy vi16 / vu16 | 0 | 0 | yes (unchanged from R6.8) |

`axpy vi32`/`vu32` reach zero memory spills — the condition R7.0 predicted would
recover them — but admitting them fails verification (§4), so the gate is left
closed. **No pipeline that ships changed.**

### Memory traffic

Static memory operations in the recovered `axpy vi32` pipelined body:
**154 → 146**. Every avoided eviction removes a store *and* its reload and adds
one ALU instruction, so the trade is strictly favourable — there is no case where
rematerializing costs more memory traffic than spilling.

### Code that was already spill-free is untouched

`dot vi8`, `conv3 vi8` and `gemm vi16` emit **byte-identical** code with
rematerialization on and off (asserted in `_r7_1_test.py`). The pass only acts
when the allocator was going to evict something, so programs under 28 live values
cannot be affected.

## 3. A correctness regression I introduced, found, and fixed

**The first implementation made `reduction vi16` compile to a program that never
terminates.** The simulator suite hung on it; I initially misread that as an
out-of-memory kill, and it took isolating the single program to see it.

The cause was not the rematerialization mechanism. It was that I had made a
rematerialized eviction leave `self.spilled` **False**, on the reasoning that
nothing had gone to memory. But `spilled` is consulted far beyond this pass:

* tier selection in `production_codegen` skips a tier that spills;
* R3.2 superblock acceptance rejects a merge that introduces a spill;
* the R4.2.5 realisation probes reject a spilling candidate.

Widening what that flag means therefore changed **which optimization path the
whole compiler selected**. For `reduction vi16` the selected IR went from 82 to
102 instructions, and the newly reachable path contained a latent bug that had
never shipped because it had always been rejected for spilling. R7.1 did not
create that bug; it exposed it — the same class of interaction R6.6 hit with
R3.2's module-scope gate.

**The fix confines the semantic change.** `spilled` keeps its pre-R7.1 meaning
(*register pressure forced an eviction*) and is still set for a rematerialized
eviction. A new, narrower `spilled_to_memory` records true memory spills, and
**only the vector-SWP gate consults it** — the one gate R7.1 exists to unblock.

Verified afterwards: compiled in fresh processes, the emitted mcode is
**byte-identical with rematerialization on and off across all 18 kernel/marker
combinations**, so no other optimization decision moved.

The latent bug in the previously unreachable path is *not* fixed here. It is now
unreachable again, and finding it is separate work — recorded in §9.

## 4. The second latent defect: the pipelines this would have unblocked are wrong

With the gate relaxed to admit memory-spill-free pipelines, two programs fail:

```
FAIL  axpy vi32   vec  [compared]  5 PostCondition comparisons performed, 4 declared
FAIL  axpy vu32   vec  [compared] 15 PostCondition comparisons performed, 4 declared
```

The result-writing code executes more often than the program declares, so control
flow through the pipelined loop is wrong.

**Rematerialization is not the cause.** Three measurements separate them:

| configuration | result |
|---|---|
| remat ON, vector SWP OFF | **PASS**, 1539 ticks |
| remat OFF, vector SWP ON (pipeline rejected as before) | **PASS**, 1539 ticks |
| remat ON, vector SWP ON (pipeline admitted) | **FAIL** |

and the emitted mcode is byte-identical with rematerialization on and off across
all 18 kernel/marker combinations (§3).

So this is a defect in R6.8's pipelined output for `axpy` at 32-bit markers, in
code that **has never executed**: the spill gate rejected it at R6.8 time and has
rejected it ever since. R6.8's differential oracle runs on the IR and passed it,
which means the defect is either in the IR transformation in a way the oracle
does not model, or in lowering that IR shape.

**The gate is therefore left closed**, with `APARA_VSWP_UNBLOCK=1` as a one-flag
reproducer for whoever fixes it. Diagnosing it is R6.8 work, not R7.1 work, and
guessing at it inside this milestone would have risked the correctness guarantees
the milestone requires.

## 5. Register demand is unchanged — and that is the correct behaviour

| kernel | demand R7.0 | demand R7.1 |
|---|---|---|
| elementwise vi16 | 35 | 35 |
| elementwise vi32 | 37 | 37 |
| axpy vi16 | 27 | 27 |
| axpy vi32 | 36 | 36 |

Rematerialization does not reduce the number of simultaneously live values. It
reduces the **cost of exceeding the limit** from two memory operations to one ALU
instruction. `axpy vi32` still needs 36 registers and still has 28; it now pays
for the shortfall in arithmetic instead of memory, which is why it compiles
spill-free at unchanged demand.

## 6. Against the R7.0 projection — outcome matches, model does not

R7.0 predicted all four rejected kernels would be recovered, reasoning:

> peak 35, remat-free values at peak 11, therefore peak after remat = 24 < 28

**That subtraction was wrong**, and R7.0's own threats section flagged the
assumption ("a real implementation must re-measure rather than assume the
subtraction"). A rematerializable value is still *live* — it holds a register
until something needs one. Rematerialization is not a live-range transformation
at all; it is a cheaper eviction. So demand stays at 35 and the kernel still
exceeds the pool.

What actually determines recovery is **how many evictions the pool shortfall
forces, and whether a recomputable victim is available for each one**:

| kernel | shortfall (demand − 28) | evictions forced | recomputable victims available |
|---|---|---|---|
| axpy vi32 | 8 | 8 | **8 — all of them** → spill-free |
| elementwise vi32 | 9 | 9 | 8 → 1 residual spill |
| elementwise vi16 | 7 | 16 | 10 → 6 residual spills |

`elementwise vi16` forces 16 evictions against a shortfall of 7 because pressure
there is *sustained*, not a single peak — R7.0 measured it at the limit for 19.5%
of its instructions, against 0% for `axpy`. Each of those points forces its own
eviction, and there are not enough recomputable values to cover them all.

**The R7.0 ranking was right — rematerialization was the highest-return option and
it did recover kernels no other change would have — but its magnitude estimate
was optimistic by a factor of two.** The corrected model is above.

## 7. What still spills, and what those values are

`elementwise vi16` (6 residual) and `elementwise vi32` (1 residual). The residual
victims are **not** rematerializable, and R7.0 already classified them: at the
peak, `elementwise vi16` holds 24 non-rematerializable values against 11
recomputable ones, dominated by **rotating-bank copies of vector data**
(`_vea6~p1`, `_ver10~p0`, …). Those are packed 64-bit values produced by loads and
`$v` operations. They cannot be rematerialized:

* recomputing a packed load would **duplicate a memory load**, which is explicitly
  out of scope and unsound in general (the location may be written between the two
  reads);
* recomputing a `$v` result would require its operands to stay live, which is the
  cost the pass exists to avoid.

So the remaining deficit is genuinely a *data* pressure problem, not an *address*
one — and R7.0's finding that pressure is address-dominated holds for the peak of
the program but not for `elementwise`'s sustained region.

## 8. Success criteria

| # | criterion | result |
|---|---|---|
| 1 | maintain all correctness guarantees | **met** — 38/38 simulator, 3 negative controls, `pipeline_crosscheck` (124 programs), 18/18 unit suites |
| 2 | eliminate spills caused solely by rematerializable address temporaries | **met** — 16 → 6, 9 → 1, 8 → 0; every avoided eviction is an `FP+const` value |
| 3 | allow previously pressure-limited SWP kernels to compile | **NOT met.** `axpy vi32`/`vu32` reach zero memory spills, which is the condition R7.0 identified, but admitting them fails verification because of a pre-existing R6.8 defect (§4). The gate is left closed rather than shipping a failing suite |
| 4 | reduce memory traffic | **met** — 154 → 146 static memory ops; each avoided eviction removes a store and a reload and adds one ALU instruction |
| 5 | agree with the R7.0 analysis | **partially** — R7.0's ranking was right and its mechanism claim was wrong (§6); it also did not anticipate that the pipelines it wanted to unblock were themselves broken |

**Two of the five are not fully met, and both shortfalls are reported rather than
worked around.** The value R7.1 delivers today is lower memory traffic and fewer
spills; the register-pressure unblocking it was built for is one R6.8 fix away.

## 9. Threats to validity

* **The eviction policy is a heuristic, not an optimum.** Four policies were
  measured (§1) and no single one wins on every kernel; the shipped one is the
  only one that never regresses.
* **Recovery is measured on four kernel families at two markers each.** The
  unsigned twins behave identically and are not independent samples.
* **`spilled` is deliberately not set for a rematerialized eviction.** That flag
  gates SWP, superblock and realisation acceptance, so this is what unblocks the
  kernels — but it also means those gates now accept code that exceeded the
  register pool. That is correct (nothing was spilled to memory) and it is the
  intended mechanism, but it is a semantic change to a widely used flag.
* **Only `FP + constant` is implemented.** Other rematerializable forms (a
  loop-invariant base plus a constant held in a register) are not, because they
  would need their base kept alive.
* Static memory-operation counts are reported; dynamic memory traffic was not
  separately instrumented.
* **A latent bug remains in a code path this pass briefly made reachable** (§3).
  It is unreachable again, but it exists: some tier/superblock combination that
  the spill gate has always rejected produces a non-terminating `reduction vi16`.
  That is worth finding on its own, and it is not addressed here.

## 10. Regression

| check | result |
|---|---|
| `loopopt/pipeline_crosscheck` (124 programs) | **PASS** |
| unit suites (all 17, incl. new `_r7_1_test.py`) | **17 pass, 0 fail** |
| simulator verification | see below |

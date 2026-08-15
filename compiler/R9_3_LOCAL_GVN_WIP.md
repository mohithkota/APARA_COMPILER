# R9.3 lead #1 — Local Value Numbering for multi-def operands (MEASURED, NOT COMMITTED)

Follows `R9_3_RAW_AND_REDUNDANCY_ANALYSIS.md` §6 lead 1. Baseline: `50e2b67`
(R9.2). The change is implemented in `gvn.py` and fully validated; it is **not
committed**, because it buys **exactly zero ticks**.

---

## 1. R9.3's stated premise was wrong, and the real cause is phase order

R9.3 §6 said: *"Find out why this loop's IV is not register-promoted. R2.6
promotion already exists; one temp instead of nine makes GVN collapse all nine
`*16` with zero new machinery."*

**The IV IS register-promoted.** A fresh compile of `matmul16.c` on the current
tree shows the hot block `fb_10` already using one promoted counter, `_lr124`:

```
fb_10:                        <- 256 executions
    _vgo20 = _lr124 << 4
    _vgo37 = _lr124 << 4      <- identical, same block, nothing between
    _vgo54 = _lr124 << 4
    _vgo71 = _lr124 << 4
    ...
    _lr124 = _lr124 + 1       <- the only redefinition, AFTER all four
```

(R9.3 measured the stored `matmul16/vec2` build directory, which is stale.
Compiling from source is what corrected this.)

So the nine-distinct-temps story does not apply, and **collapsing them still did
not happen** — for a different reason:

`gvn._operand_key` rejects **any** multiply-defined Temp operand
(`du.is_multi_def(...) -> None`). A register-promoted loop counter is multi-def
**by construction** — initialised before the loop, incremented inside it — so
R2.6 promotion, which is supposed to help, is precisely what makes every
expression reading the counter invisible to GVN. Promotion and value numbering
are working against each other.

## 2. The change

A second, **block-local** table in `_gvn_function` holding expressions the
global rule refuses. Within one basic block, operand stability is decidable
without dataflow: the two computations are separated by a straight line, so the
operands are unchanged iff nothing between them redefines them. Classic local
CSE, sound on non-SSA IR.

* the global dominator-scoped table is untouched — single-def operands behave
  exactly as before;
* an entry dies as soon as any instruction redefines one of its operands or its
  leader, computed from `ir_utils.dest_names` (the only exhaustive answer:
  `IRLoadWide` writes several temps through `dests` and has no `dest` at all);
* an instruction that overwrites one of its own operands (`x = x + 1`) is never
  published — the key names the operand, but the name holds a different value
  afterwards;
* kill switch `APARA_NO_LOCAL_GVN`.

**TRAP, cost me an hour:** the order must be **lookup -> kill -> publish**.
Publishing before the kill makes every entry evict *itself* (its own destination
is in its own kill set) and yields a completely convincing `local_eliminated=0`,
indistinguishable from "there is no redundancy". This is the same trap
`R9_3_RAW_AND_REDUNDANCY_ANALYSIS.md` warns about for the mcode CSE script; the
warning was right and I walked into it anyway. The comment in `gvn.py` now says
ORDER IS LOAD-BEARING.

## 3. It works, and it is worth nothing

matmul16's four hot-loop shifts collapse to one (9 scale expressions -> 6).

| 38-program suite | R9.2 | +local GVN | delta |
|---|---|---|---|
| **simulator ticks** | **131 424** | **131 424** | **+0 (0.000%)** |
| dynamic instructions | 165 911 | 158 231 | −7 680 (−4.63%) |
| static bundles | 2 058 | 2 058 | +0 |
| static instructions | 5 007 | 4 977 | −30 |

**0 programs improved, 0 regressed, 38 unchanged.** Every kernel that changes is
a GEMM: vi32/vu32 −2 304 dynamic instructions each, vi16/vu16 −1 280, vi8/vu8
−256. matmul16 alone: 14 258 -> 12 978 dynamic instructions (−9.0%), ticks
10 099 -> 10 099.

The 124-program corpus (`testing/`, `new_isa_tests/`, `demo_prof/`,
`isa_coverage_tests/`) is **byte-identical** — 13 564 instructions and 6 996
bundles in both arms, 0 programs changed, no opcode count moved. The entire
effect is confined to the packed-GEMM family, which is the only place a promoted
counter feeds repeated address arithmetic inside one block.

Validation (all on the local-GVN arm): **38/38 + 3/3 negative controls**,
`pipeline_crosscheck` **124/124**, `_r9_1`/`_r9_2`/`_r7_1`/`_r6_2` PASS.

## 4. What this proves

R9.3 §5 predicted exactly this and is now **empirically confirmed**: the
duplicate address arithmetic is **free**. It rides along in already-populated
bundles, so deleting 4.6% of all executed instructions moves nothing. Bundle
count is identical — the removed instructions were never the reason a bundle
existed.

It is also the cleanest demonstration yet of the project's IPB warning: this
transform *lowers* IPB (fewer instructions, same bundles) while performance is
bit-for-bit identical.

**What costs ticks is chain DEPTH, not duplication** — which is R9.3 lead #2
(`[reg + imm]` addressing), the only remaining lead with a tick thesis.

## 5. Ship or not

Neutral on time, −30 static instructions, no bundle change, all tests green.
Arguments against shipping: it adds a second table to GVN for **0% speedup**,
and the R8.1a lesson says any transform that changes emitted SIZE perturbs the
R4.2.5 realisation probe and can silently cost an unrelated kernel a transform
(not observed here — no kernel regressed, no bundle count moved, but the suite
is the only evidence).

**Recommendation: do not commit on performance grounds.** Commit only if smaller
code is wanted for its own sake. Decision deferred to the user.

To revert: `git checkout compiler/gvn.py`.
To keep the mechanism but disable it: `APARA_NO_LOCAL_GVN=1`.

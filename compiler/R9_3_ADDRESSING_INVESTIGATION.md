# R9.3 lead #2 — `[reg + imm]` addressing in vector loops (INVESTIGATION, no code changed)

Follows `R9_3_RAW_AND_REDUNDANCY_ANALYSIS.md` §5/§6 lead 2. Baseline `50e2b67`
(R9.2). **Analysis only — nothing in the compiler was modified for this.**

---

## 1. Where matmul16's time actually goes

Aligned image, bundles attributed to the real block that executes them:

| block | aligned bundles | executions | ticks | share |
|---|---|---|---|---|
| **`fb_10`** (k-loop body) | **25** | 256 | **6 400** | **63%** |
| `fc_9` (k-loop test) | 5 | 272 | 1 360 | 13% |
| everything else | — | — | 2 339 | 23% |
| | | | **10 099** | |

`fb_10` is **16 source bundles but 25 aligned** — 9 `pad_*` bundles per
iteration, and they are *inside* the loop body, before the backedge branch, so
they execute every time: **2 304 ticks (22.8% of the program) is `$null`
padding.** This ESTABLISHES, for this kernel, the reachability that
`R9_2_DELIVERY.md` §7 explicitly left unestablished.

## 2. The claim checks out, and the bundler is already optimal

All 13 memory accesses in `fb_10` use `[reg + reg]`; zero use `[reg + imm]`.
The four A-row addresses are `(i<<5) + {0,8,16,24}` bytes, exactly as R9.3 §5
said.

Measured by running the REAL bundler (`bundle_mcode`, with R6.2 disambiguation
and the list scheduler) over the block, and computing dependence height under
the bundler's own RAW/WAW/MemAlias/MemPhase rules:

| form | instrs | bundles | height(all) | height(registers only) |
|---|---|---|---|---|
| **A** production, `[reg+reg]` | 34 | **16** | **16** | 9 |
| **B** `[reg+imm]` on the A row | 27 | **15** | **15** | 8 |
| **C** `[reg+imm]` everywhere | 27 | **11** | **10** | 8 |

Form A reproduces production's 16 bundles exactly, which is what makes the
experiment trustworthy.

**bundles == height in every form: the scheduler and packer are already AT the
dependence lower bound.** No scheduling change can help. The only way to get
bundles is to delete dependence EDGES.

## 3. The finding: the prize is DISAMBIGUATION, not fewer instructions

B and C contain **the same 27 instructions**. C is **4 bundles shorter**.

Removing the address arithmetic (A -> B, −7 instructions) is worth **1 bundle**.
Making the addresses *provably distinct* (B -> C, −0 instructions) is worth
**4 bundles**.

Memory edges nearly double the block: register dataflow alone needs 8-9 bundles,
the memory rules force 16. The four C-accumulator accesses share base `$r5` but
use four different OFFSET REGISTERS (`$r8/$r7/$r9/$r3`), which R6.2 cannot prove
distinct — so every store is ordered against every later load. Those offsets are
**loop-invariant in the k loop** (`C[i][...]` does not depend on k) and are
simply `{0,8,16,24}` bytes apart. Hoist one base, use constant offsets, and R6.2
proves them pairwise distinct; the edges vanish and height falls 15 -> 10.

**This reframes the lead.** R9.3 §5 justified `[reg+imm]` by chain length
("7 links -> 4"). That is worth 1 bundle. The real value is that a constant
offset is something the memory disambiguator can reason about and a register is
not.

## 4. Projected payoff

`fb_10` 16 -> 11 source bundles = **−5 bundles × 256 = −1 280 ticks**, ~**−12.7%**
on matmul16, before any aligner effect. The same lowering serves gemm
vi8/vu8/vi16/vu16/vi32/vu32, which are 6 of the 8 kernels that dominate suite
ticks.

**Two caveats that must travel with that number:**
1. It is a hand-written what-if on one block, not a compiler change. A real
   implementation must hoist the C-row base out of the k loop, emit `[reg+imm]`
   from the vector lowering, and re-run register allocation. Pressure should
   *improve* (four offset registers freed, one base added; `fb_10` is at 25/28).
2. `mcode_align` may convert some of the saving into padding, exactly as it did
   in R9.2 — where a saved instruction inside a multi-slot bundle bought
   nothing. Here whole bundles are removed, which survived the aligner in R9.2's
   gemm case, but this must be MEASURED, not assumed.

## 5. Recommendation

Implement, in this order:
1. hoist the loop-invariant accumulator row base out of the k loop;
2. emit `[reg + imm]` for the resulting constant offsets in the vector lowering
   (offsets 0-24 are far inside the field — the compiler already emits
   `[$r5 + 256]` and `[$r5 + 510]` elsewhere);
3. measure `fb_10` bundles, then whole-suite ticks, then the aligned image.

Do NOT judge it on instruction count: this whole investigation shows instruction
count and tick count are decoupled here (see `R9_3_LOCAL_GVN_WIP.md`, where
−4.63% dynamic instructions bought exactly 0 ticks).

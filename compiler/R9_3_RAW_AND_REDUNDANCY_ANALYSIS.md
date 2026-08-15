# RAW hazards, register pressure, and instruction redundancy — analysis only

2026-08-02. **No code changed.** Measured on `matmul16/vec2` (the R9.2 build) and
the 36-kernel suite. Companion to `R9_2_WIP.md`.

---

## 1. The register work is COMPLETE — renaming has nothing left to fix

Split reasons across matmul16: RAW 39, Label 10, MemAlias 9, Control 3,
MemPhase 1, and **WAR 0, WAW 0, BundleFull 0, MemLane 0, FUnit 0, spills 0**.

WAR/WAW are the *false* dependences — what register allocation, renaming, R2.6
promotion and R7.1 rematerialization exist to remove. **There are none.** The
allocator is behaving perfectly. The 8-wide issue, the 4 memory lanes and the
div/sqrt lane are **never** the binding constraint.

Every RAW is a true def→use. No register technique can remove a true dependence;
only removing the producing instruction can.

## 2. Registers are NOT the constraint

| scope | used | free |
|---|---|---|
| whole program (union) | 28/28 | 0 |
| **`fb_10` (hot loop)** | **25/28** | **3** |
| `fb_6` | 23/28 | 5 |
| every other block | <=11/28 | 17-28 |

The whole-program 28/28 is a union, not simultaneous pressure. (An earlier
linear-scan "peak 28" was wrong — it ignored control flow. The per-block figures
are the defensible ones.)

## 3. Why duplicate address arithmetic survives GVN — the phase-order trap

In `fb_10`, `$r25 << 4` is computed **four times**. At IR level there are nine
`i * 16` expressions and GVN eliminated **zero**. Not a GVN bug:

```
_t37   = _t35   * 16     <- _t35   defined by IRLoad from base=_t34   off=0
_vgo15 = _vgl14 * 16     <- _vgl14 defined by IRLoad from base=_vgb13 off=0
_vgo20 = _vgl19 * 16     <- _vgl19 defined by IRLoad from base=_vgb18 off=0
   ... 9 in total, 9 DISTINCT signatures
```

**Every operand is a separate reload of the same induction variable from its
stack slot**, into a fresh temp. `clone_offset` re-derives the address
computation per chunk and each derivation reloads the IV.

GVN compares operands by temp NAME. Nine names -> nine keys -> nothing collapses.
GVN is correct: proving `_vgl14 == _vgl19` needs memory reasoning, and GVN
numbers only PURE expressions (`IRBinOp`, `IRUnaryOp`, `IRAssign`, `IRLoadAddr`).
`IRLoad` is excluded by design.

Then strength reduction turns 21 `*` into 21 `<<` (this happens AFTER GVN — GVN
sees `*`, never `<<`), and codegen emits 26.

**Register allocation is the LAST stage.** It is handed nine values that are
provably distinct as far as the compiler knows, so it must give them nine
registers and sequence them. *You cannot recover at register allocation the
information destroyed at IR generation.*

## 4. Redundancy is real but almost entirely FREE — R9.1's threshold vindicated

Local CSE over emitted mcode (conservative: block boundaries reset, any source
write invalidates, any store kills cached loads):

**564 of 4238 suite instructions (13.3%) recompute a live value.**

| shape | count | |
|---|---|---|
| `+ rd, FP, const` (frame address) | 248 | **84.9%** |
| `+ rd, $r0, K` (constant) | 16 | 5.5% |
| `$ld` (reload same address) | 15 | 5.1% |
| `<<` (scaled index) | 13 | 4.5% |

Worst: elementwise vi8 25.0%, axpy vi8 23.3%, dot vi16 22.7%. matmul16: 10/151.

**BUT — the decisive number:**

| | |
|---|---|
| alone in their bundle (removing SAVES a bundle) | **13 (4.5%)** |
| co-issued (removing saves code size, not time) | **279 (95.5%)** |

**All 10 of matmul16's ride along free.** `FP + const` reads only the frame
pointer, which no loop writes — no incoming dependence, so it is freely
schedulable into any empty slot. Recomputing beats holding a register live.

This independently CONFIRMS R9.1's decision to number only offsets outside the
foldable immediate range (its comment cites conv3 +12.4%, axpy vi16 +9.2% when
unrestricted). **Chasing redundancy is worth ~13 bundles suite-wide. Do not.**

## 5. What DOES cost bundles: chain DEPTH, not duplication

CORRECTS an earlier claim of mine that matmul16's four duplicate shifts were
worth eliminating — they are free. What costs bundles in `fb_10` is that
`<<4 -> copy -> <<1 -> $ld -> $v* -> $v+ -> $st` is **seven serial links**, and
each link is a bundle no matter what else packs alongside it.

`fb_10` instruction mix (37 instructions): 9 `$ld`, 9 `<<`, 8 `$v`, 6 `+`,
4 `$st`, 1 `?` — **15 of 37 (41%) is address arithmetic** vs 8 vector ops of real
work.

**Root cause: all 13 memory accesses in `fb_10` use `[reg + reg]`. ZERO use
`[reg + imm]`** — although the compiler plainly can emit it (`fe_8` has
`[$r5 + 0]`, `[$r5 + 256]`, `[$r5 + 510]`). Because the offset must live in a
register, each access drags in a shift and an add, and those become the chain.

The four A-matrix addresses are `((i<<4)+K)<<1` for K=0,4,8,12, i.e.
`(i<<5) + {0,8,16,24}` bytes = **one shift, one add, four immediate-offset
loads**, replacing ~9 instructions and shortening the chain from 7 links to 4.

## 6. Where to start next time

1. **Find out why this loop's IV is not register-promoted.** R2.6 promotion
   already exists; one temp instead of nine makes GVN collapse all nine `*16`
   with zero new machinery. Smallest change, largest effect.
2. Emit `[reg + imm]` addressing in vector loops (§5) — removes chain links, and
   unlike R9.2 the gain is not recoverable by alignment padding.
3. Make `clone_offset` reuse the base instead of re-deriving (`R9_0` §1's 5.39
   address/memory outlier).

Scripts used are throwaway (`$CLAUDE_JOB_DIR/tmp`); all are ~40 lines and rebuild
from the descriptions above. The local-CSE one is worth rebuilding carefully:
publish the result AFTER invalidating, or every entry evicts itself and you get a
convincing, entirely false, 0%.

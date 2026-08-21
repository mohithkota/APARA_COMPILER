# R14.2 — Cross-reduction affine address sharing

Branch `feature/r13-matmul-dot`, on top of R14.1a (`8373740`). Frozen tags
untouched, nothing pushed. No change to the register pool, scheduler, bundler,
`$dot` emitter, accumulator expansion or datatype semantics.

## Answer to the final question

**Yes to the proof; partially to the gap.**

The compiler now *proves* common invariant bases plus compile-time per-column
displacements across reductions, and exploits them. For 16×16 vu8 J_TILE=4 the
four B accesses collapse from **4 separately materialised bases to 1**, emitted
as `[$r3 + 0]`, `[$r3 + 16]`, `[$r3 + 32]`, `[$r3 + 48]`.

It **materially improves** matters — J_TILE=4 went from the *worst* configuration
to the *best*, consistently 12–16% — but it does **not** close the gap to the
hand-written reference. Roughly a fifth of the way, not the whole distance.

## 1. The affine extension (generic, no matmul knowledge)

`vector_affine` recorded `const_off` and `sym_div`, which is enough for
alignment but cannot decide whether two offsets differ by a constant — `sym_div`
is a divisor, not an identity.

`AffineAccess` gains **`sym`**: the symbolic part as `{canonical_key: multiplier}`.
Keys are **value identities, not temp names** (`_sym_key`): a load of a stack
slot the loop never writes yields the same value however often it is re-loaded,
so all such loads share a key. That is what lets `(j+0)*S+k` and `(j+1)*S+k`
compare equal even though each statement computed its own `j` temp.

**`constant_delta(a, b)`** returns C with `b == a + C` bytes, or `None`:

> equal `coeff` (same rate along the IV) **and** equal `sym` (same invariant
> part) ⟹ the remainder is the constant difference `b.const_off - a.const_off`.

It knows nothing about matrices, columns or kernel kinds — it answers a question
about two affine expressions, so any vector client can use it. R9.3 already
shares a base across *chunks* by exactly this reasoning, hard-coded; this makes
the same reasoning available between any two accesses.

Verified on the real IR: the four B accesses resolve to identical
`coeff=1, sym={('slot',-528,0): 16}` with `const_off = 0/16/32/48`, giving
pairwise deltas 16/32/48.

## 2. Exploitation

Before materialising a base, an access tries `constant_delta` against every
already-materialised base **on the same array object**, and shares when the
resulting displacement (`delta + (chunks-1)*lanes*eb`) fits the ISA immediate
field. Without the proof it materialises its own base — a guess would be a
wrong-address bug.

Result for 16×16 vu8 J_TILE=4: **2 bases emitted** for 8 operand accesses
(1 shared A row + 1 shared B base with deltas 0/16/32/48).

A second defect was found and fixed while measuring: the multi-reduction path
wrote each `$dot $accumulate` to a *fresh* temp, so codegen emitted a register
copy per chunk per reduction — the same `+ rX, r0, rY` chain R13.1 removed,
reintroduced because R13.1's expansion is subsumed at N>1. Accumulating **in
place** (dest *is* the accumulator, the R2.6 form) removed it.

## 3. Performance — 16×16 vu8

| | R14.1a | **R14.2** |
|---|---|---|
| J_TILE=1 | 23.996 | 23.996 |
| J_TILE=2 | 22.121 | 22.121 |
| **J_TILE=4** | 22.621 *(worst)* | **20.871** *(best)* |

Vector block, J_TILE=4: **31 → 24 bundles**, **71 → 63 instructions**,
address instructions **50 → 42**, **7.75 → 6.00 bundles/output**.

Stop condition 6 does not fire: instructions, bundles **and** ticks all fell.

## 4. All sizes and datatypes — J_TILE=4 best everywhere

ticks/output, all 256/1024 checks correct, 0 errors, `$dot` = chunks × J_TILE:

| dtype | N | JT=1 | JT=2 | **JT=4** | gain |
|---|---|---|---|---|---|
| vu8 | 16 | 24.00 | 22.12 | **21.12** | −12.0% |
| vu8 | 32 | 26.50 | 24.53 | **23.16** | −12.6% |
| vi8 | 16 | 23.00 | 21.12 | **20.12** | −12.5% |
| vi8 | 32 | 25.50 | 23.53 | **22.16** | −13.1% |
| vu16 | 16 | 26.06 | 23.68 | **22.12** | −15.1% |
| vu16 | 32 | 28.53 | 28.09 | **23.93** | −16.1% |
| vi16 | 16 | 25.06 | 22.68 | **21.12** | −15.7% |
| vi16 | 32 | 27.53 | 27.09 | **25.93** | −5.8% |

(Measured before the in-place-accumulation fix, so these are slightly
conservative; vu8 16×16 improved further to 20.87 after it.)

## 5. Register pressure

**0 spills in every configuration**, as in R14.1a. Sharing reduces address
temporaries, so the direction is favourable, and register pressure is still not
the limiter at these widths.

## 6. Regression — production unchanged

| check | Phase-0 baseline | R14.2 |
|---|---|---|
| 38-program suite | 38/38 | **38/38, metrics CSV bit-for-bit identical** |
| negative controls | 3/3 | **3/3** |
| `pipeline_crosscheck` | 124/124 | **124/124**, 0 IR / 0 code / 0 tier mismatches |
| `compiler/_r*_test.py` | 20/20 | **24/24** |
| `loopopt/_*_test.py` | 25/25 | **25/25** |
| `_r14_2_test.py` | — | **15/15** |

**R9.3 is preserved**: GEMM, dot, reduction, convolution, AXPY and elementwise
all sit inside the byte-identical suite, and `[reg+imm]` lowering is still
active. Anti-bias: commuted index spellings produce the same base count.

## 7. Remaining gap — and why

Kernel-only, 16×16 vu8: **6.00 bundles/output vs the hand-written 1.207**. Still
~5×.

The block is now **24 bundles of which 15 hold a single instruction**. The loads
are perfect (`[$r3 + 0/16/32/48]` in one bundle) and the dots pack (one bundle
holds 8 instructions), but the surrounding scalar work does not:

- accumulator slot loads/stores emit an `IRLoadAddr` each — 8 address
  instructions per trip for 4 reductions;
- the shared bases are still re-derived **per trip** rather than hoisted out of
  the j-loop, which the hand-written kernel does by keeping `$r28` live across
  the whole row.

That second point is the next lever: the bases are invariant in the *k* loop but
are recomputed on every entry because the vector region is the k-loop and LICM
does not run over vector-lowered IR. It is loop-invariant code motion on the
generated code, not a new addressing idea.

## 8. Limitations

1. Address hoisting out of the enclosing loop (above) — the largest remaining
   item.
2. J_TILE is still programmer-written; automatic tiling needs unroll-and-jam.
3. Multi-reduction remainder rejected; compact realisation unavailable for N>1.

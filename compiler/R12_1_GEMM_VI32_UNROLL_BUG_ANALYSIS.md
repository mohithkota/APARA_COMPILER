# R12.1 — GEMM compact-loop U≥2 correctness bug: diagnosis

Branch `feature/r12-partial-unroll`, off `r11-verified`. **Diagnosis only — no
code changed yet.**

---

## 1. Root cause (proven)

`gemm_lowering.build_compact.emit()` **discards the per-copy chunk index that
`build_compact_chunk_loop` passes it**, so every one of the U unrolled copies
addresses the *same* chunk while the loop advances the IV by `U*lanes`.

```python
# gemm_lowering.py:396
def emit(_off, iv_index=None):
    ...
    body, err = _row_body(plan, instrs, def_map, region, a_val, None)
    #                                                          ^^^^
    #  iv_index is ignored; None makes clone_offset RE-LOAD the IV slot,
    #  which yields the loop's CURRENT chunk for every copy.
```

`vector_compact_loop.build_compact_chunk_loop` is correct and even documents this
exact hazard:

```python
# vector_compact_loop.py:304
# `emit_body(off, iv_index)` -- both are needed. A client that addresses
# chunks directly uses the BYTE offset; a client that re-emits the loop's own
# address computation through `clone_offset` (GEMM's row base, a shifted
# convolution window) must substitute the ELEMENT index instead, or every
# copy re-derives the same address and the copies are identical.
```

GEMM is precisely that client, and it is the one that ignores the index.

## 2. The faulty IR sequence

Captured at validation time (GEMM vi32 M=32, lanes=2, chunks=16, U=2). The
framework computes copy 1's offset and index correctly:

```
[94]  *(_vcs118+_vgo104) = _vaa117     <- copy 0 stores at the current chunk
[95]  _vcu119 = _vco97 + 8             <- framework: copy 1 BYTE offset  (+1 word)
[95]  _vci120 = _vcv96 + 2             <- framework: copy 1 ELEMENT index (+lanes)
[96]  _vgb121 = &stack[FP-12296]
[97]  _vgl122 = *(_vgb121+0)
[98]  _vgo123 = _vgl122 * 32
[99]  _vgb124 = &stack[FP-12304]       <- RE-LOADS THE IV SLOT, ignoring _vci120
[100] _vgl125 = *(_vgb124+0)
[101] _vgo126 = _vgo123 + _vgl125
[102] _vgo127 = _vgo126 * 4            <- identical address to copy 0
```

**Consumers of `_vcu119` / `_vci120`: 0.** They are computed and dead. Copy 1
re-derives copy 0's address from the IV slot at `FP-12304`.

## 3. First wrong output

Differential oracle, GEMM vi32 M=32, seed 0 — all of U=2/4/8 report the *same*
first four addresses:

```
memory differs [-12288, -12284, -12280, -12276]
```

Values (4-byte elements, so consecutive C elements):

| addr | scalar | vector | |
|---|---|---|---|
| −12288 | 48 | **96** | 2× — accumulated twice |
| −12284 | 96 | **192** | 2× |
| −12280 | 144 | **0** | never written |
| −12276 | 192 | **0** | never written |
| −12272 | 240 | **480** | 2× |
| −12268 | 288 | **576** | 2× |
| −12264 | 336 | **0** | never written |
| −12260 | 384 | **0** | never written |

**Period 4 elements = 2 chunks = U*lanes.** Exactly the predicted signature: with
U=2 both copies accumulate into chunk *c* (doubling it) and chunk *c+1* is
skipped entirely (left at its initial 0). 1 027 addresses differ in total.

## 4. Why U=1 is correct

With U=1 the loop emits one copy per iteration and increments the IV by `lanes`.
Re-loading the IV slot then yields exactly the chunk that iteration is supposed
to process, so ignoring `iv_index` is harmless. The bug is invisible at U=1 by
construction — which is why it survived since R6.4.

## 5. Why U≥2 fails

The copies are emitted at `k = 0..U-1` but all re-derive the address from the IV
slot, so they are literally identical bodies. The loop then advances by
`U*lanes`, so of every `U` chunks one is written `U` times and `U-1` are never
written.

## 6. Minimal reproducer, and the bug is far wider than R12.0 reported

Forcing the compact realisation (`APARA_VECTOR_REALISATION=compact`) with
`APARA_VECTOR_UNROLL=2`:

| type | lanes | M | chunks | U=1 | U=2 |
|---|---|---|---|---|---|
| vi32 | 2 | 16 | 8 | match | **mismatch** |
| vi32 | 2 | 32 | 16 | match | **mismatch** |
| vi16 | 4 | 16 | 4 | match | **mismatch** |
| vi16 | 4 | 32 | 8 | match | **mismatch** |
| vi8 | 8 | 16 | 2 | match | **mismatch** |
| vi8 | 8 | 32 | 4 | match | **mismatch** |

**Every GEMM compact build with U≥2 is wrong, at every element type and every
chunk count.** This corrects `R12_0_PARTIAL_UNROLL_DELIVERY.md`, which bounded
the defect to "chunks=16". That bound was an artefact of the *default* search: in
every other configuration the unrolled realisation wins, so the buggy compact
path is never built. vi32 M=32 is simply the only configuration where compact is
selected — it is where the bug becomes *observable*, not where it exists.

Smallest reproducer: **GEMM vi8, M=16 (2 chunks), forced compact, U=2.**

## 7. Correctness impact: none shipped

* The differential oracle rejects it on every build, before commit.
* The search falls back to U=1, which is validated.
* No shipped kernel selects compact GEMM with U≥2, so no emitted binary has ever
  contained this.
* GEMM vi32 M=32 passes its gcc golden check today (3/3 PostConditions).

The oracle is behaving correctly. Per the stop conditions, this is **not** a case
of "the failure is in the oracle" — the generated code is genuinely wrong, and
the memory-value pattern in §3 proves it independently of the oracle's verdict.

## 8. Proposed minimal fix

One argument, in `gemm_lowering.build_compact`:

```python
-        body, err = _row_body(plan, instrs, def_map, region, a_val, None)
+        body, err = _row_body(plan, instrs, def_map, region, a_val, iv_index)
```

`clone_offset` substitutes `iv_value` for the IV load whenever it is not None,
and it accepts any value node — the existing unrolled realisation passes a
`Const`, and `iv_index` is a `Temp`, which flows through the same path
(`mapping[t.name] = iv_value`).

For copy 0 the framework passes `i_body`, the temp already holding the loaded IV,
so substituting it is equivalent to the current re-load and **U=1 behaviour is
unchanged** — which the validation must confirm.

Nothing else changes: no new realisation, no selector change, no profitability
change, no framework change. `conv`/`axpy`/`elementwise` clients already use the
offset the framework hands them and are untouched.

## 9. What this does NOT establish

Fixing correctness does **not** imply U≥2 becomes profitable. U≥2 has never
produced a correct build for GEMM, so no valid performance number for it exists
at any size. After the fix, the search will evaluate genuinely-correct U≥2
candidates for the first time and may still choose U=1. That must be measured,
not assumed.

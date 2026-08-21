# R14.10 — Result-store pointer IVSR: **STOPPED**

Compiler at R14.8 (`a8fede2`), docs on top of R14.9 (`b4697d1`).
**ANALYSIS ONLY — 0 production `.py` files changed.** Frozen tags untouched,
nothing pushed.

## Answer

The result pointer **is** an induction variable of the J-tile loop, but the
existing IVSR **cannot express it**, and closing the gap is far broader than this
milestone. **Stopped, per the milestone's own clause.**

## 1. The pointer is genuinely an IV

R14.9 established the full base is *not* loop-invariant. It is, however, affine
in the tile IV:

```
address = ((i*N) + j) * element_size          j = J-tile IV, step = J_TILE
        = i*N*element_size + j*element_size
next    = ptr + J_TILE * element_size
```

So the transformation is legal in principle. The obstacle is analysis, not
legality.

## 2. The exact abstraction gap

Two independent restrictions in `ivsr.py`, both verified against the source:

**(a) `_iv_term` requires `iv * Const` with the multiplied operand *directly* an
IV load.**

```python
if type(ins).__name__ == 'IRBinOp' and ins.op == '*':
    for cst, oth in ((ins.right, ins.left), (ins.left, ins.right)):
        if isinstance(cst, Const) and isinstance(oth, Temp):
            ivo = _is_iv_load_name(oth.name)      # <-- must BE the IV load
```

**(b) `_decompose` handles only `+` at the top level** — it recurses through
addition and returns `None` for anything else, including a multiply.

The result address is a constant multiply applied to a **sum**,
`((i*N) + j) * 8`. `_iv_term` fails because `_t138` is a sum, not an IV load;
`_decompose` fails because the defining op is `*`, not `+`. The access is never
a candidate.

**(c)** Separately, candidates are restricted to `IRLoad`/`IRStore` with an
**invariant base** and a **Temp offset** — while R14.8 deliberately moved the
varying part *into* the base and left `Const` offsets, the exact complement.

## 3. What was tried, and why it was reverted

An additive `IRGlobalAddrOf` candidate kind was added to IVSR — reducing the
address-materialisation node itself, so the four stores keep their own constant
displacements and share one pointer (preserving R14.8 by construction).

Measured with IVSR's own diagnostics (`APARA_IVSR_DEBUG=1`):

```
[ivsr] loop @42 (fc_9): basic_iv offsets=[-528]
[ivsr] loop @42: 0 candidate accesses
```

**Zero candidates** — blocked by gap (b), which the extension did not touch.
Ticks were unchanged at 4575. The edit was **reverted**: shipping inert,
never-exercised code inside a delicate shared pass is worse than not shipping it.

## 4. The what-if (measured, project's own bundler)

| epilogue form | instructions | **bundles** |
|---|---|---|
| current — address rebuilt per tile | 12 | **5** |
| IVSR pointer form | 9 | **3** |

Block 12 → 10, ≈**2.8%** whole-program (4575 → ~4447).

## 5. Why the gap was not closed

Making `_decompose` distribute a constant multiply over a sum requires scaling
**every** part it returns — and its `inv_parts` representation is a flat list of
temps/constants that are *added*. It cannot express "this invariant temp, scaled
by k"; that needs new preheader multiplies and a richer representation, in the
core analysis every IVSR candidate in every program flows through.

That is a broad change to a shared pass for a measured **2.8%**. Judged out of
scope, and the milestone's clause is explicit: *"If the existing IVSR machinery
cannot safely express this, STOP and explain the exact abstraction gap."*

## 6. Verification (nothing changed)

| check | result |
|---|---|
| production `.py` changed | **0** |
| 38-program suite | **38/38 PASS**, **0 programs differ from R14.8** |
| negative controls | **3/3** |
| `pipeline_crosscheck` | **124/124**, 0 IR / 0 code / 0 tier mismatches |
| `compiler/_r*_test.py` | **29/29** |
| `loopopt/_*_test.py` | **25/25** |
| `_r14_10_test.py` | **9/9** |

Every remaining suite was re-run **individually** with a 300 s cap, each
preceded and followed by a process check. All passed; the 15 compiler suites
total ≈266 s and the 25 loopopt suites ≈2 s.

`_r14_10_test.py` pins the gap against IVSR's source contract — `_decompose`
handles only `+`, `_iv_term` needs a direct IV load, candidates are restricted
to `IRLoad`/`IRStore` with an invariant base — plus the fact that the result
offset is defined by a multiply. It also guards R14.8's three
immediate-displaced stores.

## 7. Standing conclusion

- The full result pointer is **not** loop-invariant (R14.9).
- It **is** an induction variable: `((i*N) + j) * element_size`.
- Existing IVSR cannot express the multiply-over-sum form:
  `_iv_term` requires `IV * Const` directly; `_decompose` handles only top-level
  addition.
- What-if: **5 → 3 epilogue bundles, ≈2.8% whole-program**.
- Implementing it needs a generic IVSR representation able to distribute a
  constant multiply over a sum — too broad for this milestone.

**Not implemented. The reverted `IRGlobalAddrOf` candidate is deliberately not
revived: it produced zero candidates. R15 not started.**

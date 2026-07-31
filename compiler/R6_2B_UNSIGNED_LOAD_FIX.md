# R6.2B — Unsigned Packed Load Correctness (defect D3)

**Scope kept:** D3 only. No D1 (unaligned convolution loads) and no D2 (packed
GEMM at 16/32-bit) work was performed. One production line of code changed, in
`ir_gen.py`.

**Result:** `gemm vu8` goes from FAIL to PASS on the simulator; the 124-program
corpus is **byte-identical**; **no signed test changed by a single tick**.

---

## 1. Root cause

`pycparser` does **not** expand typedefs. A declaration written

```c
vu8_t a[64];
```

arrives in the front end as `IdentifierType(names=['vu8_t'])` — the marker name
itself, never `unsigned char`. Every C type is resolved through one table:

```python
_CTYPE_TO_APARA = {
    'char': '$i8', 'unsigned char': '$u8', ...
    'float': '$f32', 'float32_t': '$f32', 'vf32_t': '$f32',   # <- vf32_t IS here
    'double': '$f64', 'float64_t': '$f64',
}                                                             # <- the six integer
                                                              #    markers were NOT
def _c_decl_to_apara_type(node):
    ...
    return _CTYPE_TO_APARA.get(name, '$i64')                  # <- silent default
```

The float marker `vf32_t` had been added; the six integer markers
(`vi8_t`/`vu8_t`/`vi16_t`/`vu16_t`/`vi32_t`/`vu32_t`) had not. So every packed
integer marker fell through to the `'$i64'` default, and

```python
def _is_unsigned_decl(node):
    return _c_decl_to_apara_type(node).startswith('$u')
```

answered **signed** for `vu8_t`, `vu16_t` and `vu32_t`.

The defect is a **missing table entry with a silent fallback**, not faulty logic:
every stage downstream was already correct and simply propagated the wrong
answer. Note that the same fallback gives the *right* answer for the signed
markers by coincidence — `$i64` is not unsigned, and neither is `$i8` — which is
why only the unsigned half ever misbehaved.

---

## 2. Pipeline trace — where unsignedness was lost

Traced stage by stage on `vu8_t a[64]; … s = a[i];`, recording signed/unsigned at
each step. The **first** stage that loses it is stage 3.

| # | stage | carries signedness? | observed value |
|---|---|---|---|
| 1 | source | yes | `vu8_t` |
| 2 | fake typedef (`compiler.py`) | yes | `typedef unsigned char vu8_t;` |
| 3 | **parser / type resolution** (`_c_decl_to_apara_type`) | **NO — LOST HERE** | `'vu8_t'` not in table → **`'$i64'`** |
| 4 | `_is_unsigned_decl` | already lost | `'$i64'.startswith('$u')` → **False** |
| 5 | `_unsigned_vars` set (`visit_Decl`, line 1039) | already lost | `a` **not** added |
| 6 | `_Addr.unsigned` / `_arrayref` (line 2162) | already lost | `unsigned=False` |
| 7 | `IRLoad.unsigned` | already lost | **`False`** |
| 8 | codegen `_atype(1, False)` | already lost | **`($i8)`** |
| 9 | mcode | already lost | `$ld ($i8) $r16 [$r15 + 3]` |
| 10 | simulator | correct behaviour, wrong input | `Sign_Extend_64(7, 0xC0)` → **−64** |

Stages 4–10 were all working correctly. They faithfully propagated a wrong
answer produced at stage 3.

Two independent paths reach `IRLoad.unsigned`, and **both** were checked — the
`_arrayref` path (`unsigned = name in self._unsigned_vars`) and the `_eval_addr`
path (all five `_Addr(...)` constructions consult `self._unsigned_vars` too). Both
read the same `_unsigned_vars` set, so both were fixed by correcting stage 3, and
neither needed its own change.

### Verification that stage 3 is the first loss

```
all sub-word IRLoads (width, unsigned) per kernel   [after the fix]
  vu8_t    element-width(1B) loads unsigned=[True]
  vi8_t    element-width(1B) loads unsigned=[False]
  vu16_t   element-width(2B) loads unsigned=[True]
  vi16_t   element-width(2B) loads unsigned=[False]
  vu32_t   element-width(4B) loads unsigned=[True]
  vi32_t   element-width(4B) loads unsigned=[False]
```

(The additional `(4, False)` entries in the vu8/vu16/vu32 rows are the `int i`
induction-variable loads — correctly signed.)

---

## 3. Does the ISA provide an unsigned load?

**Yes — the compiler must emit it, and now does.**

From `engine_isp/isa/isa.txt`, the 5-bit type codes are an explicit signed /
unsigned pair, with the signed family documented as "sign-extend results in
64-bit registers":

```
$i4  00000  --> sign-extend results in 64-bit registers.
$i8  00001
$i16 00010
$i32 00011
$i64 00100

$u4  00101
$u8  00110          <- distinct opcode, zero-extends
$u16 00111
$u32 01000
$u64 01001
```

The simulator honours the distinction correctly
(`McodeExecute.cpp::___execute_load_operation___`):

```c
// sign-extend.
ovalues[0] = (signed_flag ? Sign_Extend_64(sign_bit_index, mval) : mval);
```

So this is **compiler-side**, unambiguously. No ISA limitation is being worked
around, and no simulator change is involved.

---

## 4. Compiler changes

One table, three entries, in `ir_gen.py`:

```python
    'vu8_t':  '$u8',        'vu16_t': '$u16',          'vu32_t': '$u32',
```

The fix is at the point of loss, so it benefits **every** use of a packed
unsigned type — array element loads, pointer dereferences, `&`-taken addresses,
casts, and the arithmetic-conversion logic (`_expr_is_unsigned`) — with no
kernel-specific or vector-specific special case. Nothing in the vectorizer,
scheduler, bundler or code generator was touched.

### Why the three SIGNED markers were deliberately left out

`_c_decl_to_apara_type` has exactly five consumers. For signedness the `$i64`
fallback already yields the correct answer for `vi8_t`/`vi16_t`/`vi32_t`. The
only other consumer is **cast narrowing**:

```python
dest_type = _c_decl_to_apara_type(node.to_type.type ...)
...
if dest_type == '$i64':
    return expr_val          # no narrowing emitted
```

Adding the signed markers makes `(vi8_t)x` start emitting a truncating `$cast`
it does not emit today. Measured: doing so changed the tick count of **28**
programs, including purely signed ones. That is out of scope for a milestone
whose rule is "no signed tests may change", so it is recorded below as a
separate defect rather than folded in silently.

### D4 (new, NOT fixed here) — casts to packed markers do not narrow

`(vi8_t)300` and `(vu8_t)300` currently do not truncate to 8 bits, because the
cast path sees `$i64` and returns the value unchanged. Adding the unsigned
markers fixes this for `vu8_t`/`vu16_t`/`vu32_t` as a side effect of the D3 fix;
the three signed markers remain affected. Evidence: with all six markers in the
table, a `(vi16_t)(i & 7)` cast begins emitting `$cast ($i16)` where it
previously emitted nothing. Small values are unaffected, which is why no test
detects it today — but `(vi8_t)300` is wrong now.

---

## 5. Generated mcode, before and after

Source: `vu8_t a[16]; … a[i] = (vu8_t)(200+i); s = a[3];`

```
BEFORE   $ld ($i8) $r16 [$r15 + 3]        -> Sign_Extend_64(7, 0xC0) = -64
AFTER    $ld ($u8) $r16 [$r15 + 3]        -> 0xC0                    = 192
```

Whole-kernel tag census for `gemm vu8_t`:

| tag | before | after |
|---|---|---|
| `$ld ($i8)` | **4** | 0 |
| `$ld ($u8)` | 0 | **4** |
| `$ld ($i64)` | 5 | 1 |
| `$ld ($u64)` | 0 | 4 |
| `$st ($i8)` | 3 | 3 (unchanged) |

Two things worth noting. Stores are **unchanged**: the grammar ignores `$u` on
`$st` because truncation is identical either way, which is why `IRLoad` carries
the flag and `IRStore` does not. And four 64-bit loads moved from `$i64` to
`$u64`, which is a no-op at that width (`Sign_Extend_64(63, x)` is the identity)
— harmless, and it assembles and runs.

---

## 6. Simulator verification

The failing case, on the real toolchain, through the R6.2A harness (six checks,
independent gcc golden reference):

```
BEFORE   FAIL gemm vu8   [clean] 1 PostCondition mismatch:
                Error: PostCondition Mem[0x82] = 0xffffffffffffffc0, expected 0xc0
AFTER    PASS gemm vu8    3 checks   8416 ticks   ipb=1.169
```

`0xffffffffffffffc0` is −64; `0xc0` is 192. Exactly the reported defect, now
correct.

Full suite: **27/38 → 28/38 verified**, the single status change being
`gemm vu8` FAIL → PASS. The 10 remaining failures are the untouched D1 (six
convolution programs) and D2 (four GEMM programs). All three negative controls
still fail as required, so the harness is still capable of rejecting a bad run.

---

## 7. Regression summary

| check | result |
|---|---|
| 124-program corpus, bundled mcode hashes | **0 changed** (byte-identical) |
| simulator status changes | **1**: `gemm vu8` FAIL → PASS |
| signed-marker + scalar programs, tick counts | **0 changed** |
| unsigned-marker programs, tick counts | 15 changed (see below) |
| `pipeline_crosscheck` | PASS 124/124 |
| unit suites (all 15 `_r*_test.py`) | **all pass** |

### The 15 unsigned-marker tick changes are correct, and one is important

They come from two effects, both correctness fixes:

1. **Casts to `vu*_t` now narrow.** `(vu8_t)(i & 7)` emits a truncating `$cast`
   that was previously elided. Costs an instruction; required by C semantics.

2. **The vectorizer now correctly REJECTS unsigned reductions.** With `vu8_t`
   finally known to be unsigned, the R4.0 legality check fires:

   ```
   reduction vi8_t: vectorized=1  reason=ok
   reduction vu8_t: vectorized=0  reason=illegal:isa-unsupported:unsigned-vreduce-buggy
   ```

   R4.0's capability database records unsigned `$vreduce` as toolchain-broken.
   Before this fix the compiler did not know these arrays were unsigned, so it
   **vectorized unsigned reductions using signed `$vreduce`** — a second latent
   correctness hole, closed as a consequence of fixing D3. `reduction vu8` going
   752 → 1192 ticks is that kernel correctly falling back to scalar.

### Three test suites needed updating — and why that is not moving the goalposts

`_r4_3_test.py`, `_r4_2_5_test.py` and `_r4_2_6_test.py` were failing **before
this milestone**, at HEAD, and all three pass with `APARA_NO_MEMDISAMB=1`. They
are **R6.2 consequences that the R6.2 sweep missed** because those three suites
were not run — an honest gap in that milestone's verification, recorded here
rather than quietly repaired.

All three asserted a *snapshot* of R4.2.5's compact-vs-unrolled crossover
("8 chunks → compact", "margin 0.0 → compact"). R6.2's disambiguation made the
unrolled form pack tighter, so the crossover legitimately moved. Each test was
changed to assert the **invariant it exists to protect** — that the realisation
is measured rather than assumed, and that the compiler keeps the smaller
candidate — instead of re-freezing today's outcome. The weaker alternative
(deleting the checks) was not taken, and the reason for each change is written
into the test.

---

## 8. Reproduction

```sh
cd compiler
python3 -m verification --csv after.csv        # gemm vu8 now PASSes
python3 loopopt/pipeline_crosscheck.py         # 124/124
for t in _r*_test.py; do python3 $t | tail -1; done
```

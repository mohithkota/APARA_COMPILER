# Universality campaign — make the compiler handle ALL C, not just the tests

Motivation: verify the compiler has no test-specific bias and handles arbitrary
valid C (within its feature set). Method: differential testing with NOVEL
algorithms (not the feature unit-tests) vs gcc golden. Reproducers: u1-u8 here.

## Bias audit (done — clean)
No codegen bias. `results` is used ONLY by compiler.py's golden-verify harness
(gcc oracle); ir_gen/codegen never special-case any name. `0x400` GBASE is a
configurable default. All programs compiled uniformly.

## Scorecard (as of 2026-07-17)
| Test | Exercises | Status |
|------|-----------|--------|
| u2_binsearch | pointer-param binary search | PASS |
| u3_gcd | Euclid loop, %/ | PASS |
| u4_popcount | unsigned shift/mask loop | PASS |
| u6_matrix_transpose | 2D global array | PASS |
| u1_bubblesort | LOCAL array init + in-place swaps | **PASS (fixed)** |
| u5_sieve | global array + `results[n++]` + `i<30 && n<5` | FAIL |
| u7_struct_ptr_algo | array of structs, `&pts[i]`, `p->x` | FAIL |
| u8_reverse_accumulate | pointer swap `*lo=*hi` into a LOCAL array | FAIL |

## FIXED this session
**Local array initializer** (`int a[3]={11,22,33}`): visit_Decl InitList store
used `i*esz` offset + `esz` width; local elements are one-per-DMEM-word (stride
8) accessed full-word, so values were mis-placed/mis-sized (0xb00000016). Fix in
ir_gen.py: use `_array_elem[name]` (DMEM stride) for BOTH offset and width.

## OPEN gaps (each has a reproducer; fix like the pointer campaign)
- **u8** — pointer store through a local pointer into a LOCAL array (`int *lo=a;
  *lo=*hi`) doesn't take (array unchanged). The pointer refactor covered stores
  into GLOBAL arrays (t07/t15); local-array target via pointer differs. Start by
  diffing the `*lo=*hi` store address/width vs a working global case.
- **u7** — array of structs: `&pts[i]` / `p->x` read wrong fields (best=41 not
  113). Likely struct-element stride vs field-offset interaction in `_eval_addr`
  for `ArrayRef` whose element is a struct.
- **u5** — sieve returns all 0. Isolate: (a) `results[n++]` variable-index store,
  (b) global-array store in nested loop `for(j=i*i;j<30;j+=i) isprime[j]=0`,
  (c) compound `i<30 && n<5` loop condition. Bisect with minimal cases.

## Gate (run after every fix; same discipline as the pointer campaign)
Battery u1-u8 must only go UP; real suite (pointer/test_pointer, array/*,
matmul_n16) + pointer battery t01-t15 must stay 0 err / 15/15.

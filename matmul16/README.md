# 16×16 Vector Matrix Multiply — APARA

`C = A · B`, all 16×16, packed 16-bit elements (`vi16_t`, 4 lanes per 64-bit word).

## Files

| file | what it is |
|---|---|
| `matmul16.c` | the kernel (verified against gcc) |
| `measure_ipb.py` | IPB measurement, reusing the R6.1 analysis framework |
| `IPB_REPORT.txt` | generated report |
| `vec/` | vectorized build (`matmul16.mcode`, `data.map`, `run.sh`) |
| `scalar/` | same source with `APARA_NO_VECTORIZE=1`, for comparison |

Reproduce: `python3 measure_ipb.py`, or `cd vec && bash run.sh`.

## Why it is written i-k-j over flat 1-D arrays

Three constraints, all measured during R4.3/R4.4:

* a 2-D `vi16_t m[16][16]` is **never packed** — `ir_gen` packs only 1-D
  marker-typed arrays — so it must be flattened to `m[i*16+j]`;
* **i-j-k** order strides `B` by 16 in the inner loop (a column walk); no packed
  load can gather that;
* **i-k-j** hoists `A[i*16+k]` into a loop-invariant scalar `s`, leaving
  `C[i*16+j] += s * B[k*16+j]` — an AXPY over contiguous rows, which lowers to
  `$v *` with `$replicate` plus `$v +`.

## Correctness

4 PostConditions, all verified against a natively-compiled gcc golden reference
(`C[0]=0x18, C[17]=0x30, C[128]=0x18, C[255]=0xc0`), **0 `Error:` lines**.

## Results

| | scalar | **vectorized** |
|---|---|---|
| simulator ticks | 64 642 | **10 371 (6.23× faster)** |
| dynamic bundles | 46 624 | **7 472 (−84%)** |
| dynamic instructions | 70 916 | 15 327 |
| static bundles | 66 | 68 |
| vector instructions emitted | 0 | **8** (4 × `$v *`, 4 × `$v +`) |
| **dynamic IPB (whole program)** | **1.521** | **2.051** |
| whole-program occupancy | 19.0% | 25.6% |

Vector region alone: **16 bundles / 37 instructions, IPB 2.312** (28.9% of the
8-wide machine), realisation `unrolled`, kernel reported as `saxpy vi16 ×4` with
dynamic ops 560 → 74.

## Reading the IPB number

**IPB here is not a speed metric, and this kernel is the clearest example in the
project.** Before R9.1 (address value numbering) this same region measured
**IPB 6.13** — 23 bundles / 141 instructions. R9.1 removed ~104 redundant address
recomputations from it, giving **16 bundles / 37 instructions, IPB 2.312**.

So IPB fell 6.13 → 2.31 **while the kernel got 12.9% faster** (gemm vi16
11 905 → 10 365 ticks in the verification suite). IPB counts *instructions per
bundle*; deleting instructions lowers it. The honest speed metrics are ticks and
dynamic bundles, both of which improved.

The 8-wide machine's issue width is the ceiling; the vectorized program reaches
25.6% of it whole-program. That figure is dominated by the 256-element scalar
initialisation loop, which is ~84% of dynamic bundles and is not vectorized.

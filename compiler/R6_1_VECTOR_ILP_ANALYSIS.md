# R6.1 — Vector Backend ILP Analysis

**Milestone R6.1 — measurement only.** No optimization was performed. No scheduler, bundler, code generator, vectorizer, IR, legality or profitability file was modified. Everything below is measured on the code the frozen compiler actually emits.

* kernels analysed: **25** (25 with a measurable vector region) across **8** families
* bundle reconstruction verified against the production bundler on **25/25** programs (identical bundles)
* empty issue slots classified: **100%** (every slot inherits the single reason its bundle closed)

## 0. Answer first

Across the vectorized suite the shipped code issues **25.9%** of its dynamic issue slots (22536 instructions in 86944 slots); inside the hot vector loop bodies it issues **31.0%**.

The single highest-impact compiler optimization, by measurement, is **vector loop unrolling combined with memory disambiguation** — unrolling the vector loop body and giving the bundle packer the distinct-object information the IR-level analysis already has. Neither half is worth much alone; together they are worth **+36.7%** dynamic IPB across the whole suite (u=8), every number obtained by repacking the synthesised code with the production bundler.

The measurement that decides it: unrolling ALONE is worth only **1.24x** fewer bundles per iteration (range 1.00x..2.00x), because the bundler can prove two memory accesses disjoint only when they share a base register and differ in a constant offset — so every store serialises against the next copy's loads. With that one rule informed by distinct-object information, the same unroll is worth **2.57x** fewer bundles per iteration (range 1.00x..4.00x).

## 1. Method, and what is fact vs model

```
Scalar IR -> Vectorizer -> Vector IR -> [ R6.2+ optimizer ] -> Scheduler -> Bundler -> Codegen
                             ^                                        ^
                             |                                        |
             dependency_graph.py measures                occupancy.py measures
             the ILP that EXISTS                         the ILP that is DELIVERED
```

Each kernel is compiled through the real production path: the vectorizer, then the six-tier scalar optimizer ladder from `compiler.py` (first tier that compiles spill-free wins), then the R3.2 superblock pass, then codegen, then the scheduler and bundler. The winning tier is reported per kernel.

**FACT** (structural properties of the emitted code): 8 issue slots per bundle, 4 memory lanes, 1 divide/sqrt lane, 28 allocatable registers (`vector_capability_db`); every bundle's occupancy, instruction mix and closing reason; register lifetimes; execution frequencies from proved trip counts.

**MODEL** (relative weights, never a cycle count): per-instruction latency. APARA publishes no instruction timings and no cycle-accurate run has ever been made on this project, so `latency.py` reuses the frozen R2.4 weights rather than inventing new ones. Critical paths and MII bounds are therefore relative rankings, not cycles.

**Dynamic weighting.** R6 is graded on dynamic IPB, so every bundle carries an execution frequency: the product of the enclosing loops' proved trip counts (the header runs trip+1 times). Trip counts are read from the pre-optimizer vectorized IR, where the induction variable is still memory-backed and `analysis_iv` can prove them, and attached to the mcode by label.

## 2. Per-kernel reports

| kernel | family | realisation | static bundles | static IPB | dyn bundles | dyn IPB | dyn occupancy | body share of dyn bundles | crit path (model) |
|---|---|---|---|---|---|---|---|---|---|
| dot vi8 | dot | compact | 21 | 2.10 | 74 | 1.88 | 23.5% | 32% | 8 |
| dot vi16 | dot | compact | 21 | 2.14 | 130 | 1.88 | 23.5% | 37% | 8 |
| reduction vi8 | reduction | unrolled | 20 | 2.50 | 20 | 2.50 | 31.2% | 75% | n/a (no loop) |
| reduction vi32 | reduction | compact | 21 | 1.90 | 130 | 1.49 | 18.7% | 37% | 7 |
| elementwise add | elementwise | compact+peeled | 25 | 2.48 | 77 | 1.64 | 20.5% | 36% | 6 |
| elementwise mul | elementwise | compact+peeled | 25 | 2.48 | 77 | 1.64 | 20.5% | 36% | 6 |
| elementwise copy  *(no $v emitted)* | elementwise | unrolled | 25 | 2.24 | 50 | 1.68 | 21.0% | 26% | n/a (no loop) |
| expr a+b+c | expression | compact+peeled | 27 | 2.85 | 85 | 1.80 | 22.5% | 41% | 8 |
| expr a*b+c | expression | compact+peeled | 27 | 2.85 | 85 | 1.80 | 22.5% | 41% | 8 |
| expr (a+b)*c | expression | compact+peeled | 27 | 2.85 | 85 | 1.80 | 22.5% | 41% | 8 |
| expr a+b+c+d | expression | compact+peeled | 30 | 3.07 | 94 | 1.91 | 23.9% | 45% | 10 |
| axpy vi8 | axpy | compact | 20 | 1.70 | 87 | 1.32 | 16.5% | 46% | 8 |
| axpy vi16 | axpy | compact | 20 | 1.75 | 159 | 1.38 | 17.2% | 50% | 9 |
| axpy remainder | axpy | unrolled | 22 | 2.32 | 53 | 1.66 | 20.8% | 15% | n/a (no loop) |
| gemm vi8 16^3 | gemm | unrolled | 44 | 2.75 | 3667 | 1.73 | 21.7% | 63% | 16 |
| gemm vi8 8x8x32 | gemm | unrolled | 54 | 2.98 | 1399 | 1.97 | 24.6% | 69% | 28 |
| gemm vi16 | gemm | unrolled | 56 | 3.04 | 1471 | 2.11 | 26.4% | 70% | 29 |
| conv 3-tap | convolution | compact+peeled | 30 | 3.33 | 100 | 2.37 | 29.6% | 49% | 12 |
| conv 5-tap | convolution | compact+peeled | 30 | 3.67 | 112 | 2.63 | 32.9% | 56% | 16 |
| conv 7-tap | convolution | compact+peeled | 32 | 3.00 | 126 | 2.61 | 32.6% | 61% | 20 |
| conv 3-tap vi16 | convolution | compact+peeled | 25 | 2.56 | 101 | 2.23 | 27.8% | 55% | 13 |
| conv2d 3-point row | conv2d | unrolled | 52 | 2.98 | 573 | 1.99 | 24.8% | 22% | n/a (no loop) |
| conv2d 3x3 stencil | conv2d | compact | 72 | 2.88 | 627 | 3.61 | 45.2% | 46% | 20 |
| conv2d 3-point weighted | conv2d | unrolled | 52 | 3.50 | 635 | 3.08 | 38.4% | 20% | n/a (no loop) |
| conv2d 3-point vi16 | conv2d | compact | 44 | 3.07 | 851 | 2.18 | 27.3% | 59% | 14 |

`realisation` is R4.2.5's per-kernel choice between a compact vector loop and a fully unrolled chunk sequence; `body share` is the fraction of all dynamic bundles spent in the hot vector region — the Amdahl ceiling on anything that optimizes only that region.

### 2.1 Per-family statistics

| family | kernels | avg bundle occupancy (program) | avg bundle occupancy (vector body) | peak occupancy | critical path | avg ready queue | dependency depth |
|---|---|---|---|---|---|---|---|
| elementwise | 3 | 20.6% | 25.0% | 8 | 6.0 | 1.67 | 2.0 |
| axpy | 3 | 18.2% | 28.6% | 8 | 8.5 | 1.45 | 3.5 |
| reduction | 2 | 25.0% | 30.4% | 8 | 7.0 | 1.67 | 2.0 |
| dot | 2 | 23.5% | 37.5% | 6 | 8.0 | 2.33 | 2.0 |
| gemm | 3 | 24.2% | 26.9% | 8 | 24.3 | 2.07 | 12.3 |
| convolution | 4 | 30.8% | 33.8% | 8 | 15.2 | 3.64 | 6.8 |
| conv2d | 4 | 33.9% | 59.1% | 8 | 17.0 | 6.72 | 8.5 |
| expression | 4 | 22.9% | 20.2% | 8 | 8.5 | 1.76 | 3.2 |

Peak occupancy reaching 8 while the average sits near 2 is the whole story in one line: the machine can be filled, and almost never is.

### 2.2 Kernel statistics

| kernel | average bundle occupancy (vector body, dynamic) | peak occupancy | critical path (model) | average ready-queue size | max ready | dependency depth |
|---|---|---|---|---|---|---|
| dot vi8 | 37.5% | 4 | 8 | 2.33 | 5 | 2 |
| dot vi16 | 37.5% | 4 | 8 | 2.33 | 5 | 2 |
| reduction vi8 | 35.8% | 8 | n/a | n/a | n/a | n/a |
| reduction vi32 | 25.0% | 2 | 7 | 1.67 | 3 | 2 |
| elementwise add | 18.8% | 2 | 6 | 1.67 | 3 | 2 |
| elementwise mul | 18.8% | 2 | 6 | 1.67 | 3 | 2 |
| elementwise copy | 37.5% | 8 | n/a | n/a | n/a | n/a |
| expr a+b+c | 20.0% | 3 | 8 | 1.75 | 4 | 3 |
| expr a*b+c | 20.0% | 3 | 8 | 1.75 | 4 | 3 |
| expr (a+b)*c | 20.0% | 3 | 8 | 1.75 | 4 | 3 |
| expr a+b+c+d | 20.8% | 4 | 10 | 1.80 | 5 | 4 |
| axpy vi8 | 17.5% | 2 | 8 | 1.50 | 3 | 3 |
| axpy vi16 | 20.0% | 2 | 9 | 1.40 | 2 | 4 |
| axpy remainder | 48.4% | 8 | n/a | n/a | n/a | n/a |
| gemm vi8 16^3 | 25.0% | 5 | 16 | 1.89 | 5 | 8 |
| gemm vi8 8x8x32 | 26.7% | 4 | 28 | 2.07 | 7 | 14 |
| gemm vi16 | 28.9% | 4 | 29 | 2.25 | 7 | 15 |
| conv 3-tap | 30.4% | 5 | 12 | 3.17 | 6 | 5 |
| conv 5-tap | 34.7% | 5 | 16 | 3.62 | 8 | 7 |
| conv 7-tap | 37.5% | 5 | 20 | 4.50 | 10 | 9 |
| conv 3-tap vi16 | 32.8% | 5 | 13 | 3.29 | 6 | 6 |
| conv2d 3-point row | 44.5% | 6 | n/a | n/a | n/a | n/a |
| conv2d 3x3 stencil | 78.1% | 8 | 20 | 10.18 | 24 | 10 |
| conv2d 3-point weighted | 80.5% | 8 | n/a | n/a | n/a | n/a |
| conv2d 3-point vi16 | 33.3% | 4 | 14 | 3.25 | 6 | 7 |

`n/a` marks a kernel R4.2.5 realised as a fully UNROLLED chunk sequence: there is no loop left, so there is no loop body to build a recurrence graph over. Its bundles are still measured in full.

## 3. Per-bundle statistics

Every bundle of the hot vector region, slot by slot, with the reason it closed. `EMPTY` slots carry that reason.

### 3.1 axpy vi8 — hot region `0004:vcl_2_body`, executed 8x, 5 bundles/iteration

```
Bundle 10   2/8 issued   (aligner capacity 8)   closed by: waiting-for-vector-load
    slot0  VLOAD     $ld ($i64) $r9 [$r6 + $r3]
    slot1  VLOAD     $ld ($i64) $r10 [$r7 + $r3]
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 11   1/8 issued   (aligner capacity 1)   closed by: waiting-for-vector-multiply
    slot0  VMUL      $v * $r11 ($vi8) $r9 $r5 $replicate
    slot1  EMPTY     
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 12   1/8 issued   (aligner capacity 1)   closed by: waiting-for-vector-alu
    slot0  VADD      $v + $r12 ($vi8) $r10 $r11
    slot1  EMPTY     
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 13   1/8 issued   (aligner capacity 8)   closed by: store-ordering
    slot0  VSTORE    $st ($i64) [$r8 + $r3] $r12
    slot1  EMPTY     
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 14   2/8 issued   (aligner capacity 8)   closed by: region-boundary-label
    slot0  ALU       + $r3 ($i64) $r3 8
    slot1  BRANCH    ? ($i64) $r0 == $goto vcl_1_cond
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

```

### 3.2 dot vi8 — hot region `0004:vcl_2_body`, executed 8x, 3 bundles/iteration

```
Bundle 12   4/8 issued   (aligner capacity 8)   closed by: waiting-for-vector-load
    slot0  VLOAD     $ld ($i64) $r13 [$r11 + 0]
    slot1  VLOAD     $ld ($i64) $r14 [$r12 + 0]
    slot2  ALU       + $r15 ($i64) $r0 $r8
    slot3  ALU       + $r7 ($i64) $r7 8
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 13   3/8 issued   (aligner capacity 4)   closed by: waiting-for-reduction
    slot0  VDOT      $dot $accumulate $r15 ($vi8) $r13 $r14
    slot1  ALU       + $r11 ($i64) $r11 8
    slot2  ALU       + $r12 ($i64) $r12 8
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 14   2/8 issued   (aligner capacity 8)   closed by: region-boundary-label
    slot0  ALU       + $r8 ($i64) $r0 $r15
    slot1  BRANCH    ? ($i64) $r0 == $goto vcl_1_cond
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

```

### 3.3 gemm vi8 16^3 — hot region `0007:fb_6`, executed 256x, 9 bundles/iteration

```
Bundle 21   5/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  ALU       << $r21 ($i64) $r14 4
    slot1  ALU       + $r23 ($i64) $r20 $r14
    slot2  VLOAD     $ld ($i64) $r25 [$r7 + $r18]
    slot3  ALU       << $r30 ($i64) $r14 4
    slot4  ALU       + $r14 ($i64) $r14 1
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 22   3/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  ALU       + $r22 ($i64) $r21 0
    slot1  VLOAD     $ld ($i8) $r15 [$r5 + $r23]
    slot2  ALU       + $r1 ($i64) $r30 8
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 23   2/8 issued   (aligner capacity 8)   closed by: waiting-for-vector-load
    slot0  VLOAD     $ld ($i64) $r24 [$r6 + $r22]
    slot1  VLOAD     $ld ($i64) $r2 [$r9 + $r1]
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 24   2/8 issued   (aligner capacity 2)   closed by: waiting-for-vector-multiply
    slot0  VMUL      $v * $r29 ($vi8) $r24 $r15 $replicate
    slot1  VMUL      $v * $r13 ($vi8) $r2 $r15 $replicate
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 25   1/8 issued   (aligner capacity 1)   closed by: waiting-for-vector-alu
    slot0  VADD      $v + $r31 ($vi8) $r25 $r29
    slot1  EMPTY     
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 26   1/8 issued   (aligner capacity 8)   closed by: memory-dependence
    slot0  VSTORE    $st ($i64) [$r8 + $r18] $r31
    slot1  EMPTY     
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 27   1/8 issued   (aligner capacity 8)   closed by: waiting-for-vector-load
    slot0  VLOAD     $ld ($i64) $r12 [$r10 + $r19]
    slot1  EMPTY     
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 28   1/8 issued   (aligner capacity 1)   closed by: waiting-for-vector-alu
    slot0  VADD      $v + $r16 ($vi8) $r12 $r13
    slot1  EMPTY     
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 29   2/8 issued   (aligner capacity 8)   closed by: region-boundary-label
    slot0  VSTORE    $st ($i64) [$r11 + $r19] $r16
    slot1  BRANCH    ? ($i64) $r0 == $goto fc_5
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

```

### 3.4 conv2d 3x3 stencil — hot region `0007:vcl_2_body`, executed 18x, 16 bundles/iteration

```
Bundle 24   8/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  SET       $set $r8 0 64880
    slot1  ALU       + $r9 ($i64) $r0 -1
    slot2  SET       $set $r10 0 64880
    slot3  ALU       + $r11 ($i64) $r0 -1
    slot4  SET       $set $r12 0 64880
    slot5  ALU       + $r13 ($i64) $r0 -1
    slot6  ALU       << $r15 ($i64) $r3 5
    slot7  ALU       + $r18 ($i64) $r26 -320

Bundle 25   8/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  ALU       << $r9 ($i64) $r9 16
    slot1  ALU       << $r11 ($i64) $r11 16
    slot2  ALU       << $r13 ($i64) $r13 16
    slot3  ALU       + $r19 ($i64) $r26 -320
    slot4  ALU       << $r20 ($i64) $r3 5
    slot5  SET       $set $r22 0 64880
    slot6  ALU       + $r23 ($i64) $r0 -1
    slot7  SET       $set $r30 0 64880

Bundle 26   8/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  ALU       | $r8 ($i64) $r8 $r9
    slot1  ALU       | $r10 ($i64) $r10 $r11
    slot2  ALU       | $r12 ($i64) $r12 $r13
    slot3  ALU       << $r13 ($i64) $r3 5
    slot4  ALU       << $r23 ($i64) $r23 16
    slot5  ALU       + $r31 ($i64) $r0 -1
    slot6  ALU       + $r1 ($i64) $r26 -320
    slot7  ALU       + $r2 ($i64) $r3 1

Bundle 27   7/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  ALU       + $r7 ($i64) $r26 $r8
    slot1  ALU       + $r9 ($i64) $r26 $r10
    slot2  ALU       + $r11 ($i64) $r26 $r12
    slot3  ALU       | $r22 ($i64) $r22 $r23
    slot4  ALU       << $r31 ($i64) $r31 16
    slot5  ALU       << $r5 ($i64) $r2 5
    slot6  ALU       + $r6 ($i64) $r3 1
    slot7  EMPTY     

Bundle 28   5/8 issued   (aligner capacity 8)   closed by: waiting-for-scalar-load
    slot0  LOAD      $ld ($i32) $r8 [$r7 + 0]
    slot1  VLOAD     $ld ($i32) $r10 [$r9 + 0]
    slot2  VLOAD     $ld ($i32) $r12 [$r11 + 0]
    slot3  ALU       + $r21 ($i64) $r26 $r22
    slot4  ALU       | $r30 ($i64) $r30 $r31
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 29   8/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  ALU       + $r14 ($i64) $r13 $r8
    slot1  ALU       + $r16 ($i64) $r15 $r10
    slot2  ALU       + $r24 ($i64) $r20 $r12
    slot3  LOAD      $ld ($i32) $r25 [$r21 + 0]
    slot4  ALU       + $r29 ($i64) $r26 $r30
    slot5  SET       $set $r9 0 64880
    slot6  ALU       + $r11 ($i64) $r0 -1
    slot7  ALU       << $r13 ($i64) $r6 5

Bundle 30   7/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  ALU       + $r17 ($i64) $r14 1
    slot1  VLOAD     $ld ($i64) $r22 [$r18 + $r16]
    slot2  ALU       + $r30 ($i64) $r24 2
    slot3  LOAD      $ld ($i32) $r31 [$r29 + 0]
    slot4  ALU       << $r11 ($i64) $r11 16
    slot5  ALU       + $r15 ($i64) $r26 -320
    slot6  ALU       + $r20 ($i64) $r26 -320
    slot7  EMPTY     

Bundle 31   8/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  VLOAD     $ld ($i64) $r23 [$r19 + $r17]
    slot1  VLOAD     $ld ($i64) $r4 [$r1 + $r30]
    slot2  ALU       | $r9 ($i64) $r9 $r11
    slot3  ALU       + $r11 ($i64) $r5 $r25
    slot4  ALU       + $r14 ($i64) $r13 $r31
    slot5  ALU       + $r18 ($i64) $r3 1
    slot6  ALU       + $r5 ($i64) $r0 -1
    slot7  SET       $set $r31 0 64896

Bundle 32   8/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  ALU       + $r7 ($i64) $r26 $r9
    slot1  VADD      $v + $r9 ($vi8) $r22 $r23
    slot2  VLOAD     $ld ($i64) $r10 [$r15 + $r11]
    slot3  ALU       + $r19 ($i64) $r14 1
    slot4  ALU       << $r17 ($i64) $r18 5
    slot5  ALU       + $r1 ($i64) $r26 -320
    slot6  SET       $set $r22 0 64880
    slot7  ALU       + $r23 ($i64) $r0 -1

Bundle 33   7/8 issued   (aligner capacity 8)   closed by: waiting-for-scalar-load
    slot0  LOAD      $ld ($i32) $r8 [$r7 + 0]
    slot1  VADD      $v + $r16 ($vi8) $r9 $r4
    slot2  VLOAD     $ld ($i64) $r12 [$r20 + $r19]
    slot3  ALU       << $r23 ($i64) $r23 16
    slot4  ALU       << $r5 ($i64) $r5 16
    slot5  ALU       + $r9 ($i64) $r0 -1
    slot6  ALU       + $r4 ($i64) $r0 -1
    slot7  EMPTY     

Bundle 34   7/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  ALU       + $r21 ($i64) $r17 $r8
    slot1  VADD      $v + $r24 ($vi8) $r16 $r10
    slot2  ALU       | $r22 ($i64) $r22 $r23
    slot3  SET       $set $r23 0 64880
    slot4  ALU       << $r7 ($i64) $r3 5
    slot5  ALU       << $r9 ($i64) $r9 16
    slot6  ALU       << $r4 ($i64) $r4 16
    slot7  EMPTY     

Bundle 35   5/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  ALU       + $r29 ($i64) $r21 2
    slot1  ALU       + $r2 ($i64) $r26 $r22
    slot2  ALU       | $r23 ($i64) $r23 $r5
    slot3  ALU       | $r31 ($i64) $r31 $r9
    slot4  SET       $set $r9 0 64880
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 36   6/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  VLOAD     $ld ($i64) $r30 [$r1 + $r29]
    slot1  ALU       + $r22 ($i64) $r26 $r23
    slot2  VADD      $v + $r23 ($vi8) $r24 $r12
    slot3  LOAD      $ld ($i32) $r5 [$r2 + 0]
    slot4  ALU       + $r13 ($i64) $r26 $r31
    slot5  ALU       | $r9 ($i64) $r9 $r4
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 37   4/8 issued   (aligner capacity 8)   closed by: waiting-for-scalar-load
    slot0  LOAD      $ld ($i64) $r25 [$r22 + 0]
    slot1  VADD      $v + $r6 ($vi8) $r23 $r30
    slot2  ALU       + $r15 ($i64) $r7 $r5
    slot3  ALU       + $r31 ($i64) $r26 $r9
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 38   2/8 issued   (aligner capacity 8)   closed by: waiting-for-address-alu
    slot0  ALU       + $r11 ($i64) $r25 8
    slot1  VSTORE    $st ($i64) [$r13 + $r15] $r6
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

Bundle 39   2/8 issued   (aligner capacity 8)   closed by: region-boundary-label
    slot0  STORE     $st ($i64) [$r31 + 0] $r11
    slot1  BRANCH    ? ($i64) $r0 == $goto vcl_1_cond
    slot2  EMPTY     
    slot3  EMPTY     
    slot4  EMPTY     
    slot5  EMPTY     
    slot6  EMPTY     
    slot7  EMPTY     

```

The axpy body is the canonical case: two loads, a multiply, an add and a store, each in its own bundle, six of eight slots idle every time. The 3x3 stencil is the counter-example — a body large enough to fill bundles reaches 78% occupancy with the SAME scheduler and bundler, which is the direct evidence that the loss is a shortage of independent work, not a backend defect.

## 4. Occupancy histograms

Whole programs, STATIC (one count per emitted bundle):

```
  1 instr          321   39.1%  ################
  2 instr          221   26.9%  ###########
  3 instr           68    8.3%  ###
  4 instr           61    7.4%  ###
  5 instr           25    3.0%  #
  6 instr           15    1.8%  #
  7 instr           11    1.3%  #
  8 instr          100   12.2%  #####
```

Whole programs, DYNAMIC (each bundle weighted by its execution count):

```
  1 instr         5310   48.9%  ####################
  2 instr         3003   27.6%  ###########
  3 instr         1107   10.2%  ####
  4 instr          582    5.4%  ##
  5 instr          374    3.4%  #
  6 instr           67    0.6%  
  7 instr          101    0.9%  
  8 instr          324    3.0%  #
```

Hot VECTOR BODIES only, dynamic:

```
  1 instr         1698   28.0%  ###########
  2 instr         2293   37.8%  ###############
  3 instr          957   15.8%  ######
  4 instr          441    7.3%  ###
  5 instr          345    5.7%  ##
  6 instr           60    1.0%  
  7 instr           81    1.3%  #
  8 instr          185    3.1%  #
```

**65.9%** of dynamic vector-body bundles issue two instructions or fewer.

Per-family dynamic bundle occupancy:

| family | 1/8 | 2/8 | 3/8 | 4/8 | 5/8 | 6/8 | 7/8 | 8/8 |
|---|---|---|---|---|---|---|---|---|
| elementwise | 51% | 42% | 1% | 1% | 0% | 0% | 1% | 3% |
| axpy | 45% | 51% | 2% | 2% | 0% | 1% | 0% | 1% |
| reduction | 11% | 81% | 2% | 0% | 2% | 2% | 0% | 3% |
| dot | 0% | 33% | 33% | 33% | 0% | 0% | 0% | 0% |
| gemm | 31% | 42% | 18% | 3% | 6% | 0% | 0% | 0% |
| convolution | 34% | 11% | 11% | 31% | 11% | 0% | 0% | 0% |
| conv2d | 8% | 26% | 11% | 19% | 6% | 6% | 8% | 17% |
| expression | 62% | 19% | 14% | 5% | 0% | 0% | 0% | 0% |

## 5. Dependency graphs (vector IR, one loop body)

Node tags: `[V]` vector operation, `[M]` memory, `[C]` loop-carried edge. Edges are `target(kind,latency)`; `lat` is the model latency and `h` the latency-weighted height (its distance to the end of the body).

### axpy vi8 — `vcl_1_cond`, 6 operations, 16 edges (8 loop-carried)

```
  [M] n13   lat=3  h=8   IRLoad         -> 15(RAW,3), 18(WAR,0)
  [M] n14   lat=3  h=6   IRLoad         -> 16(RAW,3), 17(MEM_WAR,0), 18(WAR,0)
  [V] n15   lat=2  h=5   IRVecArith     -> 16(RAW,2)
  [V] n16   lat=2  h=3   IRVecArith     -> 17(RAW,2)
  [M] n17   lat=1  h=1   IRStore        -> 18(WAR,0)
  [ ] n18   lat=1  h=1   IRBinOp        -> (sink)
  [C] n18 ==RAW/1==> n13   (LOOP-CARRIED)
  [C] n18 ==RAW/1==> n14   (LOOP-CARRIED)
  [C] n18 ==RAW/1==> n17   (LOOP-CARRIED)
  [C] n15 ==WAR/0==> n13   (LOOP-CARRIED)
  [C] n16 ==WAR/0==> n14   (LOOP-CARRIED)
  [C] n16 ==WAR/0==> n15   (LOOP-CARRIED)
  [C] n17 ==WAR/0==> n16   (LOOP-CARRIED)
  [C] n17 ==MEM_RAW/1==> n14   (LOOP-CARRIED)
```

work 12 / span 8 = available parallelism **1.50**; edge census {'RAW': 7, 'WAR': 7, 'MEM_RAW': 1, 'MEM_WAR': 1}

### dot vi8 — `vcl_1_cond`, 7 operations, 12 edges (6 loop-carried)

```
  [M] n18   lat=3  h=8   IRLoad         -> 20(RAW,3), 23(WAR,0)
  [M] n19   lat=3  h=8   IRLoad         -> 20(RAW,3), 24(WAR,0)
  [V] n20   lat=4  h=5   IRVecDot       -> 21(WAR,0), 21(RAW,4)
  [ ] n21   lat=1  h=1   IRAssign       -> (sink)
  [ ] n22   lat=1  h=1   IRBinOp        -> (sink)
  [ ] n23   lat=1  h=1   IRBinOp        -> (sink)
  [ ] n24   lat=1  h=1   IRBinOp        -> (sink)
  [C] n21 ==RAW/1==> n20   (LOOP-CARRIED)
  [C] n23 ==RAW/1==> n18   (LOOP-CARRIED)
  [C] n24 ==RAW/1==> n19   (LOOP-CARRIED)
  [C] n20 ==WAR/0==> n18   (LOOP-CARRIED)
  [C] n20 ==WAR/0==> n19   (LOOP-CARRIED)
  [C] n21 ==WAR/0==> n20   (LOOP-CARRIED)
```

work 14 / span 8 = available parallelism **1.75**; edge census {'RAW': 6, 'WAR': 6}

### conv 3-tap — `vcl_1_cond`, 17 operations, 40 edges (20 loop-carried)

```
  [M] n15   lat=3  h=3   IRLoad         -> 33(MEM_WAR,0)
  [M] n18   lat=3  h=12  IRLoad         -> 21(RAW,3), 33(MEM_WAR,0)
  [M] n19   lat=3  h=12  IRLoad         -> 22(RAW,3), 33(MEM_WAR,0)
  [M] n20   lat=3  h=10  IRLoad         -> 25(RAW,3), 33(MEM_WAR,0)
  [ ] n21   lat=1  h=9   IRBinOp        -> 23(RAW,1)
  [ ] n22   lat=1  h=9   IRBinOp        -> 24(RAW,1)
  [M] n23   lat=3  h=8   IRLoad         -> 27(RAW,3)
  [M] n24   lat=3  h=8   IRLoad         -> 27(RAW,3)
  [ ] n25   lat=1  h=7   IRBinOp        -> 26(RAW,1)
  [M] n26   lat=3  h=6   IRLoad         -> 30(RAW,3)
  [V] n27   lat=2  h=5   IRVecArith     -> 30(RAW,2)
  [M] n28   lat=3  h=5   IRLoad         -> 31(RAW,3), 33(MEM_WAR,0)
  [M] n29   lat=3  h=4   IRLoad         -> 32(RAW,3), 33(MEM_WAR,0)
  [V] n30   lat=2  h=3   IRVecArith     -> 32(RAW,2)
  [ ] n31   lat=1  h=2   IRBinOp        -> 33(RAW,1)
  [M] n32   lat=1  h=1   IRStore        -> (sink)
  [M] n33   lat=1  h=1   IRStore        -> (sink)
  [C] n21 ==WAR/0==> n18   (LOOP-CARRIED)
  [C] n22 ==WAR/0==> n19   (LOOP-CARRIED)
  [C] n25 ==WAR/0==> n20   (LOOP-CARRIED)
  [C] n23 ==WAR/0==> n21   (LOOP-CARRIED)
  [C] n24 ==WAR/0==> n22   (LOOP-CARRIED)
  [C] n27 ==WAR/0==> n23   (LOOP-CARRIED)
  [C] n27 ==WAR/0==> n24   (LOOP-CARRIED)
  [C] n26 ==WAR/0==> n25   (LOOP-CARRIED)
  [C] n30 ==WAR/0==> n26   (LOOP-CARRIED)
  [C] n30 ==WAR/0==> n27   (LOOP-CARRIED)
  [C] n31 ==WAR/0==> n28   (LOOP-CARRIED)
  [C] n32 ==WAR/0==> n29   (LOOP-CARRIED)
  [C] n32 ==WAR/0==> n30   (LOOP-CARRIED)
  [C] n33 ==WAR/0==> n31   (LOOP-CARRIED)
  [C] n33 ==MEM_RAW/1==> n15   (LOOP-CARRIED)
  [C] n33 ==MEM_RAW/1==> n18   (LOOP-CARRIED)
  [C] n33 ==MEM_RAW/1==> n19   (LOOP-CARRIED)
  [C] n33 ==MEM_RAW/1==> n20   (LOOP-CARRIED)
  [C] n33 ==MEM_RAW/1==> n28   (LOOP-CARRIED)
  [C] n33 ==MEM_RAW/1==> n29   (LOOP-CARRIED)
```

work 37 / span 12 = available parallelism **3.08**; edge census {'RAW': 14, 'WAR': 14, 'MEM_RAW': 6, 'MEM_WAR': 6}

### elementwise add — `vcl_1_cond`, 5 operations, 12 edges (6 loop-carried)

```
  [M] n11   lat=3  h=6   IRLoad         -> 13(RAW,3), 15(WAR,0)
  [M] n12   lat=3  h=6   IRLoad         -> 13(RAW,3), 15(WAR,0)
  [V] n13   lat=2  h=3   IRVecArith     -> 14(RAW,2)
  [M] n14   lat=1  h=1   IRStore        -> 15(WAR,0)
  [ ] n15   lat=1  h=1   IRBinOp        -> (sink)
  [C] n15 ==RAW/1==> n11   (LOOP-CARRIED)
  [C] n15 ==RAW/1==> n12   (LOOP-CARRIED)
  [C] n15 ==RAW/1==> n14   (LOOP-CARRIED)
  [C] n13 ==WAR/0==> n11   (LOOP-CARRIED)
  [C] n13 ==WAR/0==> n12   (LOOP-CARRIED)
  [C] n14 ==WAR/0==> n13   (LOOP-CARRIED)
```

work 10 / span 6 = available parallelism **1.67**; edge census {'RAW': 6, 'WAR': 6}

Note what the census shows: the carried WAR edges are name reuse, not dataflow — they disappear under renaming and are exactly what a software pipeliner or an unroller removes. The carried RAW edges are the induction variable; the carried MEM_RAW edges are the store-then-load pair of the SAME element, which is intra-iteration in truth and only appears carried because the disambiguator will not prove the next chunk distinct.

## 6. Critical path analysis

| kernel | ops | work (sum lat) | span (true deps) | span (all deps) | depth (hops) | parallelism = work/span | ResMII | RecMII (register) | RecMII (all carried edges) | bundles/iter shipped |
|---|---|---|---|---|---|---|---|---|---|---|
| dot vi8 | 7 | 14 | 8 | 8 | 2 | 1.75 | 1 | 5 | 5 | 3 |
| dot vi16 | 7 | 14 | 8 | 8 | 2 | 1.75 | 1 | 5 | 5 | 3 |
| reduction vi32 | 5 | 9 | 7 | 7 | 2 | 1.29 | 1 | 1 | 3 | 3 |
| elementwise add | 5 | 10 | 6 | 7 | 2 | 1.67 | 1 | 1 | 6 | 4 |
| elementwise mul | 5 | 10 | 6 | 7 | 2 | 1.67 | 1 | 1 | 6 | 4 |
| expr a+b+c | 7 | 15 | 8 | 9 | 3 | 1.88 | 1 | 1 | 8 | 5 |
| expr a*b+c | 7 | 15 | 8 | 9 | 3 | 1.88 | 1 | 1 | 8 | 5 |
| expr (a+b)*c | 7 | 15 | 8 | 9 | 3 | 1.88 | 1 | 1 | 8 | 5 |
| expr a+b+c+d | 9 | 20 | 10 | 11 | 4 | 2.00 | 2 | 1 | 10 | 6 |
| axpy vi8 | 6 | 12 | 8 | 9 | 3 | 1.50 | 1 | 1 | 8 | 5 |
| axpy vi16 | 7 | 13 | 9 | 9 | 4 | 1.44 | 1 | 1 | 8 | 5 |
| gemm vi8 16^3 | 17 | 31 | 16 | 16 | 8 | 1.94 | 3 | 1 | 12 | 9 |
| gemm vi8 8x8x32 | 31 | 57 | 28 | 28 | 14 | 2.04 | 4 | 1 | 24 | 15 |
| gemm vi16 | 36 | 62 | 29 | 29 | 15 | 2.14 | 5 | 1 | 24 | 16 |
| conv 3-tap | 17 | 37 | 12 | 12 | 5 | 3.08 | 3 | 1 | 5 | 7 |
| conv 5-tap | 25 | 55 | 16 | 16 | 7 | 3.44 | 4 | 1 | 5 | 9 |
| conv 7-tap | 33 | 73 | 20 | 20 | 9 | 3.65 | 5 | 1 | 5 | 11 |
| conv 3-tap vi16 | 21 | 41 | 13 | 13 | 6 | 3.15 | 3 | 1 | 5 | 8 |
| conv2d 3x3 stencil | 61 | 96 | 20 | 20 | 10 | 4.80 | 8 | 1 | 5 | 16 |
| conv2d 3-point vi16 | 24 | 44 | 14 | 14 | 7 | 3.14 | 3 | 1 | 5 | 9 |

Available parallelism below 2 means a single iteration of the body is very nearly a straight dependence chain: there is essentially nothing for a scheduler to interleave WITHIN one iteration. Every kernel whose ResMII is 1 could in principle run one bundle per iteration — the whole body fits in one bundle's worth of lanes — and every one of them ships at three to seven.

## 7. Ready-queue analysis

Ideal ready-set list schedule of the body under TRUE dependences only (infinite registers, real lane caps): how many operations are ready at each scheduling step.

| kernel | ops | avg ready | max ready | ideal steps | ideal IPB | bundles/iter shipped |
|---|---|---|---|---|---|---|
| dot vi8 | 7 | 2.33 | 5 | 3 | 2.33 | 3 |
| dot vi16 | 7 | 2.33 | 5 | 3 | 2.33 | 3 |
| reduction vi32 | 5 | 1.67 | 3 | 3 | 1.67 | 3 |
| elementwise add | 5 | 1.67 | 3 | 3 | 1.67 | 4 |
| elementwise mul | 5 | 1.67 | 3 | 3 | 1.67 | 4 |
| expr a+b+c | 7 | 1.75 | 4 | 4 | 1.75 | 5 |
| expr a*b+c | 7 | 1.75 | 4 | 4 | 1.75 | 5 |
| expr (a+b)*c | 7 | 1.75 | 4 | 4 | 1.75 | 5 |
| expr a+b+c+d | 9 | 1.80 | 5 | 5 | 1.80 | 6 |
| axpy vi8 | 6 | 1.50 | 3 | 4 | 1.50 | 5 |
| axpy vi16 | 7 | 1.40 | 2 | 5 | 1.40 | 5 |
| gemm vi8 16^3 | 17 | 1.89 | 5 | 9 | 1.89 | 9 |
| gemm vi8 8x8x32 | 31 | 2.07 | 7 | 15 | 2.07 | 15 |
| gemm vi16 | 36 | 2.25 | 7 | 16 | 2.25 | 16 |
| conv 3-tap | 17 | 3.17 | 6 | 6 | 2.83 | 7 |
| conv 5-tap | 25 | 3.62 | 8 | 8 | 3.12 | 9 |
| conv 7-tap | 33 | 4.50 | 10 | 10 | 3.30 | 11 |
| conv 3-tap vi16 | 21 | 3.29 | 6 | 7 | 3.00 | 8 |
| conv2d 3x3 stencil | 61 | 10.18 | 24 | 11 | 5.55 | 16 |
| conv2d 3-point vi16 | 24 | 3.25 | 6 | 8 | 3.00 | 9 |

Distribution of ready-set sizes over all vector loop bodies:

```
  1 ready     76   58.0%  #######################
  2 ready      6    4.6%  ##
  3 ready      8    6.1%  ##
  4 ready     11    8.4%  ###
  5 ready      8    6.1%  ##
  6 ready      8    6.1%  ##
  7 ready      3    2.3%  #
  8 ready     11    8.4%  ###
```

**62.6%** of scheduling steps have at most two ready operations on an 8-wide machine. This is the measurement that rules out every scheduling-only optimization: a better scheduler cannot issue instructions that are not ready.

## 8. Empty-slot classification — 100% attributed

Every empty issue slot is attributed to the single reason its bundle closed. The categories come from the packer's own if/elif cascade (exactly one branch fires per rejection), refined for dependences by asking WHICH instruction in the bundle produced the value that was waited on. The partition is exhaustive by construction: the counts sum to the total.

### 8.1 whole programs

| cause | family | static empty slots | static % | dynamic empty slots | dynamic % | meaning |
|---|---|---|---|---|---|---|
| waiting-for-address-alu | dependence | 1787 | 41.8% | 21521 | 33.4% | waiting for scalar address / IV arithmetic |
| region-boundary-label | region | 785 | 18.4% | 10842 | 16.8% | a label starts a new bundle (no cross-block scheduling) |
| waiting-for-vector-alu | dependence | 219 | 5.1% | 8527 | 13.2% | waiting for a vector ALU result |
| waiting-for-vector-load | dependence | 154 | 3.6% | 7430 | 11.5% | waiting for a vector load result |
| region-boundary-control | region | 224 | 5.2% | 5691 | 8.8% | a control transfer ends the bundle |
| memory-dependence | dependence | 387 | 9.1% | 4944 | 7.7% | may-alias memory dependence (store -> access) |
| waiting-for-vector-multiply | dependence | 51 | 1.2% | 2491 | 3.9% | waiting for a vector multiply |
| waiting-for-scalar-load | dependence | 185 | 4.3% | 1700 | 2.6% | waiting for a scalar load result |
| store-ordering | bundler | 310 | 7.3% | 891 | 1.4% | store ordering: aligner memory-phase rule |
| waiting-for-reduction | dependence | 16 | 0.4% | 216 | 0.3% | waiting for a reduction ($dot/$vreduce) dependency |
| no-ready-instruction | region | 150 | 3.5% | 150 | 0.2% | no instruction left in the stream |
| memory-lanes-full | bundler | 5 | 0.1% | 5 | 0.0% | all 4 load/store lanes occupied |
| **total** |  | 4273 | 100.0% | 64408 | 100.0% |  |

### 8.2 hot vector bodies

| cause | family | static empty slots | static % | dynamic empty slots | dynamic % | meaning |
|---|---|---|---|---|---|---|
| waiting-for-vector-alu | dependence | 219 | 20.6% | 8527 | 25.5% | waiting for a vector ALU result |
| waiting-for-vector-load | dependence | 154 | 14.5% | 7430 | 22.2% | waiting for a vector load result |
| waiting-for-address-alu | dependence | 211 | 19.8% | 5359 | 16.0% | waiting for scalar address / IV arithmetic |
| memory-dependence | dependence | 135 | 12.7% | 4692 | 14.0% | may-alias memory dependence (store -> access) |
| region-boundary-label | region | 155 | 14.6% | 3685 | 11.0% | a label starts a new bundle (no cross-block scheduling) |
| waiting-for-vector-multiply | dependence | 51 | 4.8% | 2491 | 7.5% | waiting for a vector multiply |
| store-ordering | bundler | 96 | 9.0% | 544 | 1.6% | store ordering: aligner memory-phase rule |
| waiting-for-scalar-load | dependence | 21 | 2.0% | 480 | 1.4% | waiting for a scalar load result |
| waiting-for-reduction | dependence | 16 | 1.5% | 216 | 0.6% | waiting for a reduction ($dot/$vreduce) dependency |
| memory-lanes-full | bundler | 5 | 0.5% | 5 | 0.0% | all 4 load/store lanes occupied |
| **total** |  | 1063 | 100.0% | 33429 | 100.0% |  |

### 8.3 Grouped by family of cause (dynamic, hot vector bodies)

| cause family | dynamic empty slots | share |  |
|---|---|---|---|
| dependence | 29195 | 87.3% | ########################## |
| region | 3685 | 11.0% | ### |
| bundler | 549 | 1.6% |  |

* **dependence** — the next instruction needed a value produced in this bundle. Real program structure; only more independent work (unrolling, pipelining, reassociation) removes it.
* **bundler** — a rule of the packer or aligner, not a dataflow fact: the memory-phase rule that keeps a store apart from a later write of its address register, the 4-lane memory limit, the divide lane, the call/SP rule.
* **region** — a label or a control transfer ended the bundle. This is the cost of scheduling one basic block at a time.
* **register** — a WAW hazard, i.e. a name reused with no renaming.

### 8.4 Second, orthogonal decomposition: encoded vs issue-only

Of 33429 dynamic empty slots in the vector bodies, **24037 (71.9%)** are ENCODED — the aligner pads the bundle to an 8-word capacity because it contains a load, store, branch or divide, so the nulls occupy instruction memory as well as issue slots — and **9392 (28.1%)** are ISSUE-ONLY (capacity 1/2/4). This is a different question from *why* the slot is empty and is counted separately so no slot is double-attributed. It explains the +57.4% code-size cost R4.6.5 measured: a one-instruction bundle holding a load still costs eight words.

## 9. Ranked optimization opportunities

Every estimate below is produced by re-running the PRODUCTION scheduler and bundle packer on a synthesised instruction stream — not by a cost formula. `suite gain` is the mean relative dynamic-IPB gain over ALL 25 kernels (a kernel the transform does not apply to contributes zero); `where it applies` averages only over the kernels it fires on.

| # | optimization | difficulty | suite dynamic-IPB gain | where it applies | kernels | dynamic bundles removed |
|---|---|---|---|---|---|---|
| 1 | Vector software pipelining (MII bound) | high | +37.3% | +46.7% | 20 | +28.4% |
| 2 | Vector loop unrolling + disambiguation (u=8) | high | +36.7% | +36.7% | 25 | +28.9% |
| 3 | Vector loop unrolling + memory disambiguation (u=4) | high | +34.4% | +34.4% | 25 | +27.4% |
| 4 | Memory disambiguation alone (no unrolling) | medium | +13.9% | +13.9% | 25 | +11.2% |
| 5 | Multiple accumulators (u=4, reduction kernels) | medium | +5.7% | +5.7% | 25 | +9.6% |
| 6 | Vector loop unrolling alone (u=4, no disambiguation) | medium | +3.3% | +3.3% | 25 | +7.6% |
| 7 | Reduction-tree reassociation | medium | +0.3% | +1.7% | 5 | +1.2% |
| 8 | Latency-aware / bundle-aware local scheduling | low | +0.1% | +0.1% | 25 | +0.1% |

The ranking key is the MEASURED rows. Read them in this order: unrolling alone (#6) and disambiguation alone (#4) are each worth little; TOGETHER they are worth an order of magnitude more than either, because the unroll supplies the independent work and the disambiguation lets it share bundles.

> The software-pipelining row is a **bound, not a measurement**: it is the MII (max of the resource and register-recurrence lower bounds) from the measured dependence graph, i.e. the best any modulo schedule could reach. It is listed for calibration — it says the ceiling is high — but it is not a schedule that has been produced, whereas every unrolling row IS a bundle count the production packer actually produced.

### 9.1 Evidence — bundles per iteration, measured by the real packer

| kernel | shipped | model u=1 | u=4 unroll only | u=1 + disambiguation | u=4 + disambiguation | u=4 + disamb + accumulators | u=8 + disamb + accumulators | registers short at u=4 |
|---|---|---|---|---|---|---|---|---|
| dot vi8 | 3 | 3.00 | 3.00 | 3.00 | 3.00 | 1.25 | 1.12 | 0 |
| dot vi16 | 3 | 3.00 | 3.00 | 3.00 | 3.00 | 1.25 | 1.12 | 0 |
| reduction vi8 | 15 | 15.00 | 15.00 | 12.00 | 7.00 | 7.00 | 6.12 | 90 |
| reduction vi32 | 3 | 3.00 | 1.50 | 3.00 | 1.50 | 0.75 | 0.75 | 0 |
| elementwise add | 4 | 3.00 | 3.00 | 3.00 | 1.00 | 1.00 | 1.00 | 0 |
| elementwise mul | 4 | 3.00 | 3.00 | 3.00 | 1.00 | 1.00 | 1.00 | 0 |
| elementwise copy | 13 | 13.00 | 13.00 | 7.00 | 6.25 | 6.25 | 6.12 | 84 |
| expr a+b+c | 5 | 4.00 | 4.00 | 4.00 | 1.50 | 1.50 | 1.25 | 2 |
| expr a*b+c | 5 | 4.00 | 4.00 | 4.00 | 1.50 | 1.50 | 1.25 | 2 |
| expr (a+b)*c | 5 | 4.00 | 4.00 | 4.00 | 1.50 | 1.50 | 1.25 | 2 |
| expr a+b+c+d | 6 | 5.00 | 5.00 | 5.00 | 2.00 | 2.00 | 1.62 | 11 |
| axpy vi8 | 5 | 4.00 | 4.00 | 4.00 | 1.25 | 1.25 | 1.00 | 0 |
| axpy vi16 | 5 | 4.00 | 4.00 | 4.00 | 1.25 | 1.25 | 1.00 | 0 |
| axpy remainder | 8 | 8.00 | 8.00 | 6.00 | 4.25 | 4.25 | 4.12 | 73 |
| gemm vi8 16^3 | 9 | 7.00 | 7.00 | 4.00 | 2.50 | 2.50 | 2.25 | 41 |
| gemm vi8 8x8x32 | 15 | 13.00 | 13.00 | 8.00 | 4.50 | 4.50 | 4.25 | 20 |
| gemm vi16 | 16 | 14.00 | 13.25 | 11.00 | 5.25 | 5.25 | 4.75 | 20 |
| conv 3-tap | 7 | 6.00 | 6.00 | 5.00 | 3.00 | 3.00 | 2.75 | 42 |
| conv 5-tap | 9 | 8.00 | 8.00 | 7.00 | 4.50 | 4.50 | 4.00 | 39 |
| conv 7-tap | 11 | 10.00 | 10.00 | 9.00 | 5.50 | 5.50 | 5.00 | 27 |
| conv 3-tap vi16 | 8 | 6.00 | 6.00 | 5.00 | 4.00 | 4.00 | 4.00 | 50 |
| conv2d 3-point row | 16 | 12.00 | 9.00 | 11.00 | 8.00 | 8.00 | 7.50 | 30 |
| conv2d 3x3 stencil | 16 | 16.00 | 13.25 | 15.00 | 13.00 | 13.00 | 12.62 | 81 |
| conv2d 3-point weighted | 16 | 15.00 | 13.50 | 14.00 | 13.25 | 13.25 | 13.12 | 72 |
| conv2d 3-point vi16 | 9 | 7.00 | 7.00 | 6.00 | 3.75 | 3.75 | 3.38 | 31 |

For a kernel R4.2.5 realised as an UNROLLED chunk sequence the body already covers eight vector chunks, so `u=4` there means a 32-chunk body -- which is why those rows report tens of registers short. Their realistic transform is not more unrolling but the accumulator and reassociation work of 9.2/9.3.

Read the two middle columns together. Unrolling without disambiguation barely moves: the copies cannot share bundles because copy 0's store and copy 1's loads use different base registers, and `bundler._mem_may_alias` treats different base registers as a possible alias by design (two registers may hold the same address). Adding distinct-object information — which the IR-level R2.2 `MemoryDisambiguator` already computes for the IR scheduler, but which never reaches the bundle packer — is what unlocks the unroll.

### 9.2 Evidence — where multiple accumulators matter

| kernel | family | u=4 shared accumulator | u=4 independent accumulators | extra gain |
|---|---|---|---|---|
| dot vi8 | dot | 3.00 | 1.25 | 2.40x |
| dot vi16 | dot | 3.00 | 1.25 | 2.40x |
| reduction vi32 | reduction | 1.50 | 0.75 | 2.00x |

Renaming the loop-carried accumulator per copy is legal only because the operation is associative; it is the classic reassociation trade (a different, equally valid summation order) and it is the difference between a reduction that overlaps and one that does not.

### 9.3 Evidence — reduction-tree reassociation

| kernel | chain length | tree depth | bundles now | bundles as a tree | gain | registers short |
|---|---|---|---|---|---|---|
| dot vi8 | - | - | 3 | - | no-reduction-chain | - |
| dot vi16 | - | - | 3 | - | no-reduction-chain | - |
| reduction vi8 | 8 | 4 | 15 | 13 | 1.15x | 1 |
| reduction vi32 | - | - | 3 | - | no-reduction-chain | - |
| elementwise add | - | - | 4 | - | no-reduction-chain | - |
| elementwise mul | - | - | 4 | - | no-reduction-chain | - |
| elementwise copy | - | - | 13 | - | no-reduction-chain | - |
| expr a+b+c | - | - | 5 | - | no-reduction-chain | - |
| expr a*b+c | - | - | 5 | - | no-reduction-chain | - |
| expr (a+b)*c | - | - | 5 | - | no-reduction-chain | - |
| expr a+b+c+d | 3 | 2 | 6 | 5 | 1.20x | 0 |
| axpy vi8 | - | - | 5 | - | no-reduction-chain | - |
| axpy vi16 | - | - | 5 | - | no-reduction-chain | - |
| axpy remainder | - | - | 8 | - | no-reduction-chain | - |
| gemm vi8 16^3 | - | - | 9 | - | no-reduction-chain | - |
| gemm vi8 8x8x32 | - | - | 15 | - | no-reduction-chain | - |
| gemm vi16 | - | - | 16 | - | no-reduction-chain | - |
| conv 3-tap | - | - | 7 | - | no-reduction-chain | - |
| conv 5-tap | 4 | 3 | 9 | 9 | 1.00x | 0 |
| conv 7-tap | 6 | 3 | 11 | 12 | 0.92x | 1 |
| conv 3-tap vi16 | - | - | 8 | - | no-reduction-chain | - |
| conv2d 3-point row | - | - | 16 | - | no-reduction-chain | - |
| conv2d 3x3 stencil | 5 | 3 | 16 | 18 | 0.89x | 0 |
| conv2d 3-point weighted | - | - | 16 | - | no-reduction-chain | - |
| conv2d 3-point vi16 | - | - | 9 | - | no-reduction-chain | - |

The reduction kernels sum eight partial results in a SEVEN-STEP SERIAL CHAIN because mem2reg leaves the recurrence in SSA form — each partial sum in a fresh register — and nothing reassociates it. A balanced tree is the same instruction count at logarithmic depth. The measured gain is small only because those kernels are already fully unrolled, so the chain overlaps with the loads that feed it; the transform is cheap and it composes with unrolling rather than competing with it.

### 9.4 Evidence — why no scheduling-only optimization is ranked high

| kernel | bundles shipped | lower bound for ANY local schedule | headroom |
|---|---|---|---|
| dot vi8 | 3 | 3 | 0 |
| dot vi16 | 3 | 3 | 0 |
| reduction vi8 | 15 | 15 | 0 |
| reduction vi32 | 3 | 3 | 0 |
| elementwise add | 4 | 4 | 0 |
| elementwise mul | 4 | 4 | 0 |
| elementwise copy | 13 | 12 | 1 |
| expr a+b+c | 5 | 5 | 0 |
| expr a*b+c | 5 | 5 | 0 |
| expr (a+b)*c | 5 | 5 | 0 |
| expr a+b+c+d | 6 | 6 | 0 |
| axpy vi8 | 5 | 5 | 0 |
| axpy vi16 | 5 | 5 | 0 |
| axpy remainder | 8 | 9 | 0 |
| gemm vi8 16^3 | 9 | 9 | 0 |
| gemm vi8 8x8x32 | 15 | 15 | 0 |
| gemm vi16 | 16 | 16 | 0 |
| conv 3-tap | 7 | 7 | 0 |
| conv 5-tap | 9 | 9 | 0 |
| conv 7-tap | 11 | 11 | 0 |
| conv 3-tap vi16 | 8 | 8 | 0 |
| conv2d 3-point row | 16 | 16 | 0 |
| conv2d 3x3 stencil | 16 | 16 | 0 |
| conv2d 3-point weighted | 16 | 15 | 1 |
| conv2d 3-point vi16 | 9 | 9 | 0 |

The bound is the largest of three quantities that hold for every legal schedule of the block: ceil(N/8) for the issue width, ceil(memory ops/4) for the lanes, and the longest chain of instruction pairs that can never share a bundle. The shipped schedule already meets it almost everywhere. Latency-aware scheduling, bundle-aware scheduling and better tie-breaks therefore have essentially nothing to win: the existing R2.3/R2.4 list scheduler is already extracting all the local ILP that exists. **The problem is the supply of independent work, not the scheduling of it.**

## 10. Secondary findings (measured, not the main recommendation)

### 10.1 Duplicate base registers

codegen materialises `FP + constant` separately for every array reference, so one object routinely occupies several registers at once. Two costs follow: the instructions themselves, and the loss of the only disjointness proof the bundler has (same base register, different constant offsets).

| kernel | base registers that duplicate another |
|---|---|
| elementwise copy | 13 |
| conv 3-tap vi16 | 7 |
| axpy remainder | 6 |
| dot vi8 | 4 |
| reduction vi8 | 4 |
| elementwise add | 4 |
| elementwise mul | 4 |
| dot vi16 | 3 |
| reduction vi32 | 3 |
| conv2d 3-point row | 3 |
| axpy vi8 | 2 |
| axpy vi16 | 2 |

The vi8 reduction is the extreme case: eight registers are loaded with the SAME address `FP-64`, one per chunk, filling an entire bundle with identical address arithmetic.

### 10.2 Register headroom

| kernel | registers used | peak live | free in the loop region | avg lifetime (instrs) | max lifetime |
|---|---|---|---|---|---|
| dot vi8 | 22 | 10 | 18 | 12.2 | 38 |
| dot vi16 | 23 | 9 | 18 | 11.8 | 39 |
| reduction vi8 | 31 | 17 | 0 | 16.5 | 45 |
| reduction vi32 | 20 | 8 | 20 | 10.7 | 34 |
| elementwise add | 31 | 24 | 16 | 25.5 | 57 |
| elementwise mul | 31 | 22 | 16 | 24.8 | 57 |
| elementwise copy | 31 | 22 | 0 | 19.7 | 51 |
| expr a+b+c | 31 | 30 | 13 | 39.8 | 72 |
| expr a*b+c | 31 | 30 | 13 | 39.9 | 72 |
| expr (a+b)*c | 31 | 30 | 13 | 39.6 | 72 |
| expr a+b+c+d | 31 | 30 | 10 | 55.5 | 87 |
| axpy vi8 | 18 | 9 | 16 | 9.8 | 28 |
| axpy vi16 | 19 | 9 | 15 | 10.3 | 29 |
| axpy remainder | 31 | 18 | 2 | 15.1 | 46 |
| gemm vi8 16^3 | 31 | 24 | 1 | 53.8 | 113 |
| gemm vi8 8x8x32 | 31 | 29 | 1 | 88.5 | 153 |
| gemm vi16 | 31 | 29 | 1 | 96.3 | 163 |
| conv 3-tap | 31 | 30 | 0 | 64.7 | 95 |
| conv 5-tap | 31 | 30 | 0 | 76.1 | 105 |
| conv 7-tap | 31 | 30 | 0 | 62.6 | 91 |
| conv 3-tap vi16 | 31 | 20 | 1 | 25.4 | 59 |
| conv2d 3-point row | 31 | 30 | 0 | 84.2 | 148 |
| conv2d 3x3 stencil | 31 | 30 | 0 | 135.6 | 200 |
| conv2d 3-point weighted | 31 | 30 | 0 | 127.5 | 174 |
| conv2d 3-point vi16 | 31 | 22 | 2 | 62.4 | 129 |

This is the constraint on how far the top-ranked transform can go: kernels with a large unrolled body have zero registers left, so an unroller must run BEFORE register allocation (on the vector IR, where values are unlimited temporaries) rather than as a peephole on mcode.

### 10.3 The store-ordering rule

`store-ordering` alone accounts for 544 dynamic empty slots in the vector bodies. It is the aligner's memory-phase rule (a store may not share a bundle with a later instruction that writes a register the store addresses with), which in a vector loop means the induction-variable increment can never join the store's bundle. It is a real hardware-imposed rule, not a compiler defect, but an unroller sidesteps it: with u copies there are u-1 other stores and loads available to fill that bundle instead.

## 11. Threats to validity

* **Latency is a model, not hardware.** No cycle-accurate simulation has ever been run on this project. Critical paths, RecMII and the pipelining bound rank alternatives; they do not predict cycles. Occupancy, instruction mixes, empty-slot causes and bundle counts do not depend on latency at all.
* **Dynamic IPB here is bundle-weighted, not simulated.** Frequencies come from statically proved trip counts, so a kernel whose trip count cannot be proved is excluded from the dynamic totals rather than guessed at.
* **The what-if experiments synthesise code; they do not compile it.** They rewrite the shipped instruction stream and re-run the real scheduler and packer, so the bundle counts are real packer output for real instruction sequences — but no differential oracle has proved the synthesised sequences equivalent, because they are measurements, not candidate code. The transforms they stand for (unrolling, reassociation, multiple accumulators) all need the usual R1.x/R4.x legality and differential machinery when they are actually built.
* **The disambiguation experiment models a capability the compiler already has at IR level** (`loopopt.depgraph_disambig`, R2.2) but loses at mcode level. Whether that information can be carried to the packer cheaply is an engineering question R6.2 must answer; the measurement only says what it would be worth.
* **Unroll factors assume the trip count divides.** The remainder framework R4.4.5 already exists, and its cost is not charged in these projections.
* **25 hand-written kernels** across 8 families are a characterisation suite, not a workload mix. Six families are reused verbatim from the frozen R4.6.5 suite; the 2-D convolution family is taken from `conv_corpus.py`.
* **One kernel (`elementwise copy`) emits no vector operation at all** — a packed copy is pure wide memory movement — so its 'vector region' is its hot block, and it is flagged rather than silently counted as a vector kernel.

## 12. Reproduction

```sh
cd compiler
python3 -m vector_backend.ilp_analysis        # regenerates this report
python3 _r6_1_test.py                         # unit + invariant tests
```

The analysis imports the compiler but never modifies it; running it leaves the repository byte-identical.

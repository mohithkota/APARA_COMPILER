# APARA_COMPILER

A C compiler targeting the APARA accelerator's custom ISA — takes a (preprocessed) C source
file and emits APARA mcode text, ready to be aligned/assembled/run by the accelerator's own
toolchain. **Overall completeness: ~88% of a basic C compiler** (see
[`compiler/STATUS.md`](compiler/STATUS.md) for the full, dated change history and current
known limitations).

## Pipeline

```
input.c → [gcc -E preprocess] → pycparser AST → Three-Address IR → register allocation
        → APARA mcode → VLIW bundling → <name>.mcode (+ data.map, run.sh)
```

This repo only contains the **Python codegen pipeline** — it produces text (`.mcode`,
`data.map`, etc.) and stops there. It does not invoke an assembler or simulator itself;
turning `.mcode` into something that runs (aligning, assembling, disassembling, executing)
is a separate manual step using the accelerator's own binary toolchain (`mcode_align`,
`mcode_assemble`, `mcode_disassemble`, `mcode_run` — see each test directory's `run.sh`).

## Repo layout

```
compiler/               compiler source (the important part)
├── compiler.py          entry point — CLI, preprocessing, data.map + result-file generation
├── ir.py                 37 IR node class definitions
├── ir_gen.py             C AST → Three-Address IR (pycparser NodeVisitor)
├── codegen.py             IR → APARA mcode (v6: 28-register dynamic allocator + spilling)
├── bundler.py             VLIW bundle optimizer (RAW/WAW hazard detection, greedy packing)
└── STATUS.md              dated project log — what's done, what's blocked, what's next

alu/, array/, branch/, ldst/, pointer/, mem_march/   hardware-verified test programs
new_isa_tests/           one test per ISA instruction/intrinsic
not_used_files/          superseded scaffolding kept for reference, not part of the active pipeline
logs/                     run output logs
```

Each test directory follows the same pattern: a `.c` source, a generated `.mcode`, a
`data.map`, a `run.sh` that drives the external assembler/simulator toolchain, and a
`clean.sh` to wipe generated artifacts.

## Usage

```bash
python3 compiler/compiler.py input.c -o out.mcode -v
python3 compiler/compiler.py input.c --global-base 0x200 --stack-top 0x1000
python3 compiler/compiler.py input.c --preprocess   # force gcc -E first
```

Then, from the relevant test directory, run `./run.sh` to align/assemble/disassemble/execute
the generated mcode on the accelerator toolchain and capture a `.result`/`.log`.

## What's working (hardware-verified)

ALU (all 12 ops incl. NOR/NAND/XNOR), load/store, if/else + all 6 comparisons, while/for/do-while,
switch/case, 1D and 2D arrays, struct member access (incl. nested), pointer arithmetic,
function calls/recursion, register spilling (28-register pool + 64-slot spill area), and the
VLIW bundling optimizer (30–56% instruction-count reduction). Full instruction-by-instruction
coverage and feature table in [`compiler/STATUS.md`](compiler/STATUS.md).

## What's not done yet

- Function pointers — IR/codegen scaffolding exists but blocked at the assembler level (no way
  to load a function's absolute address into a register; see STATUS.md for details)
- Float arithmetic (+,-,*,/) — only `$fsqrt` is wired up via intrinsic so far
- Sub-word load/store (`$i32`/`$i16`/`$i8`) — blocked by an engine hardware bug; all loads/stores
  use full 8-byte-aligned `$i64` words (stride=8)
- Variadic functions; string literals are address-of only

## History recovery

Branch [`history`](../../tree/history) and tags `v1`/`v2`/`v3` capture earlier drafts of the
`compiler/` core files, recovered from Claude Code's internal edit-backup cache after this
project was developed for a while without version control. `v1`/`v2` are best-effort
reconstructions (only files that were edited more than once via the `Edit` tool had any prior
state to recover) — `main` is the authoritative, current line of history.

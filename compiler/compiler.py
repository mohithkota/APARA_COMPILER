#!/usr/bin/env python3
"""
APARA C Compiler
Usage:
    python3 compiler.py input.c
    python3 compiler.py input.c -o out.mcode -v
    python3 compiler.py input.c --global-base 0x200 --stack-top 0x1000
    python3 compiler.py input.c --preprocess   # run gcc -E first
"""

import sys, os, argparse, subprocess, tempfile

# vu8_t/vi8_t/vu16_t/vi16_t/vu32_t/vi32_t are opt-in markers for naturally-strided
# (packed, no 8-byte-per-element padding) arrays -- needed so $ld/$st ($u128)/
# ($u256) can see N tightly-packed bytes as N vector elements. Plain char/short/
# int arrays are UNCHANGED (still padded to 8 bytes/element) unless declared
# with one of these specific type names. See ir_gen.py _is_packed_array_decl.
_FAKE_TYPEDEFS = """
typedef int size_t; typedef int ptrdiff_t;
typedef unsigned int uint32_t;   typedef int          int32_t;
typedef unsigned char uint8_t;   typedef signed char  int8_t;
typedef unsigned short uint16_t; typedef short        int16_t;
typedef unsigned long long uint64_t; typedef long long int64_t;
typedef float float32_t; typedef double float64_t;
typedef unsigned char vu8_t;   typedef signed char  vi8_t;
typedef unsigned short vu16_t; typedef short        vi16_t;
typedef unsigned int vu32_t;   typedef int          vi32_t;
"""


def preprocess(src_file):
    for cpp in ('gcc -E -P', 'cc -E -P', 'cpp -P'):
        try:
            cmd = cpp.split() + [src_file, '-o', '-']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return result.stdout, True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    with open(src_file) as f:
        lines = [ln for ln in f if not ln.strip().startswith('#')]
    return ''.join(lines), False


# ── data.map generator ────────────────────────────────────────────────────────

def build_data_map(ir_globals):
    """
    Build a DMEM word image from IRGlobalDecl initial values.

    DMEM is 64-bit wide (8 bytes per word).
    word_index = byte_addr // 8
    Multiple narrow elements (e.g. int32) within the same 64-bit word are
    packed by shifting according to their byte offset within the word.

    Returns a dict {word_index: 64-bit int}.
    """
    dmem = {}

    for gd in ir_globals:
        stride = getattr(gd, 'stride', max(gd.elem_bytes, 8))
        n_elems = max(1, gd.total_bytes // max(stride, 1))
        inits = gd.init if gd.init else [0] * n_elems
        for i, val in enumerate(inits):
            byte_addr = gd.dmem_addr + i * stride
            word_idx  = byte_addr // 8
            # All stores are ($i64) — value occupies the full 64-bit word
            dmem[word_idx] = int(val) & _MASK64

    # mcode_run requires at least word 0 to be present
    dmem.setdefault(0, 0)
    return dmem


def write_data_map(path, dmem):
    """Write data.map file from a {word_index: value} dict."""
    with open(path, 'w') as f:
        for idx in sorted(dmem):
            f.write(f"0x{idx:x}: 0x{dmem[idx]:016x}\n")


# ── IR constant-folding interpreter → result file ─────────────────────────────

def _operand(val, temps):
    """Resolve a Const or Temp operand to a Python int, or None if unknown."""
    from ir import Const, Temp
    if isinstance(val, Const):
        return int(val.value)
    if isinstance(val, Temp):
        return temps.get(val.name)
    if isinstance(val, int):
        return val
    return None


_MASK64 = (1 << 64) - 1

_OPS = {
    '+':  lambda a, b: (a + b) & _MASK64,
    '-':  lambda a, b: (a - b) & _MASK64,
    '*':  lambda a, b: (a * b) & _MASK64,
    '/':  lambda a, b: int(a / b) if b else None,
    '%':  lambda a, b: a % b if b else None,
    '&':  lambda a, b: a & b,
    '|':  lambda a, b: a | b,
    '^':  lambda a, b: a ^ b,
    '<<': lambda a, b: (a << b) & _MASK64,
    '>>': lambda a, b: a >> b,
    '==': lambda a, b: int(a == b),
    '!=': lambda a, b: int(a != b),
    '<':  lambda a, b: int(a < b),
    '>':  lambda a, b: int(a > b),
    '<=': lambda a, b: int(a <= b),
    '>=': lambda a, b: int(a >= b),
}


def eval_ir(instructions):
    """
    Walk the IR and constant-fold to determine the final DMEM state and
    the return value of main().

    Returns (return_value, final_dmem) where:
      return_value  — int or None if undetermined
      final_dmem    — dict {word_index: value} of globals written during execution

    Returns (None, {}) if the program is too dynamic to evaluate statically
    (e.g. contains conditional branches or indirect calls).
    """
    from ir import (IRGlobalDecl, IRFuncBegin, IRFuncEnd, IRLabel,
                    IRAssign, IRBinOp, IRUnaryOp,
                    IRGlobalLoad, IRGlobalStore,
                    IRCondJump, IRJump, IRCall, IRReturn, IRHalt,
                    Const, Temp)

    temps      = {}   # name -> int
    byte_mem   = {}   # byte_addr -> value (element-sized int)
    elem_sizes = {}   # byte_addr -> elem_bytes (needed for big-endian packing)
    ret_val    = None
    # `instructions` is a FLAT list covering every function's body back to
    # back (main's is interspersed with everyone else's, in declaration
    # order) -- evaluation must be scoped to ONLY main's body, or the first
    # IRReturn from any function declared before main gets mistaken for
    # main's own return. Confirmed real bug, not hypothetical: test_spill.c
    # defines f01()..f30() before main, and the old unscoped version broke
    # on f01's "return 1" immediately, reporting r1=1 instead of the
    # correct 465 -- silently, since this only LOOKED like a working
    # static-eval result. See STATUS.md 2026-06-20.
    in_main = False

    for ir in instructions:
        # ── initialise globals (top-level, not inside any function) ──
        if isinstance(ir, IRGlobalDecl):
            stride = getattr(ir, 'stride', ir.elem_bytes)
            n_el = max(1, ir.total_bytes // max(stride, 1))
            inits = ir.init if ir.init else [0] * n_el
            for i, v in enumerate(inits):
                addr = ir.dmem_addr + i * stride
                byte_mem[addr]   = int(v)
                elem_sizes[addr] = ir.elem_bytes
            continue

        if isinstance(ir, IRFuncBegin):
            in_main = (ir.name == 'main')
            continue
        if isinstance(ir, IRFuncEnd):
            in_main = False
            continue
        if not in_main:
            continue  # some other function's body -- not ours to evaluate

        if isinstance(ir, (IRLabel, IRHalt)):
            continue

        # ── assignments ──
        if isinstance(ir, IRAssign):
            v = _operand(ir.src, temps)
            if v is None:
                return None, {}
            temps[ir.dest.name] = v
            continue

        if isinstance(ir, IRBinOp):
            a = _operand(ir.left, temps)
            b = _operand(ir.right, temps)
            if a is None or b is None:
                return None, {}
            fn = _OPS.get(ir.op)
            if fn is None:
                return None, {}
            result = fn(a, b)
            if result is None:
                return None, {}
            temps[ir.dest.name] = result
            continue

        if isinstance(ir, IRUnaryOp):
            v = _operand(ir.operand, temps)
            if v is None:
                return None, {}
            if ir.op == '-':
                temps[ir.dest.name] = (-v) & _MASK64
            elif ir.op == '~':
                temps[ir.dest.name] = (~v) & _MASK64
            elif ir.op == '!':
                temps[ir.dest.name] = int(v == 0)
            else:
                return None, {}
            continue

        # ── global memory ──
        if isinstance(ir, IRGlobalLoad):
            off = _operand(ir.offset, temps)
            if off is None:
                return None, {}
            # off is already a DMEM byte offset (stride-scaled by ir_gen)
            addr = ir.dmem_addr + off
            v = byte_mem.get(addr, 0)
            temps[ir.dest.name] = v
            continue

        if isinstance(ir, IRGlobalStore):
            off = _operand(ir.offset, temps)
            src = _operand(ir.src, temps)
            if off is None or src is None:
                return None, {}
            addr = ir.dmem_addr + off
            byte_mem[addr]   = src
            elem_sizes[addr] = ir.elem_bytes
            continue

        # ── control flow — give up on branches ──
        if isinstance(ir, (IRCondJump, IRJump)):
            return None, {}

        # ── function calls ──
        if isinstance(ir, IRCall):
            return None, {}

        # ── return ──
        if isinstance(ir, IRReturn):
            ret_val = _operand(ir.value, temps) if ir.value is not None else 0
            break

    # All stores are ($i64) — value is the full 64-bit DMEM word (stride=8, byte_off=0)
    final_dmem = {}
    for byte_addr, val in byte_mem.items():
        word_idx = byte_addr // 8
        final_dmem[word_idx] = int(val) & _MASK64

    return ret_val, final_dmem


# Stable location relative to this file -- see isa_coverage_tests/golden/
# golden_stubs.h's own header for what's in it and why each piece is there
# ("no bias": every implementation is derived from the ISA spec / confirmed
# hardware behavior, never from reading this compiler's own source).
_GOLDEN_STUBS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '..', 'isa_coverage_tests', 'golden', 'golden_stubs.h')


def try_golden_verify(source, ir_globals, out_dir, base_name):
    """
    If this test follows the results[] convention (a global literally named
    "results"), get independent ground truth by compiling `source` natively
    with gcc against golden_stubs.h and running it, then write a REAL
    .result file from the captured values -- one "mem" PostCondition line
    per results[] slot, in the format confirmed against the simulator
    (STATUS.md 2026-06-20).

    This compiler never computes the expected values itself; it only
    supplies the DMEM address it assigned to results[] (from ir_globals,
    already known internally -- no extra subprocess round-trip needed).

    Returns True if it wrote a real golden .result (falls back to the
    existing static-eval/placeholder path on any failure -- missing gcc,
    missing golden_stubs.h, a test that doesn't use this convention, or a
    genuine compile/run error in the native build, which gets printed
    so it's never a silent fallback).
    """
    results_global = next((g for g in ir_globals if g.name == 'results'), None)
    if results_global is None:
        return False
    if not os.path.isfile(_GOLDEN_STUBS_PATH):
        return False

    n_results = results_global.total_bytes // results_global.elem_bytes
    base_word = results_global.dmem_addr // 8

    try:
        with tempfile.TemporaryDirectory(prefix='golden_verify_') as scratch:
            driver_path = os.path.join(scratch, '_driver.c')
            with open(driver_path, 'w') as f:
                f.write(f'#include "{_GOLDEN_STUBS_PATH}"\n')
                f.write('#define main __test_main\n')
                f.write(source)
                f.write('\n#undef main\n#include <stdio.h>\n')
                f.write('int main(void) {\n')
                f.write('    __test_main();\n')
                f.write(f'    for (int i = 0; i < {n_results}; i++) {{\n')
                f.write('        printf("%016llx\\n", (unsigned long long) results[i]);\n')
                f.write('    }\n    return 0;\n}\n')

            bin_path = os.path.join(scratch, '_driver')
            cc = subprocess.run(['gcc', '-O0', '-o', bin_path, driver_path],
                                 capture_output=True, text=True)
            if cc.returncode != 0:
                print(f"\n[GOLDEN VERIFY] gcc failed -- falling back to placeholder .result:\n{cc.stderr}")
                return False

            run = subprocess.run([bin_path], capture_output=True, text=True)
            if run.returncode != 0:
                print(f"\n[GOLDEN VERIFY] native binary exited {run.returncode} -- falling back:\n{run.stderr}")
                return False

            out_lines = [l.strip() for l in run.stdout.strip().splitlines() if l.strip()]
            if len(out_lines) != n_results:
                print(f"\n[GOLDEN VERIFY] expected {n_results} result lines, got "
                      f"{len(out_lines)} -- falling back")
                return False
    except FileNotFoundError:
        return False  # gcc not installed

    result_path = os.path.join(out_dir, f'{base_name}.result')
    with open(result_path, 'w') as f:
        for i, val_hex in enumerate(out_lines):
            f.write(f"0 mem 0x{base_word + i:x} 0x{val_hex}\n")
    print(f"      golden    →  {result_path}  ({n_results} independently-verified "
          f"PostCondition checks via gcc + golden_stubs.h)")
    return True


def write_result_file(path, ret_val, final_dmem, init_dmem):
    """
    Write result.txt.

    Format empirically confirmed against the real simulator binary
    (engine_isp/assembler/bin/mcode_run) -- see compiler/STATUS.md: the
    PostCondition keyword ("reg"/"mem") must be the SECOND token, with a
    leading thread-id first (always 0 here, single-threaded programs):
      0 reg 0xN 0xVALUE          — expected value in register N (r1 = return value)
      0 mem 0xWORD_IDX 0xVALUE   — expected final DMEM word values
                                   (only for globals that changed from init)
    """
    lines = []
    if ret_val is not None:
        lines.append(f"0 reg 0x1 0x{ret_val & _MASK64:016x}")

    # emit mem lines for globals whose value changed during execution
    for word_idx in sorted(final_dmem):
        if word_idx == 0:
            continue   # skip the placeholder word
        val = final_dmem[word_idx]
        if val != init_dmem.get(word_idx, 0):
            lines.append(f"0 mem 0x{word_idx:x} 0x{val:016x}")

    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


# ── shell script generators ───────────────────────────────────────────────────

def write_run_script(script_path, mcode_name):
    content = f"""\
#!/bin/bash
cd "$(dirname "$0")"
BIN_DIR=/home/mohithkota/complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin
ALIGN=$BIN_DIR/mcode_align
AS=$BIN_DIR/mcode_assemble
DISAS=$BIN_DIR/mcode_disassemble
RUN=$BIN_DIR/mcode_run

NAME={mcode_name}

[ -f $NAME.golden ] && cp $NAME.golden $NAME.result
$ALIGN  $NAME.mcode           > $NAME.aligned.mcode
$AS     $NAME.aligned.mcode   > $NAME.obj
$DISAS  $NAME.obj             > $NAME.disass.mcode
$RUN -p 0x0 -i $NAME.obj -d data.map -r $NAME.result -v 2>&1 | tee $NAME.log
"""
    with open(script_path, 'w') as f:
        f.write(content)
    os.chmod(script_path, 0o755)


def write_clean_script(script_path):
    content = """\
#!/bin/bash
rm -f *.aligned.mcode *.obj *.disass.mcode *.log
"""
    with open(script_path, 'w') as f:
        f.write(content)
    os.chmod(script_path, 0o755)


# ── main compile entry point ──────────────────────────────────────────────────

def compile_c_to_mcode(c_file, output_file=None, verbose=False,
                       do_preprocess=False, global_base=0x400, stack_top=0x7FF8,
                       no_startup=False):
    try:
        import pycparser
    except ImportError:
        print("ERROR: pycparser not installed.\nRun: pip install pycparser")
        sys.exit(1)

    from ir_gen  import IRGenerator
    from codegen import CodeGen
    from ir      import IRGlobalDecl

    # ── Source ──
    # Always preprocess: strips comments, #defines, #includes.
    # do_preprocess flag is kept for backwards compat but no longer changes behaviour.
    source, used_cpp = preprocess(c_file)
    if verbose and used_cpp:
        print("[preprocessed with system cpp]")

    # ── Parse ──
    parser = pycparser.CParser()
    try:
        ast = parser.parse(_FAKE_TYPEDEFS + source, filename=c_file)
    except Exception as e:
        print(f"\n[PARSE ERROR] {e}")
        print("Tip: use --preprocess if your code has #include/#define")
        sys.exit(1)

    if verbose:
        print("=== pycparser AST ===")
        ast.show()
        print()

    # ── IR ──
    ir_gen = IRGenerator(global_base=global_base)
    ir_gen.visit(ast)

    # Global data and the stack share the same 64KB DMEM: globals grow UP from
    # global_base, the stack grows DOWN from stack_top. If the global area's
    # end address is at or past stack_top, they overlap from the very start
    # (found the hard way: a 64x64 matmul's globals reached past the default
    # stack_top and silently corrupted stack data -- no error, no crash, just
    # wrong results in whichever array elements happened to land in the
    # overlap). A 4KB margin below stack_top is a conservative bound on
    # worst-case frame usage (locals + 224B caller-save + 512B spill reserve
    # is typically well under 1KB per call; 4KB leaves room for a few levels
    # of nesting/recursion without claiming to bound it exactly).
    if not no_startup:
        STACK_SAFETY_MARGIN = 4096
        if ir_gen._next_global + STACK_SAFETY_MARGIN > stack_top:
            print(f"\n[COMPILE ERROR] Global data (0x{global_base:x}-0x{ir_gen._next_global:x}, "
                  f"{ir_gen._next_global - global_base} bytes) leaves less than "
                  f"{STACK_SAFETY_MARGIN} bytes of clearance before --stack-top (0x{stack_top:x}). "
                  f"Globals and the stack would silently corrupt each other.")
            print(f"Tip: pass a larger --stack-top, e.g. --stack-top "
                  f"0x{min(0xFFF8, ir_gen._next_global + STACK_SAFETY_MARGIN + 8):x} "
                  f"(must stay within the 64KB DMEM, max usable top is ~0xFFF8).")
            sys.exit(1)

    if verbose:
        print("=== Three-Address IR ===")
        for i in ir_gen.instructions:
            print(f"  {i}")
        print()

    # ── Codegen ──
    cg   = CodeGen(global_base=global_base)
    body = cg.generate(ir_gen.instructions, global_base=global_base)

    header = ""
    if not no_startup:
        header = cg.startup_code(global_base=global_base, stack_top=stack_top)

    mcode = header + body

    # ── VLIW bundle optimisation ──
    from bundler import bundle_mcode
    mcode, n_before, n_after = bundle_mcode(mcode)

    # ── Output paths — all files go into a named subfolder ──
    c_base = os.path.splitext(os.path.basename(c_file))[0]
    if output_file is None:
        out_dir     = os.path.join(os.path.dirname(os.path.abspath(c_file)), c_base)
        base_name   = c_base
        output_file = os.path.join(out_dir, base_name + '.mcode')
    else:
        out_dir   = os.path.dirname(os.path.abspath(output_file))
        base_name = os.path.splitext(os.path.basename(output_file))[0]

    os.makedirs(out_dir, exist_ok=True)
    data_map_path  = os.path.join(out_dir, 'data.map')
    result_path    = os.path.join(out_dir, f'{base_name}.result')
    run_script     = os.path.join(out_dir, 'run.sh')
    clean_script   = os.path.join(out_dir, 'clean.sh')

    # ── Build data.map from global initial values ──
    ir_globals = [i for i in ir_gen.instructions if isinstance(i, IRGlobalDecl)]
    init_dmem  = build_data_map(ir_globals)

    # ── Constant-fold IR to get final register/memory state ──
    ret_val, final_dmem = eval_ir(ir_gen.instructions)

    # ── Write files ──
    with open(output_file, 'w') as f:
        f.write(mcode)

    write_data_map(data_map_path, init_dmem)
    write_run_script(run_script, base_name)
    write_clean_script(clean_script)

    # Real, independent verification takes priority whenever the test
    # follows the results[] convention; only fall back to static-eval /
    # an empty placeholder when it doesn't (or golden verification can't
    # run -- see try_golden_verify's own docstring for exactly when).
    golden_done = try_golden_verify(source, ir_globals, out_dir, base_name)
    if not golden_done:
        if ret_val is not None or final_dmem:
            write_result_file(result_path, ret_val, final_dmem, init_dmem)
        else:
            # Program is too dynamic to evaluate statically.
            # Write truly empty result file — assembler checks nothing.
            # Fill in reg/mem lines manually after first hardware run.
            with open(result_path, 'w') as f:
                f.write("")

    # ── Summary ──
    n_globals = len(ir_globals)
    n_funcs   = sum(1 for i in ir_gen.instructions if type(i).__name__ == 'IRFuncBegin')
    reduction = int(100 * (n_before - n_after) / n_before) if n_before else 0
    print(f"[OK]  {c_file}  →  {output_file}")
    print(f"      globals={n_globals}  functions={n_funcs}  gbase=0x{global_base:x}  stack=0x{stack_top:x}")
    print(f"      bundles: {n_before} → {n_after}  ({reduction}% reduction)")
    if n_globals:
        print(f"      global area: 0x{global_base:x} – 0x{ir_gen._next_global:x}")
    print(f"      data.map  →  {data_map_path}  ({len(init_dmem)} words)")
    if golden_done:
        pass  # try_golden_verify already printed its own summary line
    elif ret_val is not None:
        print(f"      result    →  {result_path}  (r1 = 0x{ret_val:x} = {ret_val})")
    else:
        print(f"      result    →  {result_path}  (placeholder — dynamic program)")
    print(f"      run.sh    →  {run_script}")

    return output_file


def main():
    ap = argparse.ArgumentParser(description="APARA C Compiler")
    ap.add_argument('input')
    ap.add_argument('-o', '--output')
    ap.add_argument('-v', '--verbose', action='store_true')
    ap.add_argument('--preprocess', action='store_true',
                    help='Run gcc -E first (handles #include/#define)')
    ap.add_argument('--global-base', type=lambda x: int(x, 0), default=0x400,
                    help='DMEM byte address for global variables (default 0x400)')
    ap.add_argument('--stack-top',  type=lambda x: int(x, 0), default=0x7FF8,
                    help='Initial stack pointer value (default 0x7FF8)')
    ap.add_argument('--no-startup', action='store_true')
    a = ap.parse_args()

    if not os.path.isfile(a.input):
        print(f"ERROR: file not found: {a.input}")
        sys.exit(1)

    compile_c_to_mcode(a.input, a.output, a.verbose, a.preprocess,
                       a.global_base, a.stack_top, a.no_startup)


if __name__ == '__main__':
    main()

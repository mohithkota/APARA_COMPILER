#!/usr/bin/env python3
"""
APARA C Compiler
Usage:
    python3 compiler.py input.c
    python3 compiler.py input.c -o out.mcode -v
    python3 compiler.py input.c --global-base 0x200 --stack-top 0x1000
    python3 compiler.py input.c --preprocess   # run gcc -E first
"""

import sys, os, argparse, subprocess

_FAKE_TYPEDEFS = """
typedef int size_t; typedef int ptrdiff_t;
typedef unsigned int uint32_t;   typedef int          int32_t;
typedef unsigned char uint8_t;   typedef signed char  int8_t;
typedef unsigned short uint16_t; typedef short        int16_t;
typedef unsigned long long uint64_t; typedef long long int64_t;
typedef float float32_t; typedef double float64_t;
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
        inits = gd.init if gd.init else [0] * max(1, gd.total_bytes // max(gd.elem_bytes, 1))
        for i, val in enumerate(inits):
            byte_addr = gd.dmem_addr + i * gd.elem_bytes
            word_idx  = byte_addr // 8
            byte_off  = byte_addr % 8
            mask      = (1 << (gd.elem_bytes * 8)) - 1
            packed    = (int(val) & mask) << (byte_off * 8)
            dmem[word_idx] = dmem.get(word_idx, 0) | packed

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

    temps   = {}   # name -> int
    byte_mem = {}  # byte_addr -> value (element-sized)
    ret_val = None

    for ir in instructions:
        # ── initialise globals ──
        if isinstance(ir, IRGlobalDecl):
            inits = ir.init if ir.init else [0] * max(1, ir.total_bytes // max(ir.elem_bytes, 1))
            for i, v in enumerate(inits):
                byte_mem[ir.dmem_addr + i * ir.elem_bytes] = int(v)
            continue

        if isinstance(ir, (IRFuncBegin, IRFuncEnd, IRLabel, IRHalt)):
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
            addr = ir.dmem_addr + off
            v = byte_mem.get(addr, 0)
            temps[ir.dest.name] = v
            continue

        if isinstance(ir, IRGlobalStore):
            off = _operand(ir.offset, temps)
            src = _operand(ir.src, temps)
            if off is None or src is None:
                return None, {}
            byte_mem[ir.dmem_addr + off] = src
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

    # pack byte_mem back into 64-bit DMEM words
    final_dmem = {}
    for byte_addr, val in byte_mem.items():
        word_idx = byte_addr // 8
        byte_off = byte_addr % 8
        # determine element size: assume 8-byte for simplicity (safe upper bound)
        final_dmem[word_idx] = final_dmem.get(word_idx, 0) | (int(val) << (byte_off * 8))

    return ret_val, final_dmem


def write_result_file(path, ret_val, final_dmem, init_dmem):
    """
    Write result.txt.

    Format (matching APARA verification conventions):
      reg 0x1 0xVALUE           — expected return value in r1
      mem 0xWORD_IDX 0xVALUE   — expected final DMEM word values
                                  (only for globals that changed from init)
    """
    lines = []
    if ret_val is not None:
        lines.append(f"reg 0x1 0x{ret_val & _MASK64:x}")

    # emit mem lines for globals whose value changed during execution
    for word_idx in sorted(final_dmem):
        if word_idx == 0:
            continue   # skip the placeholder word
        val = final_dmem[word_idx]
        if val != init_dmem.get(word_idx, 0):
            lines.append(f"mem 0x{word_idx:x} 0x{val:016x}")

    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


# ── shell script generators ───────────────────────────────────────────────────

def write_run_script(script_path, mcode_name):
    content = f"""\
#!/bin/bash
# Auto-generated by APARA C Compiler — edit BIN_DIR to match your installation
BIN_DIR=../../../assembler/bin
ALIGN=$BIN_DIR/mcode_align
AS=$BIN_DIR/mcode_assemble
DISAS=$BIN_DIR/mcode_disassemble
RUN=$BIN_DIR/mcode_run

NAME={mcode_name}

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
    if do_preprocess:
        source, used_cpp = preprocess(c_file)
        if verbose and used_cpp:
            print("[preprocessed with system cpp]")
    else:
        with open(c_file) as f:
            source = f.read()

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

    if ret_val is not None or final_dmem:
        write_result_file(result_path, ret_val, final_dmem, init_dmem)
    else:
        # Program is too dynamic to evaluate statically; write a placeholder
        with open(result_path, 'w') as f:
            f.write("# Could not determine result statically — fill in after running\n")
            f.write("reg 0x1 0x0\n")

    # ── Summary ──
    n_globals = len(ir_globals)
    n_funcs   = sum(1 for i in ir_gen.instructions if type(i).__name__ == 'IRFuncBegin')
    n_bundles = mcode.count('\n||\n')
    print(f"[OK]  {c_file}  →  {output_file}")
    print(f"      globals={n_globals}  functions={n_funcs}  bundles={n_bundles}"
          f"  gbase=0x{global_base:x}  stack=0x{stack_top:x}")
    if n_globals:
        print(f"      global area: 0x{global_base:x} – 0x{ir_gen._next_global:x}")
    print(f"      data.map  →  {data_map_path}  ({len(init_dmem)} words)")
    if ret_val is not None:
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

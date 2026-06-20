#!/usr/bin/env python3
"""
golden_gen.py -- independent .result generator for the APARA ISA coverage suite.

This tool is deliberately separate from the APARA compiler (compiler/) and never
reads its source. It establishes ground truth for a test_X.c file by:

  1. Compiling test_X.c natively with gcc, linked against golden_stubs.h (plain-C
     reference implementations of the handful of APARA intrinsics gcc can't
     otherwise resolve -- see golden_stubs.h's own header for where each
     implementation comes from).
  2. Running the native binary and capturing the final value of every slot in
     its `results[]` array.
  3. Asking the APARA compiler (compiler.py -v) ONLY for the address it assigned
     to `results[]` -- not for any computed values, not for any logic, just
     "where did you put this array" -- and computing each slot's word index
     from that.
  4. Writing test_X.result with one "0 mem <word_index> <value>" PostCondition
     line per slot, in the format confirmed against the simulator in
     STATUS.md 2026-06-20.

Usage: python3 golden_gen.py <test_X.c> [<output_dir>]
"""
import re
import subprocess
import sys
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
STUBS = os.path.join(HERE, "golden_stubs.h")
COMPILER_PY = os.path.abspath(os.path.join(HERE, "..", "..", "compiler", "compiler.py"))


def get_n_results(src_text):
    m = re.search(r"#define\s+N_RESULTS\s+(\d+)", src_text)
    if not m:
        raise RuntimeError("test file has no '#define N_RESULTS <n>'")
    return int(m.group(1))


def native_ground_truth(test_c_path, n_results, workdir):
    src = open(test_c_path).read()
    driver_path = os.path.join(workdir, "_driver.c")
    with open(driver_path, "w") as f:
        f.write('#include "%s"\n' % STUBS)
        f.write("#define main __test_main\n")
        f.write(src)
        f.write("#undef main\n")
        f.write("#include <stdio.h>\n")
        f.write("int main(void) {\n")
        f.write("    __test_main();\n")
        f.write("    for (int i = 0; i < %d; i++) {\n" % n_results)
        f.write('        printf("%016llx\\n", (unsigned long long) results[i]);\n')
        f.write("    }\n")
        f.write("    return 0;\n")
        f.write("}\n")

    bin_path = os.path.join(workdir, "_driver")
    cc = subprocess.run(["gcc", "-O0", "-o", bin_path, driver_path],
                         capture_output=True, text=True)
    if cc.returncode != 0:
        raise RuntimeError("gcc failed:\n" + cc.stderr)

    run = subprocess.run([bin_path], capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError("native binary exited %d:\n%s" % (run.returncode, run.stderr))

    lines = [l.strip() for l in run.stdout.strip().splitlines() if l.strip()]
    if len(lines) != n_results:
        raise RuntimeError("expected %d result lines, got %d: %r" % (n_results, len(lines), lines))
    return lines  # list of 16-hex-digit strings, one per results[i]


def apara_results_base_addr(test_c_path, workdir):
    # --stack-top is pushed to the top of DMEM for this probe only, so the
    # global/stack-overlap safety check (compiler.py) never blocks large
    # test cases here -- it only affects where the STACK starts, never
    # where globals are placed, so it can't change results[]'s address.
    out = subprocess.run(
        ["python3", COMPILER_PY, test_c_path, "-v", "--stack-top", "0xfff8",
         "-o", os.path.join(workdir, "_probe.mcode")],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("APARA compiler failed:\n" + out.stdout + out.stderr)
    m = re.search(r"GLOBAL\s+results\s+@(0x[0-9a-fA-F]+)", out.stdout)
    if not m:
        raise RuntimeError("could not find 'GLOBAL results @0x...' in compiler -v output")
    return int(m.group(1), 16)


def main():
    if len(sys.argv) < 2:
        print("usage: golden_gen.py <test_X.c> [<output_dir>]")
        sys.exit(1)

    test_c_path = os.path.abspath(sys.argv[1])
    base_name = os.path.splitext(os.path.basename(test_c_path))[0]
    workdir = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.dirname(test_c_path)

    src = open(test_c_path).read()
    n_results = get_n_results(src)

    with tempfile.TemporaryDirectory(prefix="golden_gen_") as scratch:
        values = native_ground_truth(test_c_path, n_results, scratch)
        base_addr = apara_results_base_addr(test_c_path, scratch)
    base_word = base_addr // 8

    out_path = os.path.join(workdir, base_name, base_name + ".result")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        for i, val_hex in enumerate(values):
            word_idx = base_word + i
            f.write("0 mem 0x%x 0x%s\n" % (word_idx, val_hex))

    print("wrote %s (%d PostCondition lines, results[] base word 0x%x)" %
          (out_path, n_results, base_word))


if __name__ == "__main__":
    main()

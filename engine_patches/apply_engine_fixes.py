#!/usr/bin/env python3
"""
apply_engine_fixes.py -- install the six required simulator fixes into an
APARA engine_isp toolchain SOURCE tree, then you rebuild it with scons.

The compiler in this repo is verified (1,300+ programs, bit-exact vs gcc)
against a toolchain WITH these fixes. Running it against an unfixed build
gives silently wrong results ($fsqrt returns 0, float casts are garbage,
unsigned vreduce is signed...). Full rationale, symptoms and verification:
ENGINE_SOURCE_CHANGES.md and engine_source_changes_report/*.pdf.

Usage:
    python3 apply_engine_fixes.py --check  <engine_src_dir>   # report only
    python3 apply_engine_fixes.py --apply  <engine_src_dir>   # idempotent

<engine_src_dir> = .../engine_isp/assembler/src  (defaults to
$APARA_ENGINE_SRC). After --apply, rebuild:  cd ../ && scons
Each fix is an exact-string replacement: already-fixed files are skipped,
and a file matching NEITHER the pristine nor the fixed text is reported as
CONFLICT (upstream changed -- re-port by hand using the .md report).
"""
import os
import sys

FIXES = [
    # (name, file, pristine_old, fixed_new)
    ("fsqrt-execute-impl", "MachineRun.cpp",
     '''void McodeFsqrtInstruction::Execute     (McodeMachine* mc)
{
\tMcodeRoot::Error("McodeFsqrtInstruction yet to be implemented", this);
\tthis->McodeInstruction::Set_Out_Arg (0, 0);
\treturn;
}''',
     '''void McodeFsqrtInstruction::Execute     (McodeMachine* mc)
{
\t// Was a "yet to be implemented" stub that silently produced 0 (found by
\t// the fuzz1000 directed battery, 2026-07-18). The constructor already
\t// sets opcode __FSQRT and __alu_operation__ already dispatches it to
\t// fp_sqrt, so route through the standard ALU execute path (single
\t// source operand, no immediate).
\tuint64_t ovalue;
\t___execute_alu_operation___ (this->McodeInstruction::Get_Opcode(),
\t\t\tthis->Get_Dest_Type(),
\t\t\tthis->Get_Src_Type(),
\t\t\tthis->McodeInstruction::Get_In_Arg(0),
\t\t\t0, 0, 0,
\t\t\tovalue);
\tthis->McodeInstruction::Set_Out_Arg (0, ovalue);
\tif(__global_verbose_flag)
\t\tMcodeRoot::Info(Int64ToString (this->McodeInstruction::Get_Address()) + ": Fsqrt!");
\treturn;
}'''),

    ("execute-cast-float-guard", "McodeExecute.cpp",
     '''\tif(!dest_type.Get_Float_Flag()
\t\t\t&& !src_type.Get_Float_Flag())
\t{''',
     '''\t// Re-applied fix (originally 2026-07-17, lost in a source revert; see
\t// cmp_wd/compiler/STATUS.md): float casts used to fall into an
\t// unimplemented-error branch below that left ovalues UNINITIALIZED.
\t// ___cast_operation___ dispatches int<->float itself, so every cast can
\t// take the main path.
\tif(1)
\t{'''),

    ("execute-cast-error-branch", "McodeExecute.cpp",
     '''\telse
\t{
\t\tMcodeRoot::Error ("Float conversions in cast not supported as yet.", NULL);
\t}
}''',
     '''}'''),

    ("cast-dispatch-swap", "McodeOperations.cpp",
     '''\t\t\tuint64_t v = in_vals[I] & __mmask__(src_type.Get_Nbits());;
\t\t\tuint64_t w = 0;
\t\t\tif(!src_type.Get_Float_Flag())
\t\t\t\tw = cast_float_to_int (src_type, dest_type, v);
\t\t\telse if(!dest_type.Get_Float_Flag())
\t\t\t\tw = cast_int_to_float (src_type, dest_type, v);
\t\t\telse
\t\t\t\tw = cast_float_to_float (src_type, dest_type, v);''',
     '''\t\t\tuint64_t v = in_vals[I] & __mmask__(src_type.Get_Nbits());;
\t\t\tuint64_t w = 0;
\t\t\t// Re-applied fix (originally 2026-07-17, lost in a source revert;
\t\t\t// see cmp_wd/compiler/STATUS.md): the int<->float dispatch called
\t\t\t// the two conversion helpers SWAPPED — an int SOURCE must go
\t\t\t// int->float, a float source with int dest must go float->int.
\t\t\tif(!src_type.Get_Float_Flag())
\t\t\t\tw = cast_int_to_float (src_type, dest_type, v);
\t\t\telse if(!dest_type.Get_Float_Flag())
\t\t\t\tw = cast_float_to_int (src_type, dest_type, v);
\t\t\telse
\t\t\t\tw = cast_float_to_float (src_type, dest_type, v);'''),

    ("scalar-cast-vector-mask", "McodeOperations.cpp",
     '''\tint I;
\tint is_vector_cast = (in_vals.size() > 1);''',
     '''\tint I;
\t// Use the TYPE flags, not in_vals.size(): a scalar 32-bit source is
\t// Break_Vector'd into 2 entries, so size>1 misclassified scalar casts as
\t// vector and masked the result to dest_nbits — stripping the sign
\t// extension of e.g. (int)(-6.0f) stored into a 64-bit word (fp03,
\t// re-found 2026-07-19).
\tint is_vector_cast = (src_type.Get_Vector_Flag() && dest_type.Get_Vector_Flag());'''),

    ("vreduce-unsigned-lanes", "McodeOperations.cpp",
     '''\t\t\t// sign extend to 64-bits.
\t\t\tint64_t r = (int64_t) Sign_Extend_64(src_type.Get_Nbits()-1, ele);
\t\t\tif(signed_flag)
\t\t\t{''',
     '''\t\t\t// Unsigned lanes must be ZERO-extended: `ele` already arrives
\t\t\t// masked to the lane width from Break_Vector. The old shared
\t\t\t// `r` was sign-extended even on the unsigned path, so e.g.
\t\t\t// __vreduce_vu8 summed 0xf2 as -14 (fuzz1000 d06, 2026-07-19).
\t\t\tuint64_t r = ele;
\t\t\tif(signed_flag)
\t\t\t{'''),

    ("cast-int-to-float-ternary", "McodeFpuUtils.cpp",
     '''\tdouble x = (src_type.Get_Unsigned_Flag() ?  (u & 0xffffffff) : (int) u);''',
     '''\t// Re-applied fix (originally 2026-07-18, lost in a source revert; see
\t// cmp_wd/compiler/STATUS.md): the old ternary promoted the signed arm's
\t// (int) back to uint64_t (the unsigned arm's type), so (float)(-3)
\t// became (double)(2^64-3). Sign-extend explicitly from the source width.
\tdouble x;
\tif(src_type.Get_Unsigned_Flag())
\t{
\t\tx = (double) (u & __mmask__(snbits));
\t}
\telse
\t{
\t\tint64_t sv = ((int64_t) (u << (64 - snbits))) >> (64 - snbits);
\t\tx = (double) sv;
\t}'''),
]


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ('--check', '--apply'):
        print(__doc__)
        sys.exit(2)
    mode = sys.argv[1]
    src = sys.argv[2] if len(sys.argv) > 2 else os.environ.get('APARA_ENGINE_SRC')
    if not src or not os.path.isdir(src):
        print("ERROR: give the engine src dir (.../engine_isp/assembler/src) "
              "as an argument or set APARA_ENGINE_SRC")
        sys.exit(2)

    present, applied, conflicts = 0, 0, 0
    for name, fname, old, new in FIXES:
        path = os.path.join(src, fname)
        try:
            text = open(path).read()
        except OSError as e:
            print(f"CONFLICT  {name}: cannot read {path}: {e}")
            conflicts += 1
            continue
        if new in text:
            print(f"present   {name} ({fname})")
            present += 1
        elif old in text:
            if mode == '--apply':
                open(path, 'w').write(text.replace(old, new, 1))
                print(f"APPLIED   {name} ({fname})")
                applied += 1
            else:
                print(f"MISSING   {name} ({fname})  [pristine text found; --apply will fix]")
                conflicts += 1
        else:
            print(f"CONFLICT  {name} ({fname}): neither pristine nor fixed text "
                  f"matches -- upstream changed, re-port by hand "
                  f"(see ENGINE_SOURCE_CHANGES.md)")
            conflicts += 1

    print(f"\n{present} already present, {applied} applied, {conflicts} missing/conflicts")
    if applied:
        print("Now rebuild the toolchain:  cd <src>/.. && scons")
    sys.exit(1 if conflicts else 0)


if __name__ == '__main__':
    main()

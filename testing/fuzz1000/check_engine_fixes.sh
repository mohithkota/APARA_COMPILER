#!/bin/bash
# check_engine_fixes.sh -- verify the engine simulator SOURCE still contains
# every locally-applied fix. Run BEFORE and AFTER any toolchain rebuild:
# the 2026-07-19 incident showed a rebuild silently reverting fixes that
# existed only in binaries (see compiler/STATUS.md, PROCESS INCIDENT).
SRC=/home/mohithkota/complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/src
ok=0; bad=0
chk() {  # chk <file> <pattern> <name>
  if grep -q "$2" "$SRC/$1"; then ok=$((ok+1));
  else bad=$((bad+1)); echo "MISSING FIX: $3 ($1)"; fi
}
chk MachineRun.cpp        "route through the standard ALU"   "fsqrt-execute-impl"
chk McodeExecute.cpp      "every cast can"                        "execute-cast-float-path"
chk McodeOperations.cpp   "int SOURCE must go"                    "cast-dispatch-swap"
chk McodeOperations.cpp   "misclassified scalar casts as"         "scalar-cast-vector-mask"
chk McodeOperations.cpp   "Unsigned lanes must be ZERO-extended"  "vreduce-unsigned-lanes"
chk McodeFpuUtils.cpp     "Sign-extend explicitly from the source width" "cast-int-to-float-ternary"
if [ $bad -eq 0 ]; then echo "engine fixes: all $ok present"; else exit 1; fi

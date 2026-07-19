#!/bin/bash
# run_gate.sh -- the full regression gate, runnable from a fresh clone.
# Compiles every golden test with compiler.py (gcc-golden auto-generated),
# assembles and runs it on the simulator, and checks every PostCondition.
# Needs:  export APARA_TOOLS=.../engine_isp/assembler/bin
cd "$(dirname "$0")"
BIN=${APARA_TOOLS:-/home/mohithkota/complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin}
if [ ! -x "$BIN/mcode_run" ]; then
  echo "ERROR: APARA toolchain not found at $BIN (export APARA_TOOLS=...)"; exit 1
fi
CC="python3 $(cd ../compiler && pwd)/compiler.py"

declare -A GLOB=( [feature_sweep]='f[0-9]*.c' [universal]='*.c'
                  [pointer_bugs]='t[0-9]*.c'  [fp_check]='fp[0-9]*.c'
                  [fnptr]='fn[0-9]*.c' [vararg]='va[0-9]*.c'
                  [many_args]='ma[0-9]*.c' )
total_pass=0; total_fail=0
for suite in feature_sweep universal pointer_bugs fp_check fnptr vararg many_args; do
  cd "$suite" || continue
  pass=0; fail=0; failed=""
  for c in $(ls ${GLOB[$suite]} 2>/dev/null | sort -u); do
    name=${c%.c}
    $CC --preprocess $c > /dev/null 2>&1 || { fail=$((fail+1)); failed="$failed $name(compile)"; continue; }
    cd $name
    $BIN/mcode_align $name.mcode > $name.aligned.mcode 2> align.err
    $BIN/mcode_assemble $name.aligned.mcode > $name.obj 2> as.err
    if grep -qi "error" align.err as.err; then
      cd ..; fail=$((fail+1)); failed="$failed $name(tooling)"; continue
    fi
    out=$(timeout 60 $BIN/mcode_run -p 0x0 -i $name.obj -d data.map -r $name.result -v 2>&1)
    cd ..
    if echo "$out" | grep -q "expected"; then
      fail=$((fail+1)); failed="$failed $name"
    elif ! echo "$out" | grep -q "Halt!"; then
      fail=$((fail+1)); failed="$failed $name(nohalt)"
    else
      pass=$((pass+1))
    fi
  done
  echo "$suite: $pass PASS, $fail FAIL$failed"
  total_pass=$((total_pass+pass)); total_fail=$((total_fail+fail))
  cd ..
done
echo "GATE TOTAL: $total_pass PASS, $total_fail FAIL"
[ $total_fail -eq 0 ]

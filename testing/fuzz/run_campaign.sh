#!/bin/bash
# run_campaign.sh FIRST_SEED LAST_SEED -- differential fuzz campaign.
# For each seed: generate, compile (gcc-golden auto-verified), assemble, run
# on the simulator, classify. Failing seeds keep their whole directory in
# fail_<seed>/ for triage; passing ones are cleaned up.
cd "$(dirname "$0")"
BIN=${APARA_TOOLS:-/home/mohithkota/complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin}
CC="python3 ../../compiler/compiler.py"

FIRST=${1:-1}; LAST=${2:-100}
pass=0; fail=0; hang=0; cfail=0; skip=0
: > campaign.log

for seed in $(seq $FIRST $LAST); do
  name=fz$seed
  python3 gen_fuzz.py $seed > $name.c
  out=$($CC --preprocess $name.c 2>&1)
  if [ $? -ne 0 ]; then
    cfail=$((cfail+1)); echo "$seed COMPILE-FAIL" >> campaign.log
    mkdir -p fail_$seed && mv $name.c fail_$seed/ && echo "$out" > fail_$seed/compile.log
    continue
  fi
  if ! echo "$out" | grep -q "independently-verified"; then
    # golden verify fell back (e.g. gcc rejected the program) -> generator issue
    skip=$((skip+1)); echo "$seed SKIP-NO-GOLDEN" >> campaign.log
    mkdir -p fail_$seed && mv $name.c fail_$seed/ && echo "$out" > fail_$seed/compile.log
    rm -rf $name; continue
  fi
  bundles=$(echo "$out" | grep -o "bundles: [0-9]* → [0-9]*" | grep -o "[0-9]*$")
  if [ -n "$bundles" ] && [ "$bundles" -gt 240 ]; then
    skip=$((skip+1)); echo "$seed SKIP-IMEM($bundles)" >> campaign.log
    rm -rf $name $name.c; continue
  fi
  cd $name
  $BIN/mcode_align $name.mcode > $name.aligned.mcode 2> align.err
  $BIN/mcode_assemble $name.aligned.mcode > $name.obj 2> as.err
  if grep -qi "error" align.err as.err; then
    cd ..
    fail=$((fail+1)); echo "$seed TOOLING-FAIL" >> campaign.log
    mkdir -p fail_$seed && mv $name.c $name fail_$seed/
    continue
  fi
  run=$(timeout 30 $BIN/mcode_run -p 0x0 -i $name.obj -d data.map -r $name.result -v 2>&1)
  rc=$?
  cd ..
  if [ $rc -eq 124 ]; then
    hang=$((hang+1)); echo "$seed HANG" >> campaign.log
    mkdir -p fail_$seed && mv $name.c $name fail_$seed/
  elif echo "$run" | grep -q "expected"; then
    fail=$((fail+1))
    echo "$seed FAIL: $(echo "$run" | grep expected | head -2 | tr '\n' ' ')" >> campaign.log
    mkdir -p fail_$seed && mv $name.c $name fail_$seed/
    echo "$run" | grep -E "expected|PostCondition" > fail_$seed/mismatch.log
  elif ! echo "$run" | grep -q "Halt!"; then
    fail=$((fail+1)); echo "$seed NO-HALT(rc=$rc)" >> campaign.log
    mkdir -p fail_$seed && mv $name.c $name fail_$seed/
  else
    pass=$((pass+1)); rm -rf $name $name.c
  fi
done

echo "seeds $FIRST-$LAST: PASS=$pass FAIL=$fail HANG=$hang COMPILE-FAIL=$cfail SKIP=$skip"
echo "seeds $FIRST-$LAST: PASS=$pass FAIL=$fail HANG=$hang COMPILE-FAIL=$cfail SKIP=$skip" >> campaign.log

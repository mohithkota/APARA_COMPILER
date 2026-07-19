#!/bin/bash
# run_all.sh [FIRST] [LAST] -- the 1000-program verification campaign.
# Phase 1: directed battery (directed/*.c) -- guarantees every instruction /
#          sub-instruction / datatype appears at least once.
# Phase 2: randomized programs from gen_full.py, seeds FIRST..LAST.
# Every program is compiled (gcc-golden auto-verified), aligned/assembled with
# toolchain stderr CHECKED, simulated, and classified. Coverage keys of every
# PASSING program's mcode accumulate into coverage.acc; the final report
# audits them against cov_scan.py's checklist.
cd "$(dirname "$0")"
BIN=${APARA_TOOLS:-/home/mohithkota/complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin}
CC="python3 ../../compiler/compiler.py"

FIRST=${1:-1}; LAST=${2:-1000}
pass=0; fail=0; hang=0; cfail=0; skip=0
: > campaign.log
: > coverage.acc

build_and_run() {   # $1 = base name (dir with $1.c already present)
  local name=$1
  local out
  out=$($CC --preprocess $name.c 2>&1)
  if [ $? -ne 0 ]; then
    echo "COMPILE-FAIL"; mkdir -p fail_$name && cp $name.c fail_$name/ && echo "$out" > fail_$name/compile.log
    return
  fi
  if ! echo "$out" | grep -q "independently-verified"; then
    echo "SKIP-NO-GOLDEN"; mkdir -p fail_$name && cp $name.c fail_$name/ && echo "$out" > fail_$name/compile.log
    return
  fi
  local bundles
  bundles=$(echo "$out" | grep -o "bundles: [0-9]* → [0-9]*" | grep -o "[0-9]*$")
  if [ -n "$bundles" ] && [ "$bundles" -gt 240 ]; then
    echo "SKIP-IMEM($bundles)"; return
  fi
  cd $name
  $BIN/mcode_align $name.mcode > $name.aligned.mcode 2> align.err
  $BIN/mcode_assemble $name.aligned.mcode > $name.obj 2> as.err
  if grep -qi "error" align.err as.err; then
    cd ..; echo "TOOLING-FAIL"; mkdir -p fail_$name && cp $name.c fail_$name/ && cp -r $name fail_$name/
    return
  fi
  local run rc
  run=$(timeout 30 $BIN/mcode_run -p 0x0 -i $name.obj -d data.map -r $name.result -v 2>&1)
  rc=$?
  cd ..
  if [ $rc -eq 124 ]; then
    echo "HANG"; mkdir -p fail_$name && cp $name.c fail_$name/ && cp -r $name fail_$name/
  elif echo "$run" | grep -q "expected"; then
    echo "FAIL"; mkdir -p fail_$name && cp $name.c fail_$name/ && cp -r $name fail_$name/
    echo "$run" | grep -E "expected" | head -4 > fail_$name/mismatch.log
  elif ! echo "$run" | grep -q "Halt!"; then
    echo "NO-HALT(rc=$rc)"; mkdir -p fail_$name && cp $name.c fail_$name/ && cp -r $name fail_$name/
  else
    python3 cov_scan.py $name/$name.mcode >> coverage.acc
    echo "PASS"
  fi
}

echo "=== phase 1: directed battery ==="
for src in directed/d*.c; do
  base=$(basename $src .c)
  cp $src $base.c
  res=$(build_and_run $base)
  echo "directed $base: $res" | tee -a campaign.log
  case $res in
    PASS) pass=$((pass+1)); rm -rf $base $base.c;;
    SKIP*) skip=$((skip+1)); rm -rf $base $base.c;;
    COMPILE-FAIL) cfail=$((cfail+1)); rm -f $base.c;;
    HANG) hang=$((hang+1)); rm -rf $base $base.c;;
    *) fail=$((fail+1)); rm -rf $base $base.c;;
  esac
done

echo "=== phase 2: fuzz seeds $FIRST-$LAST ==="
for seed in $(seq $FIRST $LAST); do
  name=fz$seed
  python3 gen_full.py $seed > $name.c
  res=$(build_and_run $name)
  echo "$seed $res" >> campaign.log
  case $res in
    PASS) pass=$((pass+1)); rm -rf $name $name.c;;
    SKIP*) skip=$((skip+1)); rm -rf $name $name.c;;
    COMPILE-FAIL) cfail=$((cfail+1)); rm -f $name.c;;
    HANG) hang=$((hang+1)); rm -rf $name $name.c;;
    *) fail=$((fail+1)); rm -rf $name $name.c;;
  esac
done

echo "==== TOTAL: PASS=$pass FAIL=$fail HANG=$hang COMPILE-FAIL=$cfail SKIP=$skip ====" | tee -a campaign.log
echo "=== coverage audit ==="
python3 cov_scan.py --report coverage.acc | tee -a campaign.log

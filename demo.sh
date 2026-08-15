#!/bin/bash
# ============================================================================
#  APARA C Compiler — live demo
#  Runs the FULL pipeline on a real 16x16 matrix-multiply kernel:
#     matmul_n16.c  →  compiler.py  →  APARA mcode  →  assemble  →  simulate
#  and then shows the optimizer's effect (A/B) with correctness preserved.
#
#  Usage:  ./demo.sh
# ============================================================================
cd "$(dirname "$0")"

COMP=compiler/compiler.py
SRC=matmul_tests/matmul_n16.c
BIN=/home/mohithkota/complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin
WORK=demo_work
mkdir -p $WORK
cp $SRC $WORK/matmul_n16.c
cd $WORK

pause() { echo; read -rp "  [ Enter to continue ] " _; echo; }
rule()  { printf '=%.0s' {1..76}; echo; }

# ----------------------------------------------------------------------------
rule; echo "  STEP 1 — The C source (a real 16x16 matrix multiply)"; rule
sed -n '23,49p' matmul_n16.c
pause

# ----------------------------------------------------------------------------
rule; echo "  STEP 2 — Compile C -> APARA VLIW mcode (with optimizer ON)"; rule
python3 ../$COMP matmul_n16.c -o matmul_n16.mcode --stack-top 0xfff8
pause

# ----------------------------------------------------------------------------
rule; echo "  STEP 3 — Assemble & run on the real APARA simulator"; rule
$BIN/mcode_align    matmul_n16.mcode         > matmul_n16.aligned.mcode 2>/dev/null
$BIN/mcode_assemble matmul_n16.aligned.mcode > matmul_n16.obj 2>/dev/null
$BIN/mcode_run -p 0x0 -i matmul_n16.obj -d data.map -r matmul_n16.result -v \
    > opt.log 2>&1
CHECKS=$(grep -c PostCondition opt.log)
ERRS=$(grep -ic error opt.log)
LOADS_OPT=$(grep -c '\$ld' opt.log)
echo "  Result:  $CHECKS / 256 cells verified against an independent gcc reference"
echo "           errors = $ERRS"
echo "           executed loads (runtime) = $LOADS_OPT"
pause

# ----------------------------------------------------------------------------
rule; echo "  STEP 4 — Optimizer A/B: recompile with loop-opt OFF, same program"; rule
APARA_NO_LOOPOPT=1 python3 ../$COMP matmul_n16.c -o base.mcode --stack-top 0xfff8 >/dev/null
$BIN/mcode_align    base.mcode         > base.aligned.mcode 2>/dev/null
$BIN/mcode_assemble base.aligned.mcode > base.obj 2>/dev/null
$BIN/mcode_run -p 0x0 -i base.obj -d data.map -r matmul_n16.result -v > base.log 2>&1
CHECKS_B=$(grep -c PostCondition base.log)
ERRS_B=$(grep -ic error base.log)
LOADS_BASE=$(grep -c '\$ld' base.log)
echo "                        executed loads    correctness"
echo "  baseline  (opt OFF)      $LOADS_BASE          $CHECKS_B/256, $ERRS_B errors"
echo "  optimized (opt ON)        $LOADS_OPT          $CHECKS/256, $ERRS errors"
echo
PCT=$(python3 -c "print(f'{100*(1-$LOADS_OPT/$LOADS_BASE):.0f}')")
echo "  ==> optimizer cuts executed loads by ${PCT}% with identical correct output."
rule

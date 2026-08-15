#!/bin/bash
# ============================================================================
#  APARA C Compiler -- demo
#    d1  scalar ALU functions
#    d2  arrays: element-wise add
#    d3  arrays: element-wise multiply, dot product, 4x4 matrix multiply
#
#  Each demo runs the FULL pipeline:
#     C  ->  compiler.py  ->  APARA mcode  ->  assemble  ->  simulate
#  and every result slot is checked against a native gcc build of the same
#  source, so PASS means "produced the correct values", not "did not crash".
#
#  Usage:  ./run_demo.sh            (all three, with pauses)
#          ./run_demo.sh d2         (just one)
# ============================================================================
cd "$(dirname "$0")/.."

rule()  { printf '=%.0s' {1..76}; echo; }
pause() { echo; read -rp "  [ Enter to continue ] " _; echo; }

demo() {
    local name=$1 title=$2
    rule; echo "  $name  --  $title"; rule
    echo
    echo "  --- source: demo_prof/$name.c ---"
    cat "demo_prof/$name.c"
    pause

    echo "  --- compile + assemble + run on the APARA simulator ---"
    ./apara-cc "demo_prof/$name.c" --run
    pause
}

case "${1:-all}" in
  d1)  demo d1_alu       "the ALU, straight-line in main()" ;;
  d2)  demo d2_array_add "arrays: element-wise add" ;;
  d3)  demo d3_array_mul "arrays: multiply, dot product, 4x4 matmul" ;;
  all)
    demo d1_alu       "the ALU, straight-line in main()"
    demo d2_array_add "arrays: element-wise add"
    demo d3_array_mul "arrays: multiply, dot product, 4x4 matmul"
    rule
    echo "  All three verified against gcc on the real simulator."
    echo "  Full regression gate:  bash testing/run_gate.sh   (71 golden tests)"
    rule
    ;;
  *) echo "usage: $0 [d1|d2|d3|all]"; exit 2 ;;
esac

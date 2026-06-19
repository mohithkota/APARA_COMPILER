#!/bin/bash
cd "$(dirname "$0")"
BIN_DIR=/home/mohithkota/complier_Apara/engine_new/AjitHpcAccelRepo/AjitHpcAccel/engine_isp/assembler/bin
ALIGN=$BIN_DIR/mcode_align
AS=$BIN_DIR/mcode_assemble
DISAS=$BIN_DIR/mcode_disassemble
RUN=$BIN_DIR/mcode_run

NAME=test_vec_dot

[ -f $NAME.golden ] && cp $NAME.golden $NAME.result
$ALIGN  $NAME.mcode           > $NAME.aligned.mcode
$AS     $NAME.aligned.mcode   > $NAME.obj
$DISAS  $NAME.obj             > $NAME.disass.mcode
$RUN -p 0x0 -i $NAME.obj -d data.map -r $NAME.result -v 2>&1 | tee $NAME.log

ALIGN=../../../assembler/bin/mcode_align
AS=../../../assembler/bin/mcode_assemble
DISAS=../../../assembler/bin/mcode_disassemble
RUN=../../../assembler/bin/mcode_run
$ALIGN $1.mcode | tee $1.aligned.mcode
$AS    $1.aligned.mcode | tee $1.obj
$DISAS $1.obj | tee $1.disass.mcode
$RUN -p 0x0 -i $1.obj -d data.map -r $1.result -v 

# R6.2D lane/byte-order probes

Hand-written mcode, deliberately not compiler-generated: they establish an ISA
contract, so involving the compiler would make the answer circular. gcc cannot
serve as a golden reference either — the DMEM word layout is APARA-specific with
no native counterpart — so each probe is checked directly against DMEM
PostConditions.

    probe.mcode   load side : is byte address 0 the MOST significant byte?
    probe2.mcode  store side: does a byte written at address 3 land at bits [39:32]?

Both confirm MSB-first. Run:

    $APARA_TOOLS/mcode_align probe.mcode > probe.aligned.mcode
    $APARA_TOOLS/mcode_assemble probe.aligned.mcode > probe.obj
    $APARA_TOOLS/mcode_run -p 0x0 -i probe.obj -d data.map -r probe.result

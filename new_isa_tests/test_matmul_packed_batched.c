// Final, time-boxed experiment: hand-written restructuring matching the
// reference's actual load1/load2 -> dot1-dot4 batching pattern -- load A's
// row and all 4 B columns FIRST (hitting the ISA's 4-loads-per-bundle
// ceiling), THEN issue all 4 dot products against the still register-
// resident halves, THEN store each result straight to its own final C[]
// slot (no intermediate buffer, no store-then-reload hazard on results).
// Uses the one-off __dot128_batch4_vu8 intrinsic (see ir_gen.py) built
// specifically for this measurement -- not a general pass.
vu8_t A[256];
vu8_t BT[256];
long long C[256];

int main() {
    int i;
    int j;

    for (i = 0; i < 16; i++) {
        for (j = 0; j < 16; j++) {
            A[i*16+j]  = (i*16+j+1) % 256;
            BT[i*16+j] = (j*16+i+1) % 256;
        }
    }

    for (i = 0; i < 16; i++) {
        for (j = 0; j < 16; j += 4) {
            __dot128_batch4_vu8(&A[i*16],
                                 &BT[j*16], &BT[(j+1)*16], &BT[(j+2)*16], &BT[(j+3)*16],
                                 &C[i*16+j], &C[i*16+j+1], &C[i*16+j+2], &C[i*16+j+3]);
        }
    }

    if (C[0] != 0x5588)    return -1;   // row0,col0
    if (C[15] != 0x4d80)   return -2;   // row0,col15
    if (C[255] != 0x75580) return -3;   // row15,col15
    return 1;
}

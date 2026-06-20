// Retry of the unroll-by-4 experiment, this time on the corrected fused
// __dot128_direct_vu8(a_ptr, b_ptr) intrinsic instead of the old
// __ld128/__dot128_vu8 pair. No intermediate buffers/variables needed at
// all now -- each call is self-contained (loads both operands, dots them,
// returns the result directly). Same 16x16 case, same correctness checks.
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
            C[i*16+j]   = __dot128_direct_vu8(&A[i*16], &BT[j*16]);
            C[i*16+j+1] = __dot128_direct_vu8(&A[i*16], &BT[(j+1)*16]);
            C[i*16+j+2] = __dot128_direct_vu8(&A[i*16], &BT[(j+2)*16]);
            C[i*16+j+3] = __dot128_direct_vu8(&A[i*16], &BT[(j+3)*16]);
        }
    }

    if (C[0] != 0x5588)    return -1;   // row0,col0
    if (C[15] != 0x4d80)   return -2;   // row0,col15
    if (C[255] != 0x75580) return -3;   // row15,col15
    return 1;
}

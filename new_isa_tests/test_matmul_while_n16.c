// 38-bundle fused intrinsic (no unrolling, no batching), for-loops converted
// to while-loops to capture the 1-bundle-per-nest savings found earlier
// (for emits an extra increment-point label that while doesn't need).
// N=16: same case as test_matmul_packed_direct.c, same data layout/values.
vu8_t A[256];
vu8_t BT[256];
long long C[256];

int main() {
    int i;
    int j;

    i = 0;
    while (i < 16) {
        j = 0;
        while (j < 16) {
            A[i*16+j]  = (i*16+j+1) % 256;
            BT[i*16+j] = (j*16+i+1) % 256;
            j++;
        }
        i++;
    }

    i = 0;
    while (i < 16) {
        j = 0;
        while (j < 16) {
            C[i*16+j] = __dot128_direct_vu8(&A[i*16], &BT[j*16]);
            j++;
        }
        i++;
    }

    if (C[0] != 0x5588)    return -1;   // row0,col0
    if (C[15] != 0x4d80)   return -2;   // row0,col15
    if (C[255] != 0x75580) return -3;   // row15,col15
    return 1;
}

// Hand-unrolled version of test_matmul_packed.c: inner j-loop unrolled by 4
// (matching the 16x16 reference's batching granularity -- it loads/dots 4
// columns at a time per bundle group). Validates whether bundler.py's
// existing greedy packing closes the density gap once given 4-wide
// independent work, before deciding whether a general unroller pass is
// worth building. Separate buf0..buf3 (not one shared buf) so the 4 loads
// don't create a false memory dependency through a shared address.
vu8_t A[256];
vu8_t BT[256];
long long C[256];
long long buf0[2];
long long buf1[2];
long long buf2[2];
long long buf3[2];

int main() {
    int i;
    int j;
    long long a_lo;
    long long a_hi;
    long long b_lo0, b_hi0, b_lo1, b_hi1, b_lo2, b_hi2, b_lo3, b_hi3;

    for (i = 0; i < 16; i++) {
        for (j = 0; j < 16; j++) {
            A[i*16+j]  = (i*16+j+1) % 256;
            BT[i*16+j] = (j*16+i+1) % 256;
        }
    }

    for (i = 0; i < 16; i++) {
        __ld128(buf0, &A[i*16]);
        a_lo = buf0[0]; a_hi = buf0[1];

        for (j = 0; j < 16; j += 4) {
            __ld128(buf0, &BT[j*16]);
            __ld128(buf1, &BT[(j+1)*16]);
            __ld128(buf2, &BT[(j+2)*16]);
            __ld128(buf3, &BT[(j+3)*16]);

            b_lo0 = buf0[0]; b_hi0 = buf0[1];
            b_lo1 = buf1[0]; b_hi1 = buf1[1];
            b_lo2 = buf2[0]; b_hi2 = buf2[1];
            b_lo3 = buf3[0]; b_hi3 = buf3[1];

            C[i*16+j]   = __dot128_vu8(a_lo, a_hi, b_lo0, b_hi0);
            C[i*16+j+1] = __dot128_vu8(a_lo, a_hi, b_lo1, b_hi1);
            C[i*16+j+2] = __dot128_vu8(a_lo, a_hi, b_lo2, b_hi2);
            C[i*16+j+3] = __dot128_vu8(a_lo, a_hi, b_lo3, b_hi3);
        }
    }

    if (C[0] != 0x5588)    return -1;   // row0,col0
    if (C[15] != 0x4d80)   return -2;   // row0,col15
    if (C[255] != 0x75580) return -3;   // row15,col15
    return 1;
}

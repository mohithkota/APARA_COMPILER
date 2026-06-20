// Step 3: the same 16x16 vu8 matmul as test_matmul_u128.c, but using the new
// opt-in packed array type (vu8_t) instead of plain unsigned char -- this is
// the fix for the Stage 3 finding (plain char arrays are 8-byte-padded per
// element, so a $ld ($u128) never saw 16 packed bytes). A/BT are now
// genuinely 16 contiguous bytes each. B is pre-transposed (BT) exactly like
// the 16x16 reference, so each "column of B" load is contiguous.
//
// Data layout matches 16x16_loop/generate.py exactly: a_val=(r*16+k+1)%256,
// b_val=(k*16+c+1)%256.
//
// Every cell is written into results[] (256 entries) -- see
// isa_coverage_tests/test_alu_full.c / matmul_tests/matmul_n16.c /
// compiler/STATUS.md 2026-06-20 for why.
vu8_t A[256];
vu8_t BT[256];
#define N_RESULTS 256
long long results[N_RESULTS];
long long buf[2];

int main() {
    int i;
    int j;
    long long a_lo;
    long long a_hi;
    long long b_lo;
    long long b_hi;

    for (i = 0; i < 16; i++) {
        for (j = 0; j < 16; j++) {
            A[i*16+j]  = (i*16+j+1) % 256;        // a_val(r=i,k=j)
            BT[i*16+j] = (j*16+i+1) % 256;         // BT[c=i][k=j] = b_val(k=j,c=i)
        }
    }

    for (i = 0; i < 16; i++) {
        __ld128(buf, &A[i*16]);
        a_lo = buf[0]; a_hi = buf[1];
        for (j = 0; j < 16; j++) {
            __ld128(buf, &BT[j*16]);
            b_lo = buf[0]; b_hi = buf[1];
            results[i*16+j] = __dot128_vu8(a_lo, a_hi, b_lo, b_hi);
        }
    }

    return 1;
}

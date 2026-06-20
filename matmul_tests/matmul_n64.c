// Matrix multiplication, N=64. Each row is 64 elements = exactly 4 u128
// chunks (no padding needed, 64 is already a multiple of 16). Full dot
// product = sum of 4 __dot128_direct_vu8 calls.
//
// Needs --stack-top above the default (0x7ff8) -- this size's combined
// global footprint (A+BT+results) reaches past it, which silently
// corrupts the stack against C[2840..2943] otherwise. See
// compiler/STATUS.md 2026-06-20 (the global/stack-overlap fix) -- this
// is exactly the case that finding came from.
//
// Every cell of C is written into results[] (4096 entries) -- see
// matmul_n16.c / isa_coverage_tests/test_alu_full.c for why.
vu8_t A[4096];
vu8_t BT[4096];

#define N_RESULTS 4096
long long results[N_RESULTS];

long long __dot128_direct_vu8(unsigned char *a, unsigned char *b);

int main() {
    int i;
    int j;

    i = 0;
    while (i < 64) {
        j = 0;
        while (j < 64) {
            A[i*64+j]  = (i*64+j+1) % 256;
            BT[i*64+j] = (j*64+i+1) % 256;
            j++;
        }
        i++;
    }

    i = 0;
    while (i < 64) {
        j = 0;
        while (j < 64) {
            results[i*64+j] = __dot128_direct_vu8(&A[i*64],    &BT[j*64])
                             + __dot128_direct_vu8(&A[i*64+16], &BT[j*64+16])
                             + __dot128_direct_vu8(&A[i*64+32], &BT[j*64+32])
                             + __dot128_direct_vu8(&A[i*64+48], &BT[j*64+48]);
            j++;
        }
        i++;
    }

    return 1;
}

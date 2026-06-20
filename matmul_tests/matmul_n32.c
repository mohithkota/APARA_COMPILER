// Matrix multiplication, N=32. Each row is 32 elements = exactly 2 u128
// chunks (no padding needed, 32 is already a multiple of 16). Full dot
// product = sum of 2 __dot128_direct_vu8 calls (elements 0-15, 16-31).
//
// Every cell of C is written into results[] (1024 entries) -- see
// matmul_n16.c / isa_coverage_tests/test_alu_full.c for why.
vu8_t A[1024];
vu8_t BT[1024];

#define N_RESULTS 1024
long long results[N_RESULTS];

long long __dot128_direct_vu8(unsigned char *a, unsigned char *b);

int main() {
    int i;
    int j;

    i = 0;
    while (i < 32) {
        j = 0;
        while (j < 32) {
            A[i*32+j]  = (i*32+j+1) % 256;
            BT[i*32+j] = (j*32+i+1) % 256;
            j++;
        }
        i++;
    }

    i = 0;
    while (i < 32) {
        j = 0;
        while (j < 32) {
            results[i*32+j] = __dot128_direct_vu8(&A[i*32], &BT[j*32])
                             + __dot128_direct_vu8(&A[i*32+16], &BT[j*32+16]);
            j++;
        }
        i++;
    }

    return 1;
}

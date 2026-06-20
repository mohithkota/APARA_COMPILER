// Matrix multiplication, N=8. Each row is only 8 elements, but
// __dot128_direct_vu8 always reads a full 16-byte u128, so each row is
// stored in its own 16-byte-stride slot (bytes 0-7 real data, bytes 8-15
// explicitly zero-padded) -- the zero-padded terms contribute nothing to
// the dot product, giving the exact correct 8-element result.
//
// Every cell of C is written into results[] (64 entries) -- see
// matmul_n16.c / isa_coverage_tests/test_alu_full.c for why.
vu8_t A[128];
vu8_t BT[128];

#define N_RESULTS 64
long long results[N_RESULTS];

long long __dot128_direct_vu8(unsigned char *a, unsigned char *b);

int main() {
    int i;
    int j;

    i = 0;
    while (i < 8) {
        j = 0;
        while (j < 8) {
            A[i*16+j]  = (i*8+j+1) % 256;
            BT[i*16+j] = (j*8+i+1) % 256;
            j++;
        }
        j = 8;
        while (j < 16) {
            A[i*16+j]  = 0;
            BT[i*16+j] = 0;
            j++;
        }
        i++;
    }

    i = 0;
    while (i < 8) {
        j = 0;
        while (j < 8) {
            results[i*8+j] = __dot128_direct_vu8(&A[i*16], &BT[j*16]);
            j++;
        }
        i++;
    }

    return 1;
}

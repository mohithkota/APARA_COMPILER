// Matrix multiplication, N=16, using the fused __dot128_direct_vu8
// intrinsic (no round-trip, no unrolling/batching -- both confirmed
// worse, see compiler/STATUS.md). A/BT use the opt-in vu8_t packed-array
// typedef so $ld ($u128) sees 16 tightly-packed bytes as 16 real vector
// elements (the default 8-byte-per-element DMEM stride would otherwise
// break this -- see STATUS.md's byte-array-padding finding).
//
// Every cell of C is written into results[] (256 entries, one per cell)
// -- see isa_coverage_tests/test_alu_full.c / golden/golden_gen.py for
// why this gives genuine per-cell verification instead of a handful of
// spot-checks. golden_gen.py computes ground truth by compiling this
// EXACT source natively with gcc against golden_stubs.h's
// __dot128_direct_vu8 (a plain 16-element unsigned-byte dot product,
// derived from the ISA spec, not from this project's own compiler).
vu8_t A[256];
vu8_t BT[256];

#define N_RESULTS 256
long long results[N_RESULTS];

long long __dot128_direct_vu8(unsigned char *a, unsigned char *b);

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
            results[i*16+j] = __dot128_direct_vu8(&A[i*16], &BT[j*16]);
            j++;
        }
        i++;
    }

    return 1;
}

// Stage 1: u128 load mechanics only. Load one 128-bit value from src[0..1]
// into a register pair via __ld128(results, src), store it back out as two
// u64 halves into results[0..1]. No dot product, no matmul -- just proves
// the load+pair-allocation mechanism. Expect results[0]=src[0]=0x1111...,
// results[1]=src[1]=0x2222... (hypothesis: lower register = lower address,
// matching every other multi-register convention in this compiler --
// verified below, not assumed).
//
// results[] doubles as both the destination buffer and the checked array --
// see isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20.
long long src[2];
#define N_RESULTS 2
long long results[N_RESULTS];

int main() {
    src[0] = 0x1111111111111111LL;
    src[1] = 0x2222222222222222LL;

    __ld128(results, src);

    return 1;
}

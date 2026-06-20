// Mirror of test_u256_load.c, for the new $st ($u256) wide-store mechanism.
// __st256(results, src): four plain 64-bit loads of src[0..3], one
// $st ($u256) writing all four quarters to results[] in a single instruction.
//
// results[] doubles as both the destination buffer and the checked array --
// see isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20.
long long src[4];
#define N_RESULTS 4
long long results[N_RESULTS];

int main() {
    src[0] = 0x1111111111111111LL;
    src[1] = 0x2222222222222222LL;
    src[2] = 0x3333333333333333LL;
    src[3] = 0x4444444444444444LL;

    __st256(results, src);

    return 1;
}

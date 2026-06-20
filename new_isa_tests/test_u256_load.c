// u256 load mechanics, mirroring test_u128_load.c exactly. Load one 256-bit
// value from src[0..3] into a register quad via __ld256(results, src),
// store it back out as four u64 quarters into results[0..3].
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

    __ld256(results, src);

    return 1;
}

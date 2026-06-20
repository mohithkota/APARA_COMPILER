// Mirror of test_u128_load.c: proves the new $st ($u128)/($u256) wide-store
// mechanism (IRStoreWide), the confirmed gap found while auditing ISA
// coverage -- we'd built wide load extensively but never wide store.
// __st128(results, src): two plain 64-bit loads of src[0]/src[1], one
// $st ($u128) writing both halves to results[] in a single instruction.
//
// results[] doubles as both the destination buffer and the checked array --
// see isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20.
long long src[2];
#define N_RESULTS 2
long long results[N_RESULTS];

int main() {
    src[0] = 0x1111111111111111LL;
    src[1] = 0x2222222222222222LL;

    __st128(results, src);

    return 1;
}

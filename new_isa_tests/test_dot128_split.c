// Minimal test for the u128-wide dot-split: __dot128_vu8(a_lo,a_hi,b_lo,b_hi)
// must emit exactly $dot then $dot $accumulate (confirmed pattern from the
// 16x16 reference), giving the full 16-element dot product.
// a = elements 1..16 (a_lo=1..8, a_hi=9..16), b = all 1s.
// Hand-computed: sum(1..16)*1 = 136 = 0x88.
//
// The check writes its computed value into results[] -- see
// isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20 for why.
#define N_RESULTS 1
long long results[N_RESULTS];

long long __dot128_vu8(long long a_lo, long long a_hi, long long b_lo, long long b_hi);

int main() {
    long long a_lo = 0x0102030405060708LL;   // 1,2,3,4,5,6,7,8
    long long a_hi = 0x090a0b0c0d0e0f10LL;   // 9,10,...,16
    long long b_lo = 0x0101010101010101LL;
    long long b_hi = 0x0101010101010101LL;

    results[0] = __dot128_vu8(a_lo, a_hi, b_lo, b_hi);

    return 1;
}

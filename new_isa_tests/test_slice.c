/* test_slice.c — tests $slice (bit-field extract)
   $slice rd hindex lindex rs
   rd = rs[hindex:lindex], zero-extended into rd[hindex-lindex:0]

   Each check writes its computed value into results[] -- see
   isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20 for why. */
#define N_RESULTS 2
long long results[N_RESULTS];

long long __slice(long long x, int hindex, int lindex);

int main() {
    long long val = 0xABCD;   /* binary: 1010 1011 1100 1101 */

    /* Extract bits [7:4] = 0xC = 12 */
    results[0] = __slice(val, 7, 4);

    /* Extract bits [15:8] = 0xAB = 171 */
    results[1] = __slice(val, 15, 8);

    return 1;
}

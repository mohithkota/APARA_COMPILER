/* test_vreduce.c — tests $vreduce (sum all elements of a vector)
   $vreduce rd (type) rs
   rd = sum of all elements in rs interpreted as (type) vector

   Each check writes its computed value into results[] -- see
   isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20 for why.
   All-positive/signed elements here, so this doesn't exercise the
   confirmed unsigned-vreduce simulator bug (see
   isa_coverage_tests/test_vreduce_full.c, which does). */
#define N_RESULTS 3
long long results[N_RESULTS];

long long __vreduce_vi32(long long a);
long long __vreduce_vi16(long long a);
long long __vreduce_vi8 (long long a);

int main() {
    /* vi32: [10, 20] → sum = 30 */
    long long v32 = 10LL | (20LL << 32);
    results[0] = __vreduce_vi32(v32);

    /* vi16: [1, 2, 3, 4] → sum = 10 */
    long long v16 = 1LL | (2LL<<16) | (3LL<<32) | (4LL<<48);
    results[1] = __vreduce_vi16(v16);

    /* vi8: [1,2,3,4,5,6,7,8] → sum = 36 */
    long long v8 = 1LL|(2LL<<8)|(3LL<<16)|(4LL<<24)|(5LL<<32)|(6LL<<40)|(7LL<<48)|(8LL<<56);
    results[2] = __vreduce_vi8(v8);

    return 1;
}

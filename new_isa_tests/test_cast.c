/* test_cast.c — tests $cast instruction via C type casts

   Each check writes its computed value into results[] -- see
   isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20 for why. */
typedef signed char  int8_t;
typedef short        int16_t;
typedef int          int32_t;
typedef long long    int64_t;

#define N_RESULTS 4
long long results[N_RESULTS];

int main() {
    int64_t big = 0x12345678ABCDEFL;

    /* Cast to narrower integer types — should emit $cast instructions */
    int8_t  a = (int8_t)  big;   /* $cast ($i8)  dest ($i64) src */
    int16_t b = (int16_t) big;   /* $cast ($i16) dest ($i64) src */
    int32_t c = (int32_t) big;   /* $cast ($i32) dest ($i64) src */

    results[0] = a;
    results[1] = b;
    results[2] = c;
    results[3] = a + b + c;

    return 1;
}

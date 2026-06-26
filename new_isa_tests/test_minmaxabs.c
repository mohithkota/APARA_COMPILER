/* test_minmaxabs.c — exercises the $abs / $max / $min single-instruction
   intrinsics added 2026-06-26. Each result is an independent check verified
   against gcc + golden_stubs.h. See compiler/STATUS.md.

   NOTE (recorded from the simulator audit): the scalar ALU executor
   (__uexec_64__ in McodeOperations.cpp) implements neither __MAX nor __MIN
   (they hit default: assert(0)). This test is the empirical probe that
   confirms which of these instructions the simulator can actually run. */
#define N_RESULTS 8
long long results[N_RESULTS];

long long __abs    (long long x);
int       __abs_i32(int x);
long long __max    (long long a, long long b);
long long __min    (long long a, long long b);
int       __max_i32(int a, int b);
int       __min_i32(int a, int b);

int main(void) {
    results[0] = __abs(-5);          /* 5       */
    results[1] = __abs(7);           /* 7       */
    results[2] = __abs(-1000000);    /* 1000000 */
    results[3] = __abs_i32(-123);    /* 123     */
    results[4] = __max(3, 9);        /* 9       */
    results[5] = __min(3, 9);        /* 3       */
    results[6] = __max_i32(-4, -8);  /* -4      */
    results[7] = __min_i32(-4, -8);  /* -8      */
    return 1;
}

/* va03: variadic calling variadic, variadic arg built from a variadic call,
   and a variadic function that also calls a normal function */
#ifdef __APARA__
long long *__va_start();
#define va_list long long *
#define va_start(ap, last) ((ap) = __va_start())
#define va_arg(ap, type)   ((type)*(ap)++)
#define va_end(ap)
#else
#include <stdarg.h>
#endif

long long results[4];

long long twice(long long x) { return 2 * x; }

long long sum(int n, ...) {
    va_list ap;
    va_start(ap, n);
    long long s = 0;
    for (int i = 0; i < n; i++)
        s += va_arg(ap, long long);
    va_end(ap);
    return s;
}

/* variadic that itself makes calls (normal + variadic) while walking its args */
long long sum_doubled(int n, ...) {
    va_list ap;
    va_start(ap, n);
    long long s = 0;
    for (int i = 0; i < n; i++)
        s += twice(va_arg(ap, long long));
    va_end(ap);
    return s + sum(2, 1LL, 1LL);   /* +2 */
}

int main() {
    results[0] = sum(2, sum(2, 1LL, 2LL), sum(3, 1LL, 2LL, 3LL)); /* 3+6=9 */
    results[1] = sum_doubled(3, 1LL, 2LL, 3LL);                   /* 12+2=14 */
    results[2] = sum(1, sum_doubled(1, 10LL));                    /* 20+2=22 */
    results[3] = twice(sum(4, 1LL, 2LL, 3LL, 4LL));               /* 20 */
    return 1;
}

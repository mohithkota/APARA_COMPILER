/* va01: basic variadic function — sum of n long longs */
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

long long sum(int n, ...) {
    va_list ap;
    long long s = 0;
    va_start(ap, n);
    for (int i = 0; i < n; i++)
        s += va_arg(ap, long long);
    va_end(ap);
    return s;
}

int main() {
    results[0] = sum(0);                          /* 0 */
    results[1] = sum(1, 42LL);                    /* 42 */
    results[2] = sum(3, 10LL, 20LL, 30LL);        /* 60 */
    results[3] = sum(6, 1LL, 2LL, 3LL, 4LL, 5LL, 6LL);  /* 21 */
    return 1;
}

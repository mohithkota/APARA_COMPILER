/* ma03: the combined case — a VARIADIC function with 5 named params
   (named 5th goes on the stack BEFORE the variadic extras), plus
   >4-arg and variadic functions calling each other */
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

long long add5(long long a, long long b, long long c, long long d, long long e) {
    return a + b + c + d + e;
}

/* five named params, then extras: s = base5 + sum of n extras */
long long sum5plus(long long b1, long long b2, long long b3, long long b4,
                   int n, ...) {
    va_list ap;
    va_start(ap, n);
    long long s = b1 + b2 + b3 + b4;
    for (int i = 0; i < n; i++)
        s += va_arg(ap, long long);
    va_end(ap);
    return s;
}

int main() {
    results[0] = sum5plus(1, 2, 3, 4, 0);                       /* 10 */
    results[1] = sum5plus(1, 2, 3, 4, 3, 10LL, 20LL, 30LL);     /* 70 */
    results[2] = add5(1, 2, 3, 4, sum5plus(0, 0, 0, 0, 2, 5LL, 5LL));  /* 20 */
    results[3] = sum5plus(add5(1,1,1,1,1), 0, 0, 0, 1,
                          add5(2,2,2,2,2));                     /* 5+10=15 */
    return 1;
}

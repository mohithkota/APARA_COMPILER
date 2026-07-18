/* va02: variadic with several named params, non-constant args, calls in a loop */
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
long long data[5] = {7, 3, 9, 1, 5};

/* scale * (max of n values) + bias : two named params before the ellipsis */
long long scaled_max(long long scale, int n, ...) {
    va_list ap;
    va_start(ap, n);
    long long m = va_arg(ap, long long);
    for (int i = 1; i < n; i++) {
        long long v = va_arg(ap, long long);
        if (v > m) m = v;
    }
    va_end(ap);
    return scale * m;
}

int main() {
    results[0] = scaled_max(2, 3, data[0], data[1], data[2]);   /* 2*9=18 */
    results[1] = scaled_max(1, 5, data[0], data[1], data[2], data[3], data[4]); /* 9 */
    long long acc = 0;
    for (int i = 0; i < 3; i++)                    /* loop of variadic calls */
        acc += scaled_max(i + 1, 2, data[i], data[i + 1]);
    results[2] = acc;                              /* 1*7 + 2*9 + 3*9 = 52 */
    results[3] = scaled_max(3, 1, data[3] * 10 + 4);            /* 3*14=42 */
    return 1;
}

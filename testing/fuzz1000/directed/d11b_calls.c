/* d11: goto, do-while, switch fall-through, comma, ternary chains, short-circuit,
   fnptr + variadic + >4 args + recursion in ONE program */
long long results[8];
#ifdef __APARA__
long long *__va_start();
#define va_list long long *
#define va_start(ap, last) ((ap) = __va_start())
#define va_arg(ap, type)   ((type)*(ap)++)
#define va_end(ap)
#else
#include <stdarg.h>
#endif
long long add6(long long a, long long b, long long c, long long d, long long e, long long f) {
    return a + b * 2 + c * 3 + d * 4 + e * 5 + f * 6;
}
long long vmax(int n, ...) {
    va_list ap; va_start(ap, n);
    long long m = va_arg(ap, long long);
    for (int i = 1; i < n; i++) { long long v = va_arg(ap, long long); if (v > m) m = v; }
    va_end(ap); return m;
}
long long twice(long long x) { return 2 * x; }
long long thrice(long long x) { return 3 * x; }
int main() {
    results[0] = add6(1, 2, 3, 4, 5, 6);
    results[1] = vmax(5, 3LL, 99LL, -7LL, 42LL, 8LL);
    long long (*f)(long long) = (results[0] & 1) ? twice : thrice;
    results[2] = f(17);
    f = twice;
    results[3] = f(f(5));
    results[4] = vmax(2, add6(1,1,1,1,1,1), 20LL);
    results[5] = add6(results[1], 1, 1, 1, 1, 1) & 0xffff;
    results[6] = 0; for (int i = 0; i < 3; i++) results[6] += f(i);
    results[7] = twice(thrice(4));
    return 1;
}

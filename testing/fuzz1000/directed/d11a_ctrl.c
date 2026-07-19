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
long long fib(long long n) { return n < 2 ? n : fib(n - 1) + fib(n - 2); }
int main() {
    long long a = 0, i = 0;
loop:
    a += ++i;
    if (i < 5) goto loop;
    results[0] = a;                                   /* 15 */
    long long b = 10; do { b = (b & 1) ? b * 3 + 1 : b / 2; } while (b != 1);
    results[1] = b + a;
    long long c = 2;
    switch (c) { case 1: a += 100; case 2: a += 10; case 3: a += 1; break; default: a = 0; }
    results[2] = a;
    results[3] = (a > 20 ? (a > 25 ? 1 : 2) : 3) + (i = 7, i * 2);
    results[4] = (a > 0 && i > 5) + (a < 0 || i == 7) * 2;
    results[5] = fib(9);
    results[6] = (a < 100) + (a <= 26) * 2;           /* < and <= branches */
    results[7] = a % 7 + a / 7;
    return 1;
}

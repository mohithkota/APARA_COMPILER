/* ma01: more than 4 named args — 5, 6, and 8 parameter functions */
long long results[4];

long long add5(long long a, long long b, long long c, long long d, long long e) {
    return a + b + c + d + e;
}

long long add8(long long a, long long b, long long c, long long d,
               long long e, long long f, long long g, long long h) {
    return a + 10*b + 100*c + 1000*d + 10000*e + 100000*f + 1000000*g + 10000000*h;
}

long long weight6(long long a, long long b, long long c,
                  long long d, long long e, long long f) {
    return a*f + b*e + c*d;
}

int main() {
    results[0] = add5(1, 2, 3, 4, 5);                    /* 15 */
    results[1] = add8(1, 2, 3, 4, 5, 6, 7, 8);           /* 87654321 */
    results[2] = weight6(1, 2, 3, 4, 5, 6);              /* 6+10+12=28 */
    results[3] = add5(add5(1,1,1,1,1), 2*3, 40/4, -5, 100); /* 5+6+10-5+100=116 */
    return 1;
}

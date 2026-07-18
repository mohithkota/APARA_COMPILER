/* fn02: function pointer as parameter (callback pattern) */
long long results[4];

long long twice(long long x)  { return 2 * x; }
long long square(long long x) { return x * x; }

long long apply(long long (*f)(long long), long long v) { return f(v); }

long long fold(long long (*f)(long long), long long *a, int n) {
    long long s = 0;
    for (int i = 0; i < n; i++) s += f(a[i]);
    return s;
}

long long data[4] = {1, 2, 3, 4};

int main() {
    results[0] = apply(twice, 21);         /* 42 */
    results[1] = apply(square, 9);         /* 81 */
    results[2] = fold(twice, data, 4);     /* 20 */
    results[3] = fold(square, data, 4);    /* 30 */
    return 1;
}

/* d03: all 6 $cmov conditions + $cmov-lowered abs/max/min */
long long results[8];
int __cmov_gt(int, int, int); int __cmov_lt(int, int, int);
int __cmov_eq(int, int, int); int __cmov_ne(int, int, int);
int __cmov_ge(int, int, int); int __cmov_le(int, int, int);
long long __abs(long long); long long __max(long long, long long);
long long __min(long long, long long);
long long g = -12;
int main() {
    results[0] = __cmov_gt(5, 10, 20) + __cmov_gt(-5, 10, 20);
    results[1] = __cmov_lt(-3, 1, 2) + __cmov_lt(3, 1, 2);
    results[2] = __cmov_eq(0, 7, 8) + __cmov_eq(4, 7, 8);
    results[3] = __cmov_ne(0, 7, 8) + __cmov_ne(4, 7, 8);
    results[4] = __cmov_ge(0, 3, 4) + __cmov_ge(-1, 3, 4);
    results[5] = __cmov_le(0, 3, 4) + __cmov_le(1, 3, 4);
    results[6] = __abs(g) + __abs(-g) + __abs(0);
    results[7] = __max(g, 5) * __min(g, 5);
    return 1;
}

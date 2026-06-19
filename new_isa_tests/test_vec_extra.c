// test_vec_extra.c — probes previously-untested vector type suffixes: vi4, vu8
long long g;

int main() {
    long long a = 0x1234567890abcdefLL;
    long long b = 0x1111111111111111LL;
    long long r1 = __vadd_vi4(a, b);
    long long r2 = __vadd_vu8(a, b);
    g = r1 + r2;
    return (int)(g & 0xff);
}

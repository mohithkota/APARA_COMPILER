/* d09: every integer ALU op signed AND unsigned, all 6 int compares, %, unary ops */
long long results[8];
long long ga = -1234, gb = 567;
unsigned long long ua = 0xfedcba9876543210ULL, ub = 0x1234;
int main() {
    results[0] = (ga + gb) * 3 - (ga - gb);
    results[1] = (ga & gb) | (ga ^ gb);
    results[2] = ((ga & 0xffffff) << 5) + ((gb & 0xffffff) >> 2);
    results[3] = (ga / 7) + (ga % 7) * 100 + (gb / -3);
    results[4] = (ua & ub) + (ua % 97) + (ub * 3) + (ua / 1000) + (ua >> 13);
    results[5] = (ga < gb) + (ga <= gb) * 2 + (ga > gb) * 4 + (ga >= gb) * 8
               + (ga == gb) * 16 + (ga != gb) * 32;
    results[6] = ~ga + (-gb) + !ga + !0;
    long long x = gb;
    x += 5; x -= 2; x *= 3; x /= 2; x <<= 1; x >>= 2; x |= 0xf0; x &= 0xffff; x ^= 0x33;
    results[7] = x + (++ga) + (gb--);
#ifdef __APARA__
    __nop();
#endif
    return 1;
}

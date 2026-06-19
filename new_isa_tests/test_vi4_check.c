// Minimal hand-verifiable vi4 check.
// a = 0x1111111111111111 -> 16 nibbles, each = 1
// b = 0x2222222222222222 -> 16 nibbles, each = 2
// vi4 add is element-wise per nibble: 1+2=3 for all 16 lanes, no overflow (range -8..7)
// expected g = 0x3333333333333333
long long g;
int main() {
    long long a = 0x1111111111111111LL;
    long long b = 0x2222222222222222LL;
    g = __vadd_vi4(a, b);
    return 0;
}

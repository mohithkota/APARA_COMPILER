// Mirror of test_u256_load.c, for the new $st ($u256) wide-store mechanism.
// __st256(dst, src): four plain 64-bit loads of src[0..3], one $st ($u256)
// writing all four quarters to dst in a single instruction.
long long src[4];
long long dst[4];

int main() {
    src[0] = 0x1111111111111111LL;
    src[1] = 0x2222222222222222LL;
    src[2] = 0x3333333333333333LL;
    src[3] = 0x4444444444444444LL;

    __st256(dst, src);

    if (dst[0] != 0x1111111111111111LL) return -1;
    if (dst[1] != 0x2222222222222222LL) return -2;
    if (dst[2] != 0x3333333333333333LL) return -3;
    if (dst[3] != 0x4444444444444444LL) return -4;
    return 1;
}

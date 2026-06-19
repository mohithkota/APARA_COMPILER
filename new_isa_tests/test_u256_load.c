// u256 load mechanics, mirroring test_u128_load.c exactly. Load one 256-bit
// value from src[0..3] into a register quad via __ld256(dst, src), store it
// back out as four u64 quarters into dst[0..3].
long long src[4];
long long dst[4];

int main() {
    src[0] = 0x1111111111111111LL;
    src[1] = 0x2222222222222222LL;
    src[2] = 0x3333333333333333LL;
    src[3] = 0x4444444444444444LL;

    __ld256(dst, src);

    if (dst[0] != 0x1111111111111111LL) return -1;
    if (dst[1] != 0x2222222222222222LL) return -2;
    if (dst[2] != 0x3333333333333333LL) return -3;
    if (dst[3] != 0x4444444444444444LL) return -4;
    return 1;
}

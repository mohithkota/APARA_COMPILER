// Mirror of test_u128_load.c: proves the new $st ($u128)/($u256) wide-store
// mechanism (IRStoreWide), the confirmed gap found while auditing ISA
// coverage -- we'd built wide load extensively but never wide store.
// __st128(dst, src): two plain 64-bit loads of src[0]/src[1], one $st ($u128)
// writing both halves to dst in a single instruction.
long long src[2];
long long dst[2];

int main() {
    src[0] = 0x1111111111111111LL;
    src[1] = 0x2222222222222222LL;

    __st128(dst, src);

    if (dst[0] != 0x1111111111111111LL) return -1;
    if (dst[1] != 0x2222222222222222LL) return -2;
    return 1;
}

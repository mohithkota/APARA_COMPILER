// ISA coverage: $pack, all 4 legal total/word combos the ISA doc lists
// (64/64, 64/32, 32/32, 32/16). The existing test_pack.c only checked one
// combo (32/16) and its own comment had the bit order backwards (assumed
// arg1=low/arg2=high; empirically it's the reverse: arg1 lands in the
// HIGH bits, arg2 in the LOW bits, confirmed by probing before writing
// this file -- see STATUS.md 2026-06-20). 64/64 and 32/32 are degenerate:
// total/word == 1, so only ONE source register is actually read -- arg1
// passes through unchanged and arg2 is silently ignored. Confirmed
// empirically, not assumed, since the ISA doc lists them as "legal"
// without describing this degenerate behavior.
long long __pack(long long a, long long b, int result_nbits, int src_nbits);

int main() {
    // 32/16: 2 source registers, each contributing 16 bits -- arg1 high, arg2 low
    long long p1 = __pack(0xBEEF, 0xDEAD, 32, 16);
    if (p1 != 0xBEEFDEAD) return -1;

    // 64/32: 2 source registers, each contributing 32 bits -- arg1 high, arg2 low
    long long p2 = __pack(0x12345678, 0x9ABCDEF0, 64, 32);
    if (p2 != 0x123456789ABCDEF0LL) return -2;

    // 32/32: degenerate, total/word=1 -- only arg1 used, arg2 ignored
    long long p3 = __pack(0x12345678, 0xAAAAAAAA, 32, 32);
    if (p3 != 0x12345678) return -3;

    // 64/64: degenerate, total/word=1 -- only arg1 used, arg2 ignored
    long long p4 = __pack(0x123456789ABCDEFLL, 0x5555555555555555LL, 64, 64);
    if (p4 != 0x123456789ABCDEFLL) return -4;

    return 1;
}

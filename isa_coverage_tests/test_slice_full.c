// ISA coverage: $slice -- nibble (4-bit), byte (8-bit), word (16-bit),
// dword (32-bit) extracts, plus a non-byte-aligned extract. The existing
// test_slice.c only checked two byte-aligned 8-bit extracts.
long long __slice(long long x, int hindex, int lindex);

int main() {
    long long val = 0xFEDCBA98;
    // binary: 1111 1110 1101 1100 1011 1010 1001 1000

    // nibble (4-bit)
    if (__slice(val, 3, 0) != 0x8) return -1;    // low nibble
    if (__slice(val, 7, 4) != 0x9) return -2;    // next nibble

    // byte (8-bit)
    if (__slice(val, 7, 0)   != 0x98) return -3;
    if (__slice(val, 15, 8)  != 0xBA) return -4;
    if (__slice(val, 31, 24) != 0xFE) return -5;

    // word (16-bit)
    if (__slice(val, 15, 0)  != 0xBA98) return -6;
    if (__slice(val, 31, 16) != 0xFEDC) return -7;

    // non-byte-aligned (8 bits, but not on a byte boundary)
    if (__slice(val, 10, 3) != 0x53) return -8;

    // dword (32-bit) -- needs a value wider than 32 bits to be meaningful
    long long val64 = 0x123456789ABCDEF0LL;
    if (__slice(val64, 31, 0)  != 0x9ABCDEF0) return -9;
    if (__slice(val64, 63, 32) != 0x12345678) return -10;

    return 1;
}

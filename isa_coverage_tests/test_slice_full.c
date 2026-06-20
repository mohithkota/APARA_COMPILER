// ISA coverage: $slice -- nibble (4-bit), byte (8-bit), word (16-bit),
// dword (32-bit) extracts, plus a non-byte-aligned extract. The existing
// test_slice.c only checked two byte-aligned 8-bit extracts.
//
// Each check writes its computed value into results[] -- see
// test_alu_full.c / golden/golden_gen.py for why.
#define N_RESULTS 10
long long results[N_RESULTS];

long long __slice(long long x, int hindex, int lindex);

int main() {
    long long val = 0xFEDCBA98;
    // binary: 1111 1110 1101 1100 1011 1010 1001 1000

    // nibble (4-bit)
    results[0] = __slice(val, 3, 0);    // low nibble
    results[1] = __slice(val, 7, 4);    // next nibble

    // byte (8-bit)
    results[2] = __slice(val, 7, 0);
    results[3] = __slice(val, 15, 8);
    results[4] = __slice(val, 31, 24);

    // word (16-bit)
    results[5] = __slice(val, 15, 0);
    results[6] = __slice(val, 31, 16);

    // non-byte-aligned (8 bits, but not on a byte boundary)
    results[7] = __slice(val, 10, 3);

    // dword (32-bit) -- needs a value wider than 32 bits to be meaningful
    long long val64 = 0x123456789ABCDEF0LL;
    results[8] = __slice(val64, 31, 0);
    results[9] = __slice(val64, 63, 32);

    return 1;
}

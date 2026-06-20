// ISA coverage: $cast narrowing matrix, signed AND unsigned, i8/i16/i32,
// using the cast result DIRECTLY (not assigned to a narrow named variable
// first) -- this is the path that exposed the $cast no-op bug fixed
// alongside this file (see STATUS.md 2026-06-20). The existing test_cast.c
// only ever assigns a cast's result to a narrow variable, where the
// subsequent store-truncate+load-extend round trip masks the bug; this
// file specifically targets the direct-use case.
//
// Each check writes its computed value into results[] -- see
// test_alu_full.c / golden/golden_gen.py for why.
#define N_RESULTS 14
long long results[N_RESULTS];

int main() {
    long long neg1 = -1;            // 0xFFFFFFFFFFFFFFFF

    // i8 / u8
    results[0] = (signed char)neg1;
    results[1] = (unsigned char)neg1;

    long long v200 = 200;            // 0xC8 -- high bit of a byte is set
    results[2] = (signed char)v200;
    results[3] = (unsigned char)v200;

    // i16 / u16
    results[4] = (short)neg1;
    results[5] = (unsigned short)neg1;

    long long v40000 = 40000;        // 0x9C40 -- high bit of a 16-bit word is set
    results[6] = (short)v40000;
    results[7] = (unsigned short)v40000;

    // i32 / u32
    results[8] = (int)neg1;
    results[9] = (unsigned int)neg1;

    long long v3bil = 3000000000LL;  // 0xB2D05E00 -- high bit of a 32-bit word is set
    results[10] = (int)v3bil;
    results[11] = (unsigned int)v3bil;

    // Same matrix, but result assigned to a narrow named variable first
    // (the existing test_cast.c's pattern) -- must still be correct, since
    // store-truncate+load-extend was independently verified in
    // test_subword_full.c and must agree with the direct-use path here.
    signed char   sc = (signed char) v200;
    unsigned char uc = (unsigned char) v200;
    results[12] = sc;
    results[13] = uc;

    return 1;
}

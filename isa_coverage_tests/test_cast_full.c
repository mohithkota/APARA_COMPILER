// ISA coverage: $cast narrowing matrix, signed AND unsigned, i8/i16/i32,
// using the cast result DIRECTLY (not assigned to a narrow named variable
// first) -- this is the path that exposed the $cast no-op bug fixed
// alongside this file (see STATUS.md 2026-06-20). The existing test_cast.c
// only ever assigns a cast's result to a narrow variable, where the
// subsequent store-truncate+load-extend round trip masks the bug; this
// file specifically targets the direct-use case.
int main() {
    long long neg1 = -1;            // 0xFFFFFFFFFFFFFFFF

    // i8 / u8
    if ((signed char)neg1 != -1)   return -1;   // 0xFF sign-extends to -1
    if ((unsigned char)neg1 != 255) return -2;   // 0xFF zero-extends to 255

    long long v200 = 200;            // 0xC8 -- high bit of a byte is set
    if ((signed char)v200 != -56)   return -3;   // 0xC8 sign-extends to -56
    if ((unsigned char)v200 != 200) return -4;   // 0xC8 zero-extends to 200

    // i16 / u16
    if ((short)neg1 != -1)          return -5;   // 0xFFFF sign-extends to -1
    if ((unsigned short)neg1 != 65535) return -6; // 0xFFFF zero-extends to 65535

    long long v40000 = 40000;        // 0x9C40 -- high bit of a 16-bit word is set
    if ((short)v40000 != -25536)         return -7;  // 0x9C40 sign-extends to -25536
    if ((unsigned short)v40000 != 40000) return -8;   // 0x9C40 zero-extends to 40000

    // i32 / u32
    if ((int)neg1 != -1)             return -9;   // 0xFFFFFFFF sign-extends to -1
    if ((unsigned int)neg1 != 0xFFFFFFFFLL) return -10; // zero-extends to 4294967295

    long long v3bil = 3000000000LL;  // 0xB2D05E00 -- high bit of a 32-bit word is set
    if ((int)v3bil != -1294967296)         return -11; // sign-extends negative
    if ((unsigned int)v3bil != 3000000000LL) return -12; // zero-extends positive

    // Same matrix, but result assigned to a narrow named variable first
    // (the existing test_cast.c's pattern) -- must still be correct, since
    // store-truncate+load-extend was independently verified in
    // test_subword_full.c and must agree with the direct-use path here.
    signed char   sc = (signed char) v200;
    unsigned char uc = (unsigned char) v200;
    if (sc != -56) return -13;
    if (uc != 200) return -14;

    return 1;
}

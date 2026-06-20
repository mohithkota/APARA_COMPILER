// ISA coverage: scalar sub-word load/store -- sign- vs zero-extension and
// wraparound, signed AND unsigned, local AND global, scalar AND array.
// Extends the existing test_subword.c, which never used unsigned types or
// boundary/overflow values (everything fit trivially in any width, so it
// could never have caught the unsigned sign-extension bug fixed alongside
// this file -- see STATUS.md 2026-06-20).
unsigned char  g_uc;
unsigned short g_us;
unsigned int   g_ui;
signed char    g_sc;
short          g_ss;
unsigned char  g_uc_arr[3];

int main() {
    // unsigned char: max value must read back positive, not sign-extended
    unsigned char uc = 255;
    if (uc != 255) return -1;
    long long x1 = uc;
    if (x1 != 255) return -2;

    // signed char: -1 sign-extends to a full 64-bit -1
    signed char sc = -1;
    long long x2 = sc;
    if (x2 != -1) return -3;

    // unsigned char wraparound: 255 + 1 = 0
    unsigned char uc2 = 255;
    uc2 = uc2 + 1;
    if (uc2 != 0) return -4;

    // signed char wraparound: 127 + 1 = -128
    signed char sc2 = 127;
    sc2 = sc2 + 1;
    if (sc2 != -128) return -5;

    // unsigned short: max value
    unsigned short us = 65535;
    if (us != 65535) return -6;
    long long x3 = us;
    if (x3 != 65535) return -7;

    // signed short: -1 sign-extends
    short ss = -1;
    long long x4 = ss;
    if (x4 != -1) return -8;

    // unsigned short wraparound
    unsigned short us2 = 65535;
    us2 = us2 + 1;
    if (us2 != 0) return -9;

    // signed short wraparound: 32767 + 1 = -32768
    short ss2 = 32767;
    ss2 = ss2 + 1;
    if (ss2 != -32768) return -10;

    // unsigned int: large value, no sign-extension
    unsigned int ui = 0xFFFFFFFF;
    long long x5 = ui;
    if (x5 != 0xFFFFFFFFLL) return -11;

    // signed int: -1 sign-extends (baseline, should already work)
    int si = -1;
    long long x6 = si;
    if (x6 != -1) return -12;

    // global unsigned char
    g_uc = 255;
    if (g_uc != 255) return -13;

    // global signed char sign-extension
    g_sc = -1;
    long long x7 = g_sc;
    if (x7 != -1) return -14;

    // global unsigned short
    g_us = 65535;
    if (g_us != 65535) return -15;

    // global unsigned int
    g_ui = 0xFFFFFFFF;
    long long x8 = g_ui;
    if (x8 != 0xFFFFFFFFLL) return -16;

    // global signed short sign-extension
    g_ss = -1;
    long long x9 = g_ss;
    if (x9 != -1) return -17;

    // local unsigned char array element
    unsigned char arr[3];
    arr[0] = 255;
    if (arr[0] != 255) return -18;
    long long x10 = arr[0];
    if (x10 != 255) return -19;

    // global unsigned char array element
    g_uc_arr[0] = 255;
    if (g_uc_arr[0] != 255) return -20;
    long long x11 = g_uc_arr[0];
    if (x11 != 255) return -21;

    return 1;
}

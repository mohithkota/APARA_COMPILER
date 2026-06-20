// ISA coverage: load/store consolidation. Two angles not yet covered
// elsewhere in this suite:
//  1. Pointer-indirection access for signed/unsigned narrow types --
//     test_subword_full.c covers direct variable/array access, but every
//     unsigned-extension check so far went through _load_var/_arrayref
//     directly, never through a pointer dereference (*p).
//  2. A combined u128+u256 round trip (load THEN store) in one place --
//     test_u128_load/store.c and test_u256_load/store.c each verify load
//     and store independently, never chained together.
unsigned char  g_uc;
signed char    g_sc;
unsigned short g_us;
short          g_ss;
unsigned int   g_ui;
int            g_si;

long long src128[2];
long long mid128[2];
long long dst128[2];
long long src256[4];
long long mid256[4];
long long dst256[4];

int main() {
    // pointer-indirection: unsigned char
    unsigned char *puc = &g_uc;
    *puc = 255;
    if (*puc != 255) return -1;
    long long x1 = *puc;
    if (x1 != 255) return -2;

    // pointer-indirection: signed char sign-extends
    signed char *psc = &g_sc;
    *psc = -1;
    long long x2 = *psc;
    if (x2 != -1) return -3;

    // pointer-indirection: unsigned short
    unsigned short *pus = &g_us;
    *pus = 65535;
    long long x3 = *pus;
    if (x3 != 65535) return -4;

    // pointer-indirection: signed short sign-extends
    short *pss = &g_ss;
    *pss = -1;
    long long x4 = *pss;
    if (x4 != -1) return -5;

    // pointer-indirection: unsigned int
    unsigned int *pui = &g_ui;
    *pui = 0xFFFFFFFF;
    long long x5 = *pui;
    if (x5 != 0xFFFFFFFFLL) return -6;

    // pointer-indirection: signed int sign-extends
    int *psi = &g_si;
    *psi = -1;
    long long x6 = *psi;
    if (x6 != -1) return -7;

    // combined u128 round trip: WIDE load (src128 -> mid128 via __ld128),
    // then WIDE store (mid128 -> dst128 via __st128) -- chains both new
    // mechanisms together rather than testing each in isolation.
    src128[0] = 0x1111111111111111LL;
    src128[1] = 0x2222222222222222LL;
    __ld128(mid128, src128);
    __st128(dst128, mid128);
    if (dst128[0] != 0x1111111111111111LL) return -8;
    if (dst128[1] != 0x2222222222222222LL) return -9;

    // combined u256 round trip, same chaining
    src256[0] = 0x1111111111111111LL;
    src256[1] = 0x2222222222222222LL;
    src256[2] = 0x3333333333333333LL;
    src256[3] = 0x4444444444444444LL;
    __ld256(mid256, src256);
    __st256(dst256, mid256);
    if (dst256[0] != 0x1111111111111111LL) return -10;
    if (dst256[1] != 0x2222222222222222LL) return -11;
    if (dst256[2] != 0x3333333333333333LL) return -12;
    if (dst256[3] != 0x4444444444444444LL) return -13;

    return 1;
}

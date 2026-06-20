// ISA coverage: scalar sub-word load/store -- sign- vs zero-extension and
// wraparound, signed AND unsigned, local AND global, scalar AND array.
// Extends the existing test_subword.c, which never used unsigned types or
// boundary/overflow values (everything fit trivially in any width, so it
// could never have caught the unsigned sign-extension bug fixed alongside
// this file -- see STATUS.md 2026-06-20).
//
// Each check writes its computed value into results[] instead of an
// if/return pass-fail code -- see test_alu_full.c / golden/golden_gen.py
// for why (independent, per-value PostCondition verification).
#define N_RESULTS 21
long long results[N_RESULTS];

unsigned char  g_uc;
unsigned short g_us;
unsigned int   g_ui;
signed char    g_sc;
short          g_ss;
unsigned char  g_uc_arr[3];

int main() {
    unsigned char uc = 255;
    results[0] = uc;
    long long x1 = uc;
    results[1] = x1;

    signed char sc = -1;
    long long x2 = sc;
    results[2] = x2;

    unsigned char uc2 = 255;
    uc2 = uc2 + 1;
    results[3] = uc2;

    signed char sc2 = 127;
    sc2 = sc2 + 1;
    results[4] = sc2;

    unsigned short us = 65535;
    results[5] = us;
    long long x3 = us;
    results[6] = x3;

    short ss = -1;
    long long x4 = ss;
    results[7] = x4;

    unsigned short us2 = 65535;
    us2 = us2 + 1;
    results[8] = us2;

    short ss2 = 32767;
    ss2 = ss2 + 1;
    results[9] = ss2;

    unsigned int ui = 0xFFFFFFFF;
    long long x5 = ui;
    results[10] = x5;

    int si = -1;
    long long x6 = si;
    results[11] = x6;

    g_uc = 255;
    results[12] = g_uc;

    g_sc = -1;
    long long x7 = g_sc;
    results[13] = x7;

    g_us = 65535;
    results[14] = g_us;

    g_ui = 0xFFFFFFFF;
    long long x8 = g_ui;
    results[15] = x8;

    g_ss = -1;
    long long x9 = g_ss;
    results[16] = x9;

    unsigned char arr[3];
    arr[0] = 255;
    results[17] = arr[0];
    long long x10 = arr[0];
    results[18] = x10;

    g_uc_arr[0] = 255;
    results[19] = g_uc_arr[0];
    long long x11 = g_uc_arr[0];
    results[20] = x11;

    return 1;
}

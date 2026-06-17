/* test_subword.c — sub-word load/store now that the engine bug is fixed in the VM.
   Covers the three valid (register-width, memory-width) load/store pairs:
     i32,i32 — int   (already exercised elsewhere, included here for completeness)
     i32,i16 — short (memory width 16 bits, sign-extended/truncated at the i32 level)
     i8,i8   — char  (memory width 8 bits)
   i4 is arithmetic-only and has no load/store form — not tested here. */

char  gc_arr[4] = {1, 2, 3, 4};
short gs_arr[4] = {100, 200, 300, 400};
int   gi_arr[4] = {1000, 2000, 3000, 4000};

char  gc = 5;
short gs = 1000;
int   gi = 100000;

int main() {
    /* i8,i8 -- char local read/write */
    char c = 7;
    c = c + 1;
    if (c != 8) return -1;

    /* i8,i8 -- char array read/write */
    gc_arr[0] = 9;
    if (gc_arr[0] != 9) return -2;
    if (gc_arr[3] != 4) return -3;

    /* i32,i16 -- short local read/write */
    short s = 50;
    s = s + 25;
    if (s != 75) return -4;

    /* i32,i16 -- short array read/write */
    gs_arr[1] = 250;
    if (gs_arr[1] != 250) return -5;
    if (gs_arr[2] != 300) return -6;

    /* i32,i32 -- int local read/write */
    int i = 12345;
    i = i + 1;
    if (i != 12346) return -7;

    /* i32,i32 -- int array read/write */
    gi_arr[0] = 9999;
    if (gi_arr[0] != 9999) return -8;
    if (gi_arr[3] != 4000) return -9;

    /* global scalar char/short/int (not just arrays) */
    gc = gc + 1;
    if (gc != 6) return -10;
    gs = gs + 1;
    if (gs != 1001) return -11;
    gi = gi + 1;
    if (gi != 100001) return -12;

    return 1;
}

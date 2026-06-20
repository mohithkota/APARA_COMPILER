/* test_subword.c — sub-word load/store now that the engine bug is fixed in the VM.
   Covers the three valid (register-width, memory-width) load/store pairs:
     i32,i32 — int   (already exercised elsewhere, included here for completeness)
     i32,i16 — short (memory width 16 bits, sign-extended/truncated at the i32 level)
     i8,i8   — char  (memory width 8 bits)
   i4 is arithmetic-only and has no load/store form — not tested here.

   Each check writes its computed value into results[] -- see
   isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20 for why. */
#define N_RESULTS 12
long long results[N_RESULTS];

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
    results[0] = c;

    /* i8,i8 -- char array read/write */
    gc_arr[0] = 9;
    results[1] = gc_arr[0];
    results[2] = gc_arr[3];

    /* i32,i16 -- short local read/write */
    short s = 50;
    s = s + 25;
    results[3] = s;

    /* i32,i16 -- short array read/write */
    gs_arr[1] = 250;
    results[4] = gs_arr[1];
    results[5] = gs_arr[2];

    /* i32,i32 -- int local read/write */
    int i = 12345;
    i = i + 1;
    results[6] = i;

    /* i32,i32 -- int array read/write */
    gi_arr[0] = 9999;
    results[7] = gi_arr[0];
    results[8] = gi_arr[3];

    /* global scalar char/short/int (not just arrays) */
    gc = gc + 1;
    results[9] = gc;
    gs = gs + 1;
    results[10] = gs;
    gi = gi + 1;
    results[11] = gi;

    return 1;
}

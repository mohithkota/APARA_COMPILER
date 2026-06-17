/* test_subword_i8.c — i8,i8 sub-word load/store ONLY (char).
   Deliberately contains no int/short locals or globals so it is safe to
   hardware-verify even on an engine_isp build that still has the old
   32-bit ($i32) sub-word bug — this test never emits a $ld/$st ($i32). */

char gc_arr[4] = {1, 2, 3, 4};
char gc = 5;

int main() {
    char c = 7;
    c = c + 1;
    if (c != 8) return -1;

    gc_arr[0] = 9;
    if (gc_arr[0] != 9) return -2;
    if (gc_arr[3] != 4) return -3;

    gc = gc + 1;
    if (gc != 6) return -4;

    char a = gc_arr[1];
    char b = gc_arr[2];
    char sum = a + b;
    if (sum != 5) return -5;

    return 1;
}

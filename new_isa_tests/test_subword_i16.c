/* test_subword_i16.c — i32,i16 sub-word load/store ONLY (short).
   Deliberately contains no int locals or globals so it is safe to
   hardware-verify even on an engine_isp build that still has the old
   32-bit ($i32) sub-word bug — this test never emits a $ld/$st ($i32). */

short gs_arr[4] = {100, 200, 300, 400};
short gs = 1000;

int main() {
    short s = 50;
    s = s + 25;
    if (s != 75) return -1;

    gs_arr[1] = 250;
    if (gs_arr[1] != 250) return -2;
    if (gs_arr[2] != 300) return -3;

    gs = gs + 1;
    if (gs != 1001) return -4;

    short a = gs_arr[0];
    short b = gs_arr[3];
    short sum = a + b;
    if (sum != 500) return -5;

    return 1;
}

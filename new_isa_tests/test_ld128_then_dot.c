// Isolates the ld128->dot128 composition (load from memory into buf, read
// buf back into scalars, feed into dot128) -- the one thing NOT covered by
// Stage 1 (ld128 alone) or Stage 2 (dot128 fed literal constants directly).
unsigned char A[16];
unsigned char B[16];
long long buf[2];

int main() {
    int k;
    long long a_lo, a_hi, b_lo, b_hi, r;

    for (k = 0; k < 16; k++) { A[k] = k + 1; B[k] = 1; }   // A=1..16, B=all 1s

    __ld128(buf, A);
    a_lo = buf[0]; a_hi = buf[1];
    __ld128(buf, B);
    b_lo = buf[0]; b_hi = buf[1];

    r = __dot128_vu8(a_lo, a_hi, b_lo, b_hi);
    if (r != 136) return -1;   // sum(1..16) = 136 = 0x88, same as test_dot128_split.c
    return 1;
}

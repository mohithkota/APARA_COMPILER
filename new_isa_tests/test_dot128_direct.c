// Verifies the fused __dot128_direct_{type}(a_ptr, b_ptr): same hand-computed
// case as test_dot128_split.c (16-element vu8 dot, A=1..16, B=all 1s,
// hand-computed sum(1..16)=136=0x88) -- but loading straight from memory
// into the dot, no named intermediate variables, no IRStore anywhere in the
// lowering.
vu8_t A[16];
vu8_t B[16];

int main() {
    int k;
    for (k = 0; k < 16; k++) { A[k] = k + 1; B[k] = 1; }

    long long r = __dot128_direct_vu8(A, B);
    if (r != 136) return -1;
    return 1;
}

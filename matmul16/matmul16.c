/* matmul16.c -- 16x16 matrix multiply, vectorized on APARA.
 *
 * C = A * B, all 16x16, packed 16-bit elements (vi16_t = 4 lanes per 64-bit
 * word).  Written in i-k-j order over FLAT 1-D packed arrays, which is the
 * form the APARA vectorizer can lower:
 *
 *   - a 2-D array `T m[16][16]` is never packed (ir_gen packs only 1-D
 *     marker-typed arrays), so it must be flattened to m[i*16+j];
 *   - i-j-k order makes the inner loop stride B by 16 (a column walk), which
 *     no packed load can gather;
 *   - i-k-j hoists A[i*16+k] to a loop-invariant scalar `s`, leaving the inner
 *     loop as  C[i*16+j] += s * B[k*16+j]  -- an AXPY over contiguous rows,
 *     which lowers to `$v *` with $replicate plus `$v +`.
 *
 * results[] is read by the golden verifier: gcc compiles the same source
 * natively and the two must agree element for element.
 */
long long results[4];

int main(void) {
    vi16_t A[256], B[256], C[256];
    int i, j, k, s;

    for (i = 0; i < 256; i++) {
        A[i] = (vi16_t)(i & 3);
        B[i] = (vi16_t)((i & 7) + 1);
        C[i] = 0;
    }

    for (i = 0; i < 16; i++)
        for (k = 0; k < 16; k++) {
            s = A[i * 16 + k];                       /* loop-invariant scalar */
            for (j = 0; j < 16; j++)                 /* AXPY over a row       */
                C[i * 16 + j] += s * B[k * 16 + j];
        }

    results[0] = C[0];
    results[1] = C[17];
    results[2] = C[128];
    results[3] = C[255];
    return 0;
}

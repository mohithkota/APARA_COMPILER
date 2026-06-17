/*
 * test_2d.c — 2D array support tests for APARA compiler
 *
 * Returns 0 on full success; negative on the first failing sub-test.
 *
 * Tests:
 *  1. Global 2D array: write + read
 *  2. Local 2D array: write + read
 *  3. Row-major isolation: A[0][0] != A[0][1] != A[1][0]
 *  4. matmul2: 2x2 matrix multiply  C = A * B
 *  5. Read via function parameter (read_elem)
 *  6. Write via function parameter (set_elem)
 */

/* 3x3 global for tests 1,3,5,6 */
long long gMat[3][3];

/* Separate 2x2 globals for matmul test — must match the 2x2 param type */
long long gA2[2][2];
long long gB2[2][2];
long long gC2[2][2];

/* ── helpers for test 5/6 ────────────────────────────────────────────────── */
long long read_elem(long long mat[3][3], long long r, long long c) {
    return mat[r][c];
}

void set_elem(long long mat[3][3], long long r, long long c, long long v) {
    mat[r][c] = v;
}

/* ── 2×2 matrix multiply: C = A * B ─────────────────────────────────────── */
void matmul2(long long A[2][2], long long B[2][2], long long C[2][2]) {
    long long i;
    long long j;
    long long k;
    for (i = 0; i < 2; i++) {
        for (j = 0; j < 2; j++) {
            C[i][j] = 0;
            for (k = 0; k < 2; k++) {
                C[i][j] = C[i][j] + A[i][k] * B[k][j];
            }
        }
    }
}

long long main() {

    /* ── Test 1: global 3x3 write + read ──────────────────────────────────── */
    gMat[0][0] = 10;
    gMat[1][2] = 99;
    gMat[2][1] = 42;
    if (gMat[0][0] != 10) return -1;
    if (gMat[1][2] != 99) return -1;
    if (gMat[2][1] != 42) return -1;

    /* ── Test 2: local 2D array write + read ───────────────────────────────  */
    long long loc[2][4];
    loc[0][0] = 7;
    loc[0][3] = 55;
    loc[1][1] = 13;
    if (loc[0][0] != 7)  return -2;
    if (loc[0][3] != 55) return -2;
    if (loc[1][1] != 13) return -2;

    /* ── Test 3: row-major isolation ──────────────────────────────────────── */
    gMat[0][0] = 1;
    gMat[0][1] = 2;
    gMat[1][0] = 3;
    if (gMat[0][0] == gMat[0][1]) return -3;
    if (gMat[0][0] == gMat[1][0]) return -3;
    if (gMat[0][1] == gMat[1][0]) return -3;

    /* ── Test 4: 2×2 matmul ───────────────────────────────────────────────── */
    /* A = [[1,2],[3,4]], B = [[5,6],[7,8]] */
    /* C = A*B = [[19,22],[43,50]] */
    gA2[0][0] = 1; gA2[0][1] = 2;
    gA2[1][0] = 3; gA2[1][1] = 4;
    gB2[0][0] = 5; gB2[0][1] = 6;
    gB2[1][0] = 7; gB2[1][1] = 8;
    matmul2(gA2, gB2, gC2);
    if (gC2[0][0] != 19) return -4;
    if (gC2[0][1] != 22) return -4;
    if (gC2[1][0] != 43) return -4;
    if (gC2[1][1] != 50) return -4;

    /* ── Test 5: read via function param ──────────────────────────────────── */
    gMat[2][2] = 77;
    long long v;
    v = read_elem(gMat, 2, 2);
    if (v != 77) return -5;

    /* ── Test 6: write via function param ─────────────────────────────────── */
    set_elem(gMat, 0, 2, 123);
    if (gMat[0][2] != 123) return -6;

    return 0;
}

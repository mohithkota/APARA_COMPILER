/* packed f32 through pointer params: dot-product kernel shape (the exact
 * access pattern MNIST conv/gemm kernels use). */
#ifndef __APARA__
typedef float vf32_t;
#endif
vf32_t xs[6] = {1.0f, 2.0f, 3.0f, 4.0f, 5.0f, 6.0f};
vf32_t ws[6] = {0.5f, -1.0f, 2.0f, 0.25f, 1.0f, -0.5f};
vf32_t out[3];
long long results[4];

static void dot3(const vf32_t *a, const vf32_t *b, vf32_t *y, int n) {
    int i;
    float acc = 0.0f;
    for (i = 0; i < n; i++) acc = acc + a[i] * b[i];
    y[0] = acc;
}

int main() {
    dot3(xs, ws, out, 6);           /* .5-2+6+1+5-3 = 7.5 */
    dot3(xs + 2, ws + 2, out + 1, 3);   /* 6+1+5 = 12 */
    dot3(ws, ws, out + 2, 4);       /* .25+1+4+.0625 = 5.3125 */
    results[0] = (int)(out[0] * 2.0f);  /* 15 */
    results[1] = (int)out[1];           /* 12 */
    results[2] = (int)(out[2] * 16.0f); /* 85 */
    results[3] = (int)(out[0] + out[1] + out[2]); /* 24 (24.8125) */
    return 1;
}

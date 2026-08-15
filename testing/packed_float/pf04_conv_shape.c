/* miniature conv2d over packed f32 (2x4x4 in, 1 out channel, 3x3 kernel,
 * pad 1) — the real MNIST kernel in miniature, incl. bias + relu + guards. */
#ifndef __APARA__
typedef float vf32_t;
#endif
vf32_t x[32] = {
    1.0f, 2.0f, 3.0f, 4.0f,  5.0f, 6.0f, 7.0f, 8.0f,
    9.0f,10.0f,11.0f,12.0f, 13.0f,14.0f,15.0f,16.0f,
    0.5f, 0.5f, 0.5f, 0.5f,  1.5f, 1.5f, 1.5f, 1.5f,
    2.5f, 2.5f, 2.5f, 2.5f,  3.5f, 3.5f, 3.5f, 3.5f
};
vf32_t w[18] = {
    0.0f, 1.0f, 0.0f,  1.0f, -4.0f, 1.0f,  0.0f, 1.0f, 0.0f,
    1.0f, 0.0f, 0.0f,  0.0f,  1.0f, 0.0f,  0.0f, 0.0f, 1.0f
};
vf32_t b1[1] = {0.75f};
vf32_t y[16];
long long results[4];

static void conv(const vf32_t *px, const vf32_t *pw, const vf32_t *pb,
                 vf32_t *py, int C, int H, int W, int KH, int KW,
                 int pad, int relu) {
    int oh, ow, c, kh, kw;
    for (oh = 0; oh < H; oh++) {
        for (ow = 0; ow < W; ow++) {
            float acc = pb[0];
            for (c = 0; c < C; c++) {
                for (kh = 0; kh < KH; kh++) {
                    int ih = oh - pad + kh;
                    if (ih < 0) continue;
                    if (ih >= H) continue;
                    for (kw = 0; kw < KW; kw++) {
                        int iw = ow - pad + kw;
                        if (iw < 0) continue;
                        if (iw >= W) continue;
                        acc = acc + px[(c * H + ih) * W + iw]
                                  * pw[(c * KH + kh) * KW + kw];
                    }
                }
            }
            if (relu) { if (acc < 0.0f) acc = 0.0f; }
            py[oh * W + ow] = acc;
        }
    }
}

int main() {
    int i;
    float s = 0.0f;
    conv(x, w, b1, y, 2, 4, 4, 3, 3, 1, 1);
    for (i = 0; i < 16; i++) s = s + y[i];
    results[0] = (int)(s * 4.0f);
    results[1] = (int)(y[0] * 4.0f);
    results[2] = (int)(y[5] * 4.0f);
    results[3] = (int)(y[15] * 4.0f);
    return 1;
}

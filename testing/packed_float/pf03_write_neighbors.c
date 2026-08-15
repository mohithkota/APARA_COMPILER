/* packed f32 stores: writing one element must not clobber its word-mate.
 * Also: read-after-write across the same 64-bit word, and strided writes
 * through a pointer. */
#ifndef __APARA__
typedef float vf32_t;
#endif
vf32_t a[8];
long long results[6];

static void fill(vf32_t *p, int n, float base) {
    int i;
    for (i = 0; i < n; i++) p[i] = base + (float)i;
}

int main() {
    int i;
    fill(a, 8, 1.25f);              /* 1.25 2.25 ... 8.25 */
    a[3] = 100.5f;                  /* word-mate of a[2] */
    a[4] = -7.75f;                  /* word-mate of a[5] */
    results[0] = (int)(a[2] * 4.0f);    /* 13 (3.25*4) — must survive a[3]= */
    results[1] = (int)(a[3] * 2.0f);    /* 201 */
    results[2] = (int)(a[5] * 4.0f);    /* 25 (6.25*4) — must survive a[4]= */
    results[3] = (int)(a[4] * 4.0f);    /* -31 */
    {
        float s = 0.0f;
        for (i = 0; i < 8; i++) s = s + a[i];
        results[4] = (int)(s * 4.0f);   /* 4*(1.25+2.25+3.25+100.5-7.75+6.25+7.25+8.25) = 485 */
    }
    results[5] = (int)a[7];             /* 8 */
    return 1;
}

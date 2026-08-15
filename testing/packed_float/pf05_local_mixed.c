/* local packed f32 array + mixing packed and plain (stride-8) float arrays
 * in one program; int<->float casts on packed elements. */
#ifndef __APARA__
typedef float vf32_t;
#endif
float plain[3] = {10.0f, 20.0f, 30.0f};   /* stride-8 float array */
vf32_t packed[3] = {0.1f, 0.2f, 0.3f};    /* stride-4 */
long long results[5];

int main() {
    vf32_t loc[4];
    int i;
    for (i = 0; i < 4; i++) loc[i] = (float)(i * i) + 0.5f;  /* .5 1.5 4.5 9.5 */
    results[0] = (int)(loc[0] + loc[1] + loc[2] + loc[3]);   /* 16 */
    results[1] = (int)(plain[1] + packed[1] * 10.0f);        /* 22 */
    results[2] = (int)(plain[2] * packed[0]);                /* 3 */
    {
        float s = 0.0f;
        for (i = 0; i < 3; i++) s = s + plain[i] + packed[i] * 100.0f;
        results[3] = (int)s;                                 /* 120 */
    }
    results[4] = (int)((float)(int)packed[2] + loc[3]);      /* 9 */
    return 1;
}

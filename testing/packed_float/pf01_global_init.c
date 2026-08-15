/* packed f32 global array: init via data.map, subscript read, f32 arith.
 * Odd count so the last word is half-filled; neighbors share 64-bit words. */
#ifndef __APARA__
typedef float vf32_t;
#endif
vf32_t g[5] = {1.5f, -2.25f, 3.0f, 0.5f, 10.0f};
long long results[6];
int main() {
    results[0] = (int)(g[0] * 2.0f);          /* 3 */
    results[1] = (int)(g[1] * 4.0f);          /* -9 */
    results[2] = (int)(g[2] + g[3]);          /* 3 (3.5 -> 3) */
    results[3] = (int)(g[4] / g[0]);          /* 6 (6.66 -> 6) */
    results[4] = (int)(g[0] + g[1] + g[2] + g[3] + g[4]);  /* 12 (12.75) */
    results[5] = (g[1] < 0.0f);               /* 1 */
    return 1;
}

/* test_slice.c — tests $slice (bit-field extract)
   $slice rd hindex lindex rs
   rd = rs[hindex:lindex], zero-extended into rd[hindex-lindex:0] */

int g_out1 = 0;
int g_out2 = 0;

int __slice(int x, int hindex, int lindex);

int main() {
    int val = 0xABCD;   /* binary: 1010 1011 1100 1101 */

    /* Extract bits [7:4] = 0xC = 12 */
    g_out1 = __slice(val, 7, 4);

    /* Extract bits [15:8] = 0xAB = 171 */
    g_out2 = __slice(val, 15, 8);

    return g_out1 + g_out2;   /* expected: 12 + 171 = 183 = 0xB7 */
}

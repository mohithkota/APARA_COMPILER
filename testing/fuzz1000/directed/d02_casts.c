/* d02: every cast pair the compiler emits: int<->f32/f64, f32<->f64, truncations */
long long results[8];
long long gi = -47;
double    gd_ = 3.75;
float     gf_ = -2.25f;
int main() {
    results[0] = (long long)(double)gi * 10;            /* i64->f64->i64 */
    results[1] = (long long)((float)gi * 2.0f);         /* i64->f32, f32 arith */
    results[2] = (long long)(gd_ * 4.0);                /* f64->i64 */
    results[3] = (long long)(gf_ * 8.0f);               /* f32->i64 */
    results[4] = (long long)((double)gf_ * 100.0);      /* f32->f64 */
    results[5] = (long long)((float)gd_ * 100.0f);      /* f64->f32 */
    results[6] = (int)(gi * 3) + (short)(gi * 5) + (char)(gi & 0x3f);
    results[7] = (unsigned char)(gi * -7) + (unsigned short)(gi * -90);
    return 1;
}

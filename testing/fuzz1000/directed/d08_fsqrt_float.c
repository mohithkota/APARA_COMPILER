/* d08: $fsqrt f32/f64 + full float ALU (+ - * /) and all 6 float compares */
long long results[8];
double gd_[4] = {4.0, 2.25, 144.0, 0.0625};
float  gf_[4] = {9.0f, 6.25f, 0.25f, 400.0f};
double sqrt(double); float sqrtf(float);
int main() {
    results[0] = (long long)(sqrt(gd_[0]) + sqrt(gd_[1]) * 2.0);       /* 2+3 */
    results[1] = (long long)(sqrt(gd_[2]) - sqrt(gd_[3]) * 4.0);       /* 11 */
    results[2] = (long long)(sqrtf(gf_[0]) + sqrtf(gf_[1]) * 2.0f);    /* 8 */
    results[3] = (long long)(sqrtf(gf_[2]) * 10.0f + sqrtf(gf_[3]));   /* 25 */
    double a = gd_[1], b = gd_[0];
    results[4] = (a < b) + (a <= b) * 2 + (a > b) * 4 + (a >= b) * 8
               + (a == b) * 16 + (a != b) * 32;
    float c = gf_[2], d = gf_[2];
    results[5] = (c < d) + (c <= d) * 2 + (c > d) * 4 + (c >= d) * 8
               + (c == d) * 16 + (c != d) * 32;
    results[6] = (long long)((a + b) * (b - a) * 100.0);
    results[7] = (long long)((a * b) / (a + 0.5) * 10.0)
               + (long long)((gf_[3] / gf_[1]) * (gf_[0] - gf_[2]) * 2.0f);
    return 1;
}

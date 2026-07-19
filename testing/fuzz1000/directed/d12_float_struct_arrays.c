/* d12: float/double arrays+params+struct fields, float truthiness, unary minus */
long long results[8];
struct FP { double d; float f; long long tag; };
struct FP sf = {2.5, 1.5f, 7};
double da[3] = {1.5, -2.25, 4.0};
float  fa[3] = {0.5f, -1.25f, 8.0f};
double dsum(double *p, int n) { double s = 0; for (int i = 0; i < n; i++) s += p[i]; return s; }
float  fsum(float  *p, int n) { float  s = 0; for (int i = 0; i < n; i++) s += p[i]; return s; }
int main() {
    results[0] = (long long)(dsum(da, 3) * 100.0);      /* 325 */
    results[1] = (long long)(fsum(fa, 3) * 100.0f);     /* 725 */
    results[2] = (long long)((sf.d + sf.f) * 4.0);      /* 16 */
    sf.d = -sf.d; sf.f = -sf.f;
    results[3] = (long long)(sf.d * 2.0) + (long long)(sf.f * 2.0f);
    double z = 0.0;
    results[4] = (sf.d ? 1 : 0) + (z ? 10 : 20) + (!z ? 300 : 400);
    results[5] = sf.tag * 3;
    da[1] = da[0] * da[2]; fa[1] = fa[0] + fa[2];
    results[6] = (long long)(da[1] * 10.0) + (long long)(fa[1] * 10.0f);
    float fneg = -fa[2];
    results[7] = (long long)fneg + (long long)(-da[2]);
    return 1;
}

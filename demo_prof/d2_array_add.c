/* Demo 2 -- arrays: element-wise ADD.
 * Shows global arrays with initializers, a loop, and a function that takes
 * arrays as pointer parameters and writes through them.
 */
#define N 8

int A[N] = {  1,  2,  3,  4,  5,  6,  7,  8 };
int B[N] = { 10, 20, 30, 40, 50, 60, 70, 80 };
int C[N];

long long results[10];

/* c[i] = a[i] + b[i] */
void vec_add(int *a, int *b, int *c, int n)
{
    int i;
    for (i = 0; i < n; i++)
        c[i] = a[i] + b[i];
}

int vec_sum(int *v, int n)
{
    int i, s = 0;
    for (i = 0; i < n; i++)
        s += v[i];
    return s;
}

int main(void)
{
    int i;

    vec_add(A, B, C, N);

    for (i = 0; i < N; i++)
        results[i] = C[i];         /* 11 22 33 44 55 66 77 88 */

    results[8] = vec_sum(C, N);    /* 396 */
    results[9] = C[N - 1] - C[0];  /*  77 */

    return 1;
}

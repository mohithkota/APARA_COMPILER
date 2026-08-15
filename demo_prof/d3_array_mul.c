/* Demo 3 -- arrays: element-wise MULTIPLY, dot product, and a 4x4 matrix
 * multiply (2D arrays + a triple-nested loop -- the case the loop optimizer
 * targets).
 */
#define N 8
#define M 4

int A[N] = { 1, 2, 3, 4,  5,  6,  7,  8 };
int B[N] = { 2, 3, 4, 5,  6,  7,  8,  9 };
int P[N];

int X[M][M] = { {1,2,3,4}, {5,6,7,8}, {9,10,11,12}, {13,14,15,16} };
int Y[M][M] = { {1,0,0,0}, {0,2,0,0}, {0,0,3,0},    {0,0,0,4}     };
int Z[M][M];

long long results[26];

/* p[i] = a[i] * b[i] */
void vec_mul(int *a, int *b, int *p, int n)
{
    int i;
    for (i = 0; i < n; i++)
        p[i] = a[i] * b[i];
}

int dot(int *a, int *b, int n)
{
    int i, s = 0;
    for (i = 0; i < n; i++)
        s += a[i] * b[i];
    return s;
}

/* classic i-j-k matrix multiply on 2D arrays */
void matmul(void)
{
    int i, j, k;
    for (i = 0; i < M; i++)
        for (j = 0; j < M; j++) {
            int acc = 0;
            for (k = 0; k < M; k++)
                acc += X[i][k] * Y[k][j];
            Z[i][j] = acc;
        }
}

int main(void)
{
    int i, j;

    vec_mul(A, B, P, N);
    for (i = 0; i < N; i++)
        results[i] = P[i];          /* 2 6 12 20 30 42 56 72 */

    results[8] = dot(A, B, N);      /* 240 */

    matmul();
    for (i = 0; i < M; i++)
        for (j = 0; j < M; j++)
            results[9 + i * M + j] = Z[i][j];

    results[25] = Z[3][3];          /* 64 */

    return 1;
}

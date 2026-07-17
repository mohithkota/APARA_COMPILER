long long results[16];
int main(){
    int imin = -2147483648;          /* INT_MIN */
    unsigned u = 4000000000u;
    long long a = -1;
    int i = 5, j = -3;
    results[0]  = imin;              /* sign-extend to 64 */
    results[1]  = (long long)u;      /* zero-extend */
    results[2]  = a;                 /* -1 */
    results[3]  = (unsigned char)a;  /* 255 */
    results[4]  = (unsigned int)a;   /* 4294967295 */
    results[5]  = imin - 1;          /* wrap */
    results[6]  = i * j;             /* -15 */
    results[7]  = i > j;             /* 1 */
    results[8]  = (unsigned)j > (unsigned)i; /* 1 (huge > 5) */
    results[9]  = -j;                /* 3 */
    results[10] = a < 0;             /* 1 */
    results[11] = u + 1;             /* 4000000001 */
    results[12] = imin / -1;         /* overflow UB-ish; gcc gives INT_MIN */
    results[13] = j % i;             /* -3 */
    results[14] = i % j;             /* 2 */
    results[15] = ~a;                /* 0 */
    return 1;
}

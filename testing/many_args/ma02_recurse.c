/* ma02: >4-arg functions with recursion, loops, and array/pointer args */
long long results[4];
long long data[6] = {4, 9, 2, 7, 5, 1};

/* 6-arg recursive: fold the range [lo,hi) of arr with scale+bias per step */
long long rfold(long long *arr, int lo, int hi, long long scale,
                long long bias, long long acc) {
    if (lo >= hi) return acc;
    return rfold(arr, lo + 1, hi, scale, bias, acc + scale * arr[lo] + bias);
}

long long maxdiff5(long long a, long long b, long long c, long long d, long long e) {
    long long mx = a, mn = a;
    long long v[5]; v[0]=a; v[1]=b; v[2]=c; v[3]=d; v[4]=e;
    for (int i = 1; i < 5; i++) {
        if (v[i] > mx) mx = v[i];
        if (v[i] < mn) mn = v[i];
    }
    return mx - mn;
}

int main() {
    results[0] = rfold(data, 0, 6, 1, 0, 0);       /* 28 */
    results[1] = rfold(data, 1, 4, 2, 1, 10);      /* 10+ (18+1)+(4+1)+(14+1) = 49 */
    results[2] = maxdiff5(data[0], data[1], data[2], data[3], data[4]);  /* 9-2=7 */
    long long acc = 0;
    for (int i = 0; i < 3; i++)                     /* >4-arg call in a loop */
        acc += maxdiff5(i, 2*i, 3*i, 4*i, 10);
    results[3] = acc;                               /* 10 + 9 + 10 = 29 */
    return 1;
}

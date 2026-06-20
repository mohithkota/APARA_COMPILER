#define N_RESULTS 10
long long results[N_RESULTS];

long long arr[5];

int main() {
    long long x;
    long long *p;

    /* Test 1: pointer to local variable */
    x = 42;
    p = &x;
    results[0] = *p;

    /* Test 2: pointer to global array, basic deref */
    arr[0] = 10;
    arr[1] = 20;
    arr[2] = 30;
    arr[3] = 40;
    arr[4] = 50;
    p = &arr[0];
    results[1] = *p;

    /* Test 3: pointer indexing p[i] */
    results[2] = p[1];

    /* Test 4: pointer arithmetic  p + n */
    p = p + 2;
    results[3] = *p;

    /* Test 5: pointer increment p++ */
    p = &arr[0];
    p++;
    results[4] = *p;

    /* Test 6: pointer += n */
    p = &arr[0];
    p += 3;
    results[5] = *p;

    /* Test 7: write through pointer */
    p = &arr[0];
    *p = 99;
    results[6] = arr[0];

    /* Test 8: address of array element &arr[i] */
    p = &arr[2];
    results[7] = *p;

    /* Test 9: pointer decrement */
    p = &arr[4];
    p--;
    results[8] = *p;

    /* Test 10: loop with pointer */
    long long sum;
    sum = 0;
    arr[0] = 1; arr[1] = 2; arr[2] = 3; arr[3] = 4; arr[4] = 5;
    p = &arr[0];
    int i;
    for (i = 0; i < 5; i++) {
        sum = sum + *p;
        p++;
    }
    results[9] = sum;

    return 1;
}

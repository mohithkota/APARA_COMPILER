long long arr[5];
long long g_result = 0;

int main() {
    long long x;
    long long *p;

    /* Test 1: pointer to local variable */
    x = 42;
    p = &x;
    g_result = *p;
    if (g_result != 42) return -1;

    /* Test 2: pointer to global array, basic deref */
    arr[0] = 10;
    arr[1] = 20;
    arr[2] = 30;
    arr[3] = 40;
    arr[4] = 50;
    p = &arr[0];
    g_result = *p;
    if (g_result != 10) return -2;

    /* Test 3: pointer indexing p[i] */
    g_result = p[1];
    if (g_result != 20) return -3;

    /* Test 4: pointer arithmetic  p + n */
    p = p + 2;
    g_result = *p;
    if (g_result != 30) return -4;

    /* Test 5: pointer increment p++ */
    p = &arr[0];
    p++;
    g_result = *p;
    if (g_result != 20) return -5;

    /* Test 6: pointer += n */
    p = &arr[0];
    p += 3;
    g_result = *p;
    if (g_result != 40) return -6;

    /* Test 7: write through pointer */
    p = &arr[0];
    *p = 99;
    g_result = arr[0];
    if (g_result != 99) return -7;

    /* Test 8: address of array element &arr[i] */
    p = &arr[2];
    g_result = *p;
    if (g_result != 30) return -8;

    /* Test 9: pointer decrement */
    p = &arr[4];
    p--;
    g_result = *p;
    if (g_result != 40) return -9;

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
    g_result = sum;
    if (g_result != 15) return -10;

    return g_result;
}

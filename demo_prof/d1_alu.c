long long results[20];

int main(void)
{
    int a = 37, b = 5;
    int x = 0xF0, y = 0x3C;

    /* --- arithmetic --- */
    results[0] = a + b;        /*  42 */
    results[1] = a - b;        /*  32 */
    results[2] = a * b;        /* 185 */
    results[3] = a / b;        /*   7 */
    results[4] = a % b;        /*   2 */
    results[5] = -a;           /* -37 */

    /* --- bitwise --- */
    results[6] = x & y;        /* 0x30 */
    results[7] = x | y;        /* 0xFC */
    results[8] = x ^ y;        /* 0xCC */
    results[9] = ~x;           /* -241 */

    /* --- shifts --- */
    results[10] = a << 3;      /* 296 */
    results[11] = a >> 2;      /*   9 */
    results[12] = -64 >> 2;    /* -16, arithmetic: stays negative */

    /* --- comparisons (1 = true, 0 = false) --- */
    results[13] = a > b;       /* 1 */
    results[14] = a == b;      /* 0 */
    results[15] = a != b;      /* 1 */
    results[16] = a <= b;      /* 0 */

    /* --- logical / mixed expression --- */
    results[17] = (a > b) && (b > 0);   /* 1 */
    results[18] = (a < b) || (b == 5);  /* 1 */
    results[19] = a * a + 3 * b - 7;    /* 1377 */

    return 1;
}

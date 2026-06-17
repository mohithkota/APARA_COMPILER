/* test_cmov.c — tests $cmov (conditional move) via __cmov_* intrinsics
   $cmov (type) check cond dest src_true
   if (check cond 0) dest = src_true  else dest unchanged (= src_false) */

int g_res1 = 0;
int g_res2 = 0;
int g_res3 = 0;

int __cmov_gt(int check, int trueval, int falseval);
int __cmov_lt(int check, int trueval, int falseval);
int __cmov_eq(int check, int trueval, int falseval);
int __cmov_ne(int check, int trueval, int falseval);
int __cmov_ge(int check, int trueval, int falseval);
int __cmov_le(int check, int trueval, int falseval);

int main() {
    int pos = 5;   /* pos > 0  → picks trueval */
    int neg = -3;  /* neg < 0  → neg is < 0   */
    int zer = 0;   /* zer == 0 → picks trueval for _eq */

    /* check=5 > 0 → result = 100 */
    g_res1 = __cmov_gt(pos, 100, 999);

    /* check=-3 < 0 → result = 200 */
    g_res2 = __cmov_lt(neg, 200, 999);

    /* check=0 == 0 → result = 300 */
    g_res3 = __cmov_eq(zer, 300, 999);

    return g_res1 + g_res2 + g_res3;   /* expected: 600 = 0x258 */
}

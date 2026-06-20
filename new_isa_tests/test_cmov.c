/* test_cmov.c — tests $cmov (conditional move) via __cmov_* intrinsics
   $cmov (type) check cond dest src_true
   if (check cond 0) dest = src_true  else dest unchanged (= src_false)

   Each check writes its computed value into results[] -- see
   isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20 for why. */
#define N_RESULTS 3
long long results[N_RESULTS];

int __cmov_gt(int check, int trueval, int falseval);
int __cmov_lt(int check, int trueval, int falseval);
int __cmov_eq(int check, int trueval, int falseval);

int main() {
    int pos = 5;   /* pos > 0  → picks trueval */
    int neg = -3;  /* neg < 0  → neg is < 0   */
    int zer = 0;   /* zer == 0 → picks trueval for _eq */

    /* check=5 > 0 → result = 100 */
    results[0] = __cmov_gt(pos, 100, 999);

    /* check=-3 < 0 → result = 200 */
    results[1] = __cmov_lt(neg, 200, 999);

    /* check=0 == 0 → result = 300 */
    results[2] = __cmov_eq(zer, 300, 999);

    return 1;
}

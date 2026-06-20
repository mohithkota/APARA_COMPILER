// ISA coverage: $cmov, all 6 conditions (gt/lt/eq/ne/ge/le), each with BOTH
// a true-triggering and a false-triggering check value. The existing
// test_cmov.c only covered 3 of 6 conditions (gt/lt/eq) and only ever the
// true branch -- the false branch (dest keeps src_false) was never
// verified at all.
//
// Each check writes its computed value into results[] -- see
// test_alu_full.c / golden/golden_gen.py for why.
#define N_RESULTS 14
long long results[N_RESULTS];

int __cmov_gt(int check, int trueval, int falseval);
int __cmov_lt(int check, int trueval, int falseval);
int __cmov_eq(int check, int trueval, int falseval);
int __cmov_ne(int check, int trueval, int falseval);
int __cmov_ge(int check, int trueval, int falseval);
int __cmov_le(int check, int trueval, int falseval);

int main() {
    // gt: check > 0
    results[0] = __cmov_gt(5, 100, 999);    // true branch
    results[1] = __cmov_gt(-5, 100, 999);   // false branch

    // lt: check < 0
    results[2] = __cmov_lt(-5, 100, 999);   // true branch
    results[3] = __cmov_lt(5, 100, 999);    // false branch

    // eq: check == 0
    results[4] = __cmov_eq(0, 100, 999);    // true branch
    results[5] = __cmov_eq(5, 100, 999);    // false branch

    // ne: check != 0
    results[6] = __cmov_ne(5, 100, 999);    // true branch
    results[7] = __cmov_ne(0, 100, 999);    // false branch

    // ge: check >= 0
    results[8] = __cmov_ge(0, 100, 999);    // true branch (boundary)
    results[9] = __cmov_ge(5, 100, 999);    // true branch
    results[10] = __cmov_ge(-5, 100, 999);  // false branch

    // le: check <= 0
    results[11] = __cmov_le(0, 100, 999);   // true branch (boundary)
    results[12] = __cmov_le(-5, 100, 999);  // true branch
    results[13] = __cmov_le(5, 100, 999);   // false branch

    return 1;
}

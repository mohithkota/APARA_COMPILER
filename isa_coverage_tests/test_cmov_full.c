// ISA coverage: $cmov, all 6 conditions (gt/lt/eq/ne/ge/le), each with BOTH
// a true-triggering and a false-triggering check value. The existing
// test_cmov.c only covered 3 of 6 conditions (gt/lt/eq) and only ever the
// true branch -- the false branch (dest keeps src_false) was never
// verified at all.
int __cmov_gt(int check, int trueval, int falseval);
int __cmov_lt(int check, int trueval, int falseval);
int __cmov_eq(int check, int trueval, int falseval);
int __cmov_ne(int check, int trueval, int falseval);
int __cmov_ge(int check, int trueval, int falseval);
int __cmov_le(int check, int trueval, int falseval);

int main() {
    // gt: check > 0
    if (__cmov_gt(5, 100, 999) != 100) return -1;   // true branch
    if (__cmov_gt(-5, 100, 999) != 999) return -2;  // false branch

    // lt: check < 0
    if (__cmov_lt(-5, 100, 999) != 100) return -3;  // true branch
    if (__cmov_lt(5, 100, 999) != 999) return -4;   // false branch

    // eq: check == 0
    if (__cmov_eq(0, 100, 999) != 100) return -5;   // true branch
    if (__cmov_eq(5, 100, 999) != 999) return -6;   // false branch

    // ne: check != 0
    if (__cmov_ne(5, 100, 999) != 100) return -7;   // true branch
    if (__cmov_ne(0, 100, 999) != 999) return -8;   // false branch

    // ge: check >= 0
    if (__cmov_ge(0, 100, 999) != 100) return -9;   // true branch (boundary)
    if (__cmov_ge(5, 100, 999) != 100) return -10;  // true branch
    if (__cmov_ge(-5, 100, 999) != 999) return -11; // false branch

    // le: check <= 0
    if (__cmov_le(0, 100, 999) != 100) return -12;  // true branch (boundary)
    if (__cmov_le(-5, 100, 999) != 100) return -13; // true branch
    if (__cmov_le(5, 100, 999) != 999) return -14;  // false branch

    return 1;
}

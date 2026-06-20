// ISA coverage: branch/goto, all 6 comparison conditions
// (==, !=, >, <, >=, <=), each with explicit pass/fail for BOTH the
// true-taken and false-taken outcome. The existing test_branch.c covers
// all 6 conditions but only ever the true-taken branch, and relies on
// external inspection of result globals rather than an aggregate
// pass/fail signal.
int main() {
    long long a = 10;
    long long b = 20;
    long long c = 10;

    // ==
    if (!(a == c)) return -1;   // true-taken
    if (a == b)    return -2;   // false-taken (must NOT take the branch)

    // !=
    if (!(a != b)) return -3;   // true-taken
    if (a != c)    return -4;   // false-taken

    // >
    if (!(b > a))  return -5;   // true-taken
    if (a > b)     return -6;   // false-taken

    // <
    if (!(a < b))  return -7;   // true-taken
    if (b < a)     return -8;   // false-taken

    // >=
    if (!(a >= c)) return -9;   // true-taken (equal case)
    if (!(b >= a)) return -10;  // true-taken (greater case)
    if (a >= b)    return -11;  // false-taken

    // <=
    if (!(a <= c)) return -12;  // true-taken (equal case)
    if (!(a <= b)) return -13;  // true-taken (less case)
    if (b <= a)    return -14;  // false-taken

    return 1;
}

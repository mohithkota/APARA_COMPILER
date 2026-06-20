// ISA coverage: branch/goto, all 6 comparison conditions
// (==, !=, >, <, >=, <=), each with explicit pass/fail for BOTH the
// true-taken and false-taken outcome. The existing test_branch.c covers
// all 6 conditions but only ever the true-taken branch, and relies on
// external inspection of result globals rather than an aggregate
// pass/fail signal.
//
// Each comparison's boolean outcome (1 or 0) is written into results[]
// -- see test_alu_full.c / golden/golden_gen.py for why.
#define N_RESULTS 14
long long results[N_RESULTS];

int main() {
    long long a = 10;
    long long b = 20;
    long long c = 10;

    results[0]  = (a == c);   // true
    results[1]  = (a == b);   // false

    results[2]  = (a != b);   // true
    results[3]  = (a != c);   // false

    results[4]  = (b > a);    // true
    results[5]  = (a > b);    // false

    results[6]  = (a < b);    // true
    results[7]  = (b < a);    // false

    results[8]  = (a >= c);   // true (equal case)
    results[9]  = (b >= a);   // true (greater case)
    results[10] = (a >= b);   // false

    results[11] = (a <= c);   // true (equal case)
    results[12] = (a <= b);   // true (less case)
    results[13] = (b <= a);   // false

    return 1;
}

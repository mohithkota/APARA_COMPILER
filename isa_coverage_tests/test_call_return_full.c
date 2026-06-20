// ISA coverage: $call/$return -- multiple args, nested calls, and genuine
// recursion. The existing test_scalar_full.c's fact() uses a while loop,
// never actually stressing the RAS (return address stack) with real
// recursive calls. This file adds real recursion (factorial AND
// fibonacci -- fibonacci makes TWO recursive calls per invocation, a
// different RAS push/pop pattern than factorial's single chain) plus a
// 4-argument call (the calling convention's actual maximum -- see below)
// and a 3-level call nesting.
//
// A 5-argument version of add5() was the FIRST thing tried here, and it
// silently computed the wrong sum (15 expected, got -1's worth of wrong
// register content) instead of failing to compile: the calling convention
// hardcodes exactly 4 argument registers (r2-r5) on both the caller and
// callee side, and the 5th+ argument/parameter was just dropped with no
// error. Fixed in codegen.py (_gen_IRFuncBegin/_gen_IRCall/
// _gen_IRIndirectCall) to fail loudly at compile time instead -- see
// STATUS.md 2026-06-20. This file now tests the documented, enforced
// 4-argument maximum rather than a 5th argument that's correctly rejected.
//
// Each call's return value is written into results[] -- see
// test_alu_full.c / golden/golden_gen.py for why.
#define N_RESULTS 8
long long results[N_RESULTS];

int add4(int a, int b, int c, int d) {
    return a + b + c + d;
}

int level_c(int x) {
    return x * 2;
}
int level_b(int x) {
    return level_c(x) + 1;
}
int level_a(int x) {
    return level_b(x) + 1;
}

int fact_rec(int n) {
    if (n <= 1) return 1;
    return n * fact_rec(n - 1);
}

int fib_rec(int n) {
    if (n <= 1) return n;
    return fib_rec(n - 1) + fib_rec(n - 2);
}

int main() {
    // multiple args (4 is the calling convention's actual maximum)
    results[0] = add4(1, 2, 3, 4);

    // 3-level nested calls: level_a -> level_b -> level_c
    results[1] = level_a(5);

    // genuine recursion: factorial
    results[2] = fact_rec(5);
    results[3] = fact_rec(1);   // base case
    results[4] = fact_rec(6);

    // genuine recursion: fibonacci (two recursive calls per invocation)
    results[5] = fib_rec(0);    // base case
    results[6] = fib_rec(1);    // base case
    results[7] = fib_rec(10);

    return 1;
}

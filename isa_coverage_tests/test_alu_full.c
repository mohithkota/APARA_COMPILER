// ISA coverage: scalar integer ALU -- all 12 ops the ISA lists
// (+, -, *, /, %, &, |, ^, <<, >>, ~&, ~|, ~^).
// %  is synthesized by codegen (a - (a/b)*b), included here as a real op
// since C exposes it and it goes through the same ALU instructions.
// ~&/~|/~^ (nand/nor/xnor) have NO C operator -- only reachable via the
// __nand/__nor/__xnor intrinsics below. Zero prior test coverage anywhere
// in the existing suite before this file.
//
// Each check writes its computed value into results[] (a global array, so
// each slot lands at a known, predictable DMEM address) instead of an
// if/return pass-fail code. golden/golden_gen.py independently computes
// the expected value for every slot (via gcc + golden_stubs.h, NOT by
// reading this project's own compiler source) and emits a .result file
// with one "mem" PostCondition per slot, so the simulator itself verifies
// every individual value -- not just an aggregate signal.
#define N_RESULTS 13
long long results[N_RESULTS];

long long __nand(long long a, long long b);
long long __nor(long long a, long long b);
long long __xnor(long long a, long long b);

int main() {
    long long a = 17;
    long long b = 5;

    results[0] = a + b;
    results[1] = a - b;
    results[2] = a * b;
    results[3] = a / b;
    results[4] = a % b;

    long long x = 12;   // 0b1100
    long long y = 10;   // 0b1010

    results[5] = x & y;
    results[6] = x | y;
    results[7] = x ^ y;
    results[8] = x << 2;
    results[9] = x >> 1;

    results[10] = __nand(x, y);
    results[11] = __nor(x, y);
    results[12] = __xnor(x, y);

    return 1;
}

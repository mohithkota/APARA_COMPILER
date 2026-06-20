// ISA coverage: scalar integer ALU -- all 12 ops the ISA lists
// (+, -, *, /, %, &, |, ^, <<, >>, ~&, ~|, ~^).
// %  is synthesized by codegen (a - (a/b)*b), included here as a real op
// since C exposes it and it goes through the same ALU instructions.
// ~&/~|/~^ (nand/nor/xnor) have NO C operator -- only reachable via the
// __nand/__nor/__xnor intrinsics below. Zero prior test coverage anywhere
// in the existing suite before this file.
long long __nand(long long a, long long b);
long long __nor(long long a, long long b);
long long __xnor(long long a, long long b);

int main() {
    long long a = 17;
    long long b = 5;

    if (a + b != 22) return -1;
    if (a - b != 12) return -2;
    if (a * b != 85) return -3;
    if (a / b != 3)  return -4;
    if (a % b != 2)  return -5;

    long long x = 12;   // 0b1100
    long long y = 10;   // 0b1010

    if ((x & y)  != 8)   return -6;
    if ((x | y)  != 14)  return -7;
    if ((x ^ y)  != 6)   return -8;
    if ((x << 2) != 48)  return -9;
    if ((x >> 1) != 6)   return -10;

    if (__nand(x, y) != -9)  return -11;   // ~(12&10)  = ~8  = -9
    if (__nor (x, y) != -15) return -12;   // ~(12|10)  = ~14 = -15
    if (__xnor(x, y) != -7)  return -13;   // ~(12^10)  = ~6  = -7

    return 1;
}

// ISA coverage: $dot / $dot $accumulate, vi8/vu8/vi16/vu16 (vi4/vu4 excluded
// per established direction; vi32/vu32 excluded since DOT is ISA-restricted
// to source element types up to 16 bits). The existing test_dot.c only
// covered vi16. Unlike $vreduce (confirmed buggy for unsigned -- see
// test_vreduce_full.c), $dot's sign/unsigned handling was verified correct
// here via a one-negative-element probe before writing this file, since the
// underlying mechanism (Generic_Operator/Cast_Up_To_u64) looked structurally
// different from vreduce's shadowing bug.
long long __dot_vi8(long long a, long long b);
long long __dot_vu8(long long a, long long b);
long long __dot_acc_vi8(long long acc, long long a, long long b);
long long __dot_acc_vu8(long long acc, long long a, long long b);

long long __dot_vi16(long long a, long long b);
long long __dot_vu16(long long a, long long b);
long long __dot_acc_vi16(long long acc, long long a, long long b);
long long __dot_acc_vu16(long long acc, long long a, long long b);

int main() {
    // 8-bit, positive only: a=[1..8], b=[1,1,1,1,1,1,1,1], dot=36
    long long a8 = 0x807060504030201LL;
    long long b8 = 0x101010101010101LL;
    if (__dot_vi8(a8, b8) != 36) return -1;
    if (__dot_vu8(a8, b8) != 36) return -2;
    if (__dot_acc_vi8(100LL, a8, b8) != 136) return -3;
    if (__dot_acc_vu8(100LL, a8, b8) != 136) return -4;

    // 8-bit, one negative element: a=[1,2,3,-4,5,6,7,8], b=all-ones
    long long n8 = 0x8070605fc030201LL;
    if (__dot_vi8(n8, b8) != 28)  return -5;   // signed: -4 contributes -4
    if (__dot_vu8(n8, b8) != 284) return -6;   // unsigned: 0xFC=252 contributes 252

    // 16-bit, positive only: a=[1,2,3,4], b=[1,1,1,1], dot=10
    long long a16 = 0x4000300020001LL;
    long long b16 = 0x1000100010001LL;
    if (__dot_vi16(a16, b16) != 10) return -7;
    if (__dot_vu16(a16, b16) != 10) return -8;
    if (__dot_acc_vi16(50LL, a16, b16) != 60) return -9;
    if (__dot_acc_vu16(50LL, a16, b16) != 60) return -10;

    // 16-bit, one negative element: a=[1,2,3,-4], b=all-ones
    long long n16 = 0xfffc000300020001LL;
    if (__dot_vi16(n16, b16) != 2)     return -11;  // signed: -4 contributes -4
    if (__dot_vu16(n16, b16) != 65538) return -12;  // unsigned: 0xFFFC=65532 contributes 65532

    return 1;
}

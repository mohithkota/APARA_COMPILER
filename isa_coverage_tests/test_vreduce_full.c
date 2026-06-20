// ISA coverage: $vreduce (sum-reduce), vi8/vu8/vi16/vu16/vi32/vu32.
// Only the ADD sub-op is implemented in codegen.py (_gen_IRVecReduce
// hardcodes "$vreduce +") -- the ISA allows MAX/MUL/AND/OR/XOR/XNOR as
// reduce sub-ops too, but those are NOT reachable from this compiler, so
// not tested here (a real, separate gap from this session's audit, not
// something to silently paper over).
//
// The existing test_vreduce.c only used positive elements, where signed
// vs unsigned reduce can't be told apart (no sign-extension boundary is
// crossed). This file adds a vector with one negative element per width,
// where signed reduce must sign-extend before summing and unsigned reduce
// must zero-extend -- the two give different sums, so this actually
// distinguishes correct from incorrect behavior.
long long __vreduce_vi8(long long a);
long long __vreduce_vu8(long long a);
long long __vreduce_vi16(long long a);
long long __vreduce_vu16(long long a);
long long __vreduce_vi32(long long a);
long long __vreduce_vu32(long long a);

int main() {
    // positive-only: signed and unsigned must agree
    long long a8  = 0x807060504030201LL;   // [1..8]
    long long a16 = 0x4000300020001LL;     // [1,2,3,4]
    long long a32 = 0x200000001LL;         // [1,2]

    if (__vreduce_vi8(a8)   != 36) return -1;
    if (__vreduce_vu8(a8)   != 36) return -2;
    if (__vreduce_vi16(a16) != 10) return -3;
    if (__vreduce_vu16(a16) != 10) return -4;
    if (__vreduce_vi32(a32) != 3)  return -5;
    if (__vreduce_vu32(a32) != 3)  return -6;

    // one negative element per width -- architecturally, signed vs unsigned
    // reduce should differ here (signed sign-extends, unsigned zero-extends).
    //
    // CONFIRMED HARDWARE/SIMULATOR BUG (not a compiler bug -- the compiler
    // correctly emits "$vreduce + rd ($vu8) rs"): McodeOperations.cpp's
    // __vreduce_operation__ unconditionally sign-extends `ele` into `r`
    // (line ~151) BEFORE the signed/unsigned branch, and the unsigned branch
    // (the `else` at ~174) reuses that same sign-extended `r` instead of the
    // raw value -- only the signed branch redeclares its own (identical) `r`.
    // So __vreduce_vu8/vu16/vu32 currently sign-extend exactly like their
    // signed counterparts on real hardware. Asserting the CONFIRMED actual
    // behavior here (not the architecturally-correct one) so this test suite
    // reflects reality; see STATUS.md 2026-06-20 for the full report.
    long long n8  = 0x8070605fc030201LL;   // [1,2,3,-4,5,6,7,8]
    long long n16 = 0xfffc000300020001LL;  // [1,2,3,-4]
    long long n32 = 0xfffffffe00000001LL;  // [1,-2]

    if (__vreduce_vi8(n8)   != 28) return -7;  // sign-extend -4 (correct)
    if (__vreduce_vu8(n8)   != 28) return -8;  // BUG: should be 284 (zero-extend), gives 28
    if (__vreduce_vi16(n16) != 2)  return -9;  // sign-extend -4 (correct)
    if (__vreduce_vu16(n16) != 2)  return -10; // BUG: should be 65538, gives 2
    if (__vreduce_vi32(n32) != -1) return -11; // sign-extend -2 (correct)
    if (__vreduce_vu32(n32) != -1) return -12; // BUG: should be 4294967295, gives -1

    return 1;
}

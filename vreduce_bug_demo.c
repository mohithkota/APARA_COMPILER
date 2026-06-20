// Standalone demonstration of the confirmed $vreduce bug in the APARA
// simulator (engine_isp/assembler/src/McodeOperations.cpp,
// __vreduce_operation__) -- see ENGINE_ISP_BUG_REPORT.md for the full
// writeup. This file isolates just the minimum needed to show it clearly:
//
//   1. Positive-only control (no element has its high bit set): signed and
//      unsigned reduce MUST agree, and do -- proving the instruction
//      tagging/dispatch itself is fine.
//   2. One negative-bit-pattern element per width: signed reduce sign-
//      extends (correct), unsigned reduce SHOULD zero-extend (giving a
//      different, larger result) but the simulator's actual bug makes it
//      sign-extend too, silently matching the signed answer instead.
//
// Each value is written into results[] and independently verified --
// see isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20
// for the "no bias" methodology (gcc + golden_stubs.h ground truth,
// checked against the real simulator's own PostCondition mechanism).
// golden_stubs.h's __vreduce_* implementations are architecturally
// correct (zero-extend for unsigned) on purpose, so results[3]/[5]/[7]
// below are EXPECTED to show "Error: PostCondition ..." when this runs
// against the real simulator -- that mismatch IS the bug, not a flaw in
// this test.
#define N_RESULTS 8
long long results[N_RESULTS];

long long __vreduce_vi8(long long a);
long long __vreduce_vu8(long long a);
long long __vreduce_vi16(long long a);
long long __vreduce_vu16(long long a);
long long __vreduce_vi32(long long a);
long long __vreduce_vu32(long long a);

int main() {
    // ---- Control: all-positive, no high bit set anywhere ----
    // vu8/vi8 vector [1,2,3,4,5,6,7,8] -> sum = 36, both ways, no ambiguity.
    long long a8 = 0x807060504030201LL;
    results[0] = __vreduce_vi8(a8);   // expect 36
    results[1] = __vreduce_vu8(a8);   // expect 36 -- must equal results[0]

    // ---- The bug: vu8, one element with its high bit set ----
    // [1,2,3,0xFC,5,6,7,8] -- 0xFC = 252 unsigned, -4 signed.
    long long n8 = 0x8070605fc030201LL;
    results[2] = __vreduce_vi8(n8);   // expect 28  (1+2+3-4+5+6+7+8, sign-extended -- correct)
    results[3] = __vreduce_vu8(n8);   // expect 284 (1+2+3+252+5+6+7+8, zero-extended -- BUG: simulator gives 28)

    // ---- The bug: vu16, one element with its high bit set ----
    // [1,2,3,0xFFFC] -- 0xFFFC = 65532 unsigned, -4 signed.
    long long n16 = 0xfffc000300020001LL;
    results[4] = __vreduce_vi16(n16); // expect 2     (1+2+3-4, sign-extended -- correct)
    results[5] = __vreduce_vu16(n16); // expect 65538 (1+2+3+65532, zero-extended -- BUG: simulator gives 2)

    // ---- The bug: vu32, one element with its high bit set ----
    // [1,0xFFFFFFFE] -- 0xFFFFFFFE = 4294967294 unsigned, -2 signed.
    long long n32 = 0xfffffffe00000001LL;
    results[6] = __vreduce_vi32(n32); // expect -1          (1-2, sign-extended -- correct)
    results[7] = __vreduce_vu32(n32); // expect 4294967295  (1+4294967294, zero-extended -- BUG: simulator gives -1)

    return 1;
}

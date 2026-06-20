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
//
// Each check writes its computed value into results[] -- see
// test_alu_full.c / golden/golden_gen.py for why. golden_stubs.h's
// __vreduce_* implementations are architecturally correct (zero-extend
// for unsigned), per the user's explicit "no bias" instruction. The real
// simulator has a CONFIRMED bug (McodeOperations.cpp's
// __vreduce_operation__ unconditionally sign-extends `ele` before the
// signed/unsigned branch, and the unsigned branch reuses that same
// sign-extended value instead of the raw one) -- so results[7]/[9]/[11]
// below (the unsigned, one-negative-element cases) are EXPECTED to show
// "Error: PostCondition" when run against the real simulator: the golden
// file holds the correct answer (284/65538/4294967295), the simulator
// actually produces the signed-equivalent answer (28/2/-1). That
// mismatch is the documented bug, not a flaw in this test or its golden
// values. See STATUS.md 2026-06-20 for the full report.
#define N_RESULTS 12
long long results[N_RESULTS];

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

    results[0] = __vreduce_vi8(a8);
    results[1] = __vreduce_vu8(a8);
    results[2] = __vreduce_vi16(a16);
    results[3] = __vreduce_vu16(a16);
    results[4] = __vreduce_vi32(a32);
    results[5] = __vreduce_vu32(a32);

    // one negative element per width -- see the long comment above:
    // results[7]/[9]/[11] are EXPECTED to mismatch against the real
    // simulator (confirmed hardware bug), not against this golden file.
    long long n8  = 0x8070605fc030201LL;   // [1,2,3,-4,5,6,7,8]
    long long n16 = 0xfffc000300020001LL;  // [1,2,3,-4]
    long long n32 = 0xfffffffe00000001LL;  // [1,-2]

    results[6]  = __vreduce_vi8(n8);    // sign-extend -4 (correct, both sides agree)
    results[7]  = __vreduce_vu8(n8);    // zero-extend -- EXPECT simulator mismatch (bug)
    results[8]  = __vreduce_vi16(n16);  // sign-extend -4 (correct, both sides agree)
    results[9]  = __vreduce_vu16(n16);  // zero-extend -- EXPECT simulator mismatch (bug)
    results[10] = __vreduce_vi32(n32);  // sign-extend -2 (correct, both sides agree)
    results[11] = __vreduce_vu32(n32);  // zero-extend -- EXPECT simulator mismatch (bug)

    return 1;
}

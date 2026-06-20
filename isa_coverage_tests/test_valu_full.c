// ISA coverage: $v (VALU add/sub/mul + replicate), vi8/vu8/vi16/vu16/vi32/vu32.
// Checks the FULL packed result register (every element at once), not just
// the low element like the existing test_vadd.c. Signed and unsigned share
// the same expected bit pattern for add/sub/mul (these ops work at the bit
// level -- signedness only affects interpretation when later unpacked, e.g.
// via $vreduce/$dot, not the add/sub/mul computation itself), so each
// width's expected pattern is computed once and checked against both the
// vi* and vu* intrinsic for that width.
//
// Each check writes its computed value into results[] -- see
// test_alu_full.c / golden/golden_gen.py for why.
#define N_RESULTS 24
long long results[N_RESULTS];

long long __vadd_vi8(long long a, long long b);
long long __vsub_vi8(long long a, long long b);
long long __vmul_vi8(long long a, long long b);
long long __vadd_vi8_rep(long long a, long long b);
long long __vadd_vu8(long long a, long long b);
long long __vsub_vu8(long long a, long long b);
long long __vmul_vu8(long long a, long long b);
long long __vadd_vu8_rep(long long a, long long b);

long long __vadd_vi16(long long a, long long b);
long long __vsub_vi16(long long a, long long b);
long long __vmul_vi16(long long a, long long b);
long long __vadd_vi16_rep(long long a, long long b);
long long __vadd_vu16(long long a, long long b);
long long __vsub_vu16(long long a, long long b);
long long __vmul_vu16(long long a, long long b);
long long __vadd_vu16_rep(long long a, long long b);

long long __vadd_vi32(long long a, long long b);
long long __vsub_vi32(long long a, long long b);
long long __vmul_vi32(long long a, long long b);
long long __vadd_vi32_rep(long long a, long long b);
long long __vadd_vu32(long long a, long long b);
long long __vsub_vu32(long long a, long long b);
long long __vmul_vu32(long long a, long long b);
long long __vadd_vu32_rep(long long a, long long b);

int main() {
    // 8-bit: 8 elements a=[1..8], b=[2,2,2,2,2,2,2,2]
    long long a8 = 0x807060504030201LL;
    long long b8 = 0x202020202020202LL;
    results[0] = __vadd_vi8(a8,b8);
    results[1] = __vadd_vu8(a8,b8);
    results[2] = __vsub_vi8(a8,b8);
    results[3] = __vsub_vu8(a8,b8);
    results[4] = __vmul_vi8(a8,b8);
    results[5] = __vmul_vu8(a8,b8);
    results[6] = __vadd_vi8_rep(a8,10LL);
    results[7] = __vadd_vu8_rep(a8,10LL);

    // 16-bit: 4 elements a=[1,2,3,4], b=[2,2,2,2]
    long long a16 = 0x4000300020001LL;
    long long b16 = 0x2000200020002LL;
    results[8]  = __vadd_vi16(a16,b16);
    results[9]  = __vadd_vu16(a16,b16);
    results[10] = __vsub_vi16(a16,b16);
    results[11] = __vsub_vu16(a16,b16);
    results[12] = __vmul_vi16(a16,b16);
    results[13] = __vmul_vu16(a16,b16);
    results[14] = __vadd_vi16_rep(a16,10LL);
    results[15] = __vadd_vu16_rep(a16,10LL);

    // 32-bit: 2 elements a=[1,2], b=[2,2]
    long long a32 = 0x200000001LL;
    long long b32 = 0x200000002LL;
    results[16] = __vadd_vi32(a32,b32);
    results[17] = __vadd_vu32(a32,b32);
    results[18] = __vsub_vi32(a32,b32);
    results[19] = __vsub_vu32(a32,b32);
    results[20] = __vmul_vi32(a32,b32);
    results[21] = __vmul_vu32(a32,b32);
    results[22] = __vadd_vi32_rep(a32,10LL);
    results[23] = __vadd_vu32_rep(a32,10LL);

    return 1;
}

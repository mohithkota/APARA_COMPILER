// ISA coverage: $v (VALU add/sub/mul + replicate), vi8/vu8/vi16/vu16/vi32/vu32.
// Checks the FULL packed result register (every element at once), not just
// the low element like the existing test_vadd.c. Signed and unsigned share
// the same expected bit pattern for add/sub/mul (these ops work at the bit
// level -- signedness only affects interpretation when later unpacked, e.g.
// via $vreduce/$dot, not the add/sub/mul computation itself), so each
// width's expected pattern is computed once and checked against both the
// vi* and vu* intrinsic for that width.
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
    if (__vadd_vi8(a8,b8) != 0xa09080706050403LL) return -1;
    if (__vadd_vu8(a8,b8) != 0xa09080706050403LL) return -2;
    if (__vsub_vi8(a8,b8) != 0x6050403020100ffLL) return -3;
    if (__vsub_vu8(a8,b8) != 0x6050403020100ffLL) return -4;
    if (__vmul_vi8(a8,b8) != 0x100e0c0a08060402LL) return -5;
    if (__vmul_vu8(a8,b8) != 0x100e0c0a08060402LL) return -6;
    if (__vadd_vi8_rep(a8,10LL) != 0x1211100f0e0d0c0bLL) return -7;
    if (__vadd_vu8_rep(a8,10LL) != 0x1211100f0e0d0c0bLL) return -8;

    // 16-bit: 4 elements a=[1,2,3,4], b=[2,2,2,2]
    long long a16 = 0x4000300020001LL;
    long long b16 = 0x2000200020002LL;
    if (__vadd_vi16(a16,b16) != 0x6000500040003LL) return -9;
    if (__vadd_vu16(a16,b16) != 0x6000500040003LL) return -10;
    if (__vsub_vi16(a16,b16) != 0x200010000ffffLL) return -11;
    if (__vsub_vu16(a16,b16) != 0x200010000ffffLL) return -12;
    if (__vmul_vi16(a16,b16) != 0x8000600040002LL) return -13;
    if (__vmul_vu16(a16,b16) != 0x8000600040002LL) return -14;
    if (__vadd_vi16_rep(a16,10LL) != 0xe000d000c000bLL) return -15;
    if (__vadd_vu16_rep(a16,10LL) != 0xe000d000c000bLL) return -16;

    // 32-bit: 2 elements a=[1,2], b=[2,2]
    long long a32 = 0x200000001LL;
    long long b32 = 0x200000002LL;
    if (__vadd_vi32(a32,b32) != 0x400000003LL) return -17;
    if (__vadd_vu32(a32,b32) != 0x400000003LL) return -18;
    if (__vsub_vi32(a32,b32) != 0xffffffffLL) return -19;
    if (__vsub_vu32(a32,b32) != 0xffffffffLL) return -20;
    if (__vmul_vi32(a32,b32) != 0x400000002LL) return -21;
    if (__vmul_vu32(a32,b32) != 0x400000002LL) return -22;
    if (__vadd_vi32_rep(a32,10LL) != 0xc0000000bLL) return -23;
    if (__vadd_vu32_rep(a32,10LL) != 0xc0000000bLL) return -24;

    return 1;
}

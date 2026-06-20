/* test_vadd.c — tests $v + (vector element-wise add/sub/mul)
   $v + rd (type) rs1 rs2
   $v + rd (type) rs1 rs2 $replicate  — rs2 scalar broadcast

   Each check writes the FULL packed result (not just the low element) into
   results[] -- see isa_coverage_tests/test_alu_full.c / test_valu_full.c /
   compiler/STATUS.md 2026-06-20 for why. */
#define N_RESULTS 4
long long results[N_RESULTS];

long long __vadd_vi32(long long a, long long b);
long long __vsub_vi16(long long a, long long b);
long long __vmul_vi8 (long long a, long long b);
long long __vadd_vi32_rep(long long a, long long b);  /* b is scalar, replicated */

int main() {
    /* vi32: two 32-bit signed ints packed in one 64-bit register
       a = [1, 2]  b = [3, 4]  →  add = [4, 6] */
    long long a32 = 1LL | (2LL << 32);
    long long b32 = 3LL | (4LL << 32);
    results[0] = __vadd_vi32(a32, b32);

    /* vi16: four 16-bit ints packed
       a = [10, 20, 30, 40]  b = [1, 2, 3, 4]  → sub = [9, 18, 27, 36] */
    long long a16 = 10LL | (20LL<<16) | (30LL<<32) | (40LL<<48);
    long long b16 =  1LL | ( 2LL<<16) | ( 3LL<<32) | ( 4LL<<48);
    results[1] = __vsub_vi16(a16, b16);

    /* vi8: eight 8-bit ints packed (low 4 elements only, high 4 are zero) */
    long long a8 = 2LL | (3LL<<8) | (4LL<<16) | (5LL<<24);
    long long b8 = 2LL | (2LL<<8) | (2LL<<16) | (2LL<<24);
    results[2] = __vmul_vi8(a8, b8);

    /* replicate: add [1,2] + scalar 10 → [11, 12] */
    results[3] = __vadd_vi32_rep(a32, 10LL);

    return 1;
}

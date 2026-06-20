/* test_pack.c — tests $pack (pack two regs into one)
   $pack rd result_nbits src_nbits rs2
   Takes src_nbits from rs2 and rs2+1, packs into rd as result_nbits field

   Bit order confirmed empirically (STATUS.md 2026-06-20,
   isa_coverage_tests/test_pack_full.c): arg1 lands in the HIGH bits, arg2
   in the LOW bits -- this test's comments originally assumed the
   opposite, which is why its old result (0xdead, not the expected
   0xBEEF) looked like a bug but wasn't one.

   Each check writes its computed value into results[] -- see
   isa_coverage_tests/test_alu_full.c for why. */
#define N_RESULTS 1
long long results[N_RESULTS];

long long __pack(long long a, long long b, int result_nbits, int src_nbits);

int main() {
    long long a = 0xBEEF;   /* lands in the HIGH 16 bits of the result */
    long long b = 0xDEAD;   /* lands in the LOW 16 bits of the result */

    /* Pack into a 32-bit result: result[31:16] = a, result[15:0] = b */
    long long packed = __pack(a, b, 32, 16);   /* expect 0xBEEFDEAD */

    results[0] = packed;

    return 1;
}

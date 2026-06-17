/* test_pack.c — tests $pack (pack two regs into one)
   $pack rd result_nbits src_nbits rs2
   Takes src_nbits from rs2 and rs2+1, packs into rd as result_nbits field */

int g_packed = 0;

long long __pack(long long a, long long b, int result_nbits, int src_nbits);

int main() {
    long long lo = 0xBEEF;   /* lower 16 bits of result */
    long long hi = 0xDEAD;   /* upper 16 bits of result */

    /* Pack 16-bit lo and hi into a 32-bit result:
       result[31:16] = hi[15:0]  result[15:0] = lo[15:0] */
    long long packed = __pack(lo, hi, 32, 16);

    g_packed = (int)packed;

    return (int)(packed & 0xFFFF);   /* low 16 bits = 0xBEEF = 48879 */
}

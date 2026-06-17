/* test_vadd.c — tests $v + (vector element-wise add/sub/mul)
   $v + rd (type) rs1 rs2
   $v + rd (type) rs1 rs2 $replicate  — rs2 scalar broadcast */

int g_add32 = 0;
int g_sub16 = 0;
int g_mul8  = 0;
int g_rep   = 0;

long long __vadd_vi32(long long a, long long b);
long long __vsub_vi16(long long a, long long b);
long long __vmul_vi8 (long long a, long long b);
long long __vadd_vi32_rep(long long a, long long b);  /* b is scalar, replicated */

int main() {
    /* vi32: two 32-bit signed ints packed in one 64-bit register
       a = [1, 2]  b = [3, 4]  →  add = [4, 6] */
    long long a32 = 1LL | (2LL << 32);
    long long b32 = 3LL | (4LL << 32);
    g_add32 = (int)__vadd_vi32(a32, b32);   /* low 32 bits = 4 */

    /* vi16: four 16-bit ints packed
       a = [10, 20, 30, 40]  b = [1, 2, 3, 4]  → sub = [9, 18, 27, 36] */
    long long a16 = 10LL | (20LL<<16) | (30LL<<32) | (40LL<<48);
    long long b16 =  1LL | ( 2LL<<16) | ( 3LL<<32) | ( 4LL<<48);
    g_sub16 = (int)__vsub_vi16(a16, b16);   /* low 16 bits = 9 */

    /* vi8: eight 8-bit ints packed  */
    long long a8 = 2LL | (3LL<<8) | (4LL<<16) | (5LL<<24);
    long long b8 = 2LL | (2LL<<8) | (2LL<<16) | (2LL<<24);
    g_mul8 = (int)__vmul_vi8(a8, b8);       /* low 8 bits = 4 */

    /* replicate: add [1,2] + scalar 10 → [11, 12] */
    g_rep = (int)__vadd_vi32_rep(a32, 10LL);  /* low 32 = 11 */

    return g_add32;   /* expected 4 */
}

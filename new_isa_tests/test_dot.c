/* test_dot.c — tests $dot and $dot $accumulate
   $dot rd (type) rs1 rs2
   rd = (rs1 dot rs2)   or   rd = (rs1 dot rs2) + rd  (accumulate) */

int g_dot    = 0;
int g_dotacc = 0;

long long __dot_vi16(long long a, long long b);
long long __dot_acc_vi16(long long acc, long long a, long long b);

int main() {
    /* vi16: four 16-bit values per register
       a = [1, 2, 3, 4]   b = [1, 2, 3, 4]
       dot = 1*1 + 2*2 + 3*3 + 4*4 = 1+4+9+16 = 30 */
    long long a = 1LL | (2LL<<16) | (3LL<<32) | (4LL<<48);
    long long b = 1LL | (2LL<<16) | (3LL<<32) | (4LL<<48);

    long long d = __dot_vi16(a, b);          /* d = 30 */
    g_dot = (int)d;

    /* accumulate: acc=d(30) + dot(a,b)(30) = 60 */
    long long acc = __dot_acc_vi16(d, a, b);
    g_dotacc = (int)acc;

    return g_dot + g_dotacc;   /* expected 90 = 0x5A */
}

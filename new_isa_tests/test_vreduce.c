/* test_vreduce.c — tests $vreduce (sum all elements of a vector)
   $vreduce rd (type) rs
   rd = sum of all elements in rs interpreted as (type) vector */

int g_r32 = 0;
int g_r16 = 0;
int g_r8  = 0;

long long __vreduce_vi32(long long a);
long long __vreduce_vi16(long long a);
long long __vreduce_vi8 (long long a);

int main() {
    /* vi32: [10, 20] → sum = 30 */
    long long v32 = 10LL | (20LL << 32);
    g_r32 = (int)__vreduce_vi32(v32);    /* expected 30 */

    /* vi16: [1, 2, 3, 4] → sum = 10 */
    long long v16 = 1LL | (2LL<<16) | (3LL<<32) | (4LL<<48);
    g_r16 = (int)__vreduce_vi16(v16);    /* expected 10 */

    /* vi8: [1,2,3,4,5,6,7,8] → sum = 36 */
    long long v8 = 1LL|(2LL<<8)|(3LL<<16)|(4LL<<24)|(5LL<<32)|(6LL<<40)|(7LL<<48)|(8LL<<56);
    g_r8 = (int)__vreduce_vi8(v8);       /* expected 36 */

    return g_r32 + g_r16 + g_r8;   /* expected 76 = 0x4C */
}

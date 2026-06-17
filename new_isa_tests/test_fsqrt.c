/* test_fsqrt.c — tests $fsqrt (floating-point square root)
   $fsqrt rd (type) rs
   Uses __fsqrt_f32 intrinsic (no libm dependency) */

int g_result = 0;

long long __fsqrt_f32(long long x);
long long __fsqrt_f16(long long x);

int main() {
    /* $fsqrt with $f32 type */
    long long x = __fsqrt_f32(9);   /* sqrt of 9 as f32 bits */

    /* $fsqrt with $f16 type */
    long long y = __fsqrt_f16(4);   /* sqrt of 4 as f16 bits */

    g_result = (int)(x + y);
    return (int)x;
}

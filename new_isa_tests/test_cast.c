/* test_cast.c — tests $cast instruction via C type casts */
typedef signed char  int8_t;
typedef short        int16_t;
typedef int          int32_t;
typedef long long    int64_t;

int g_i8  = 0;
int g_i16 = 0;
int g_i32 = 0;

int main() {
    int64_t big = 0x12345678ABCDEFL;

    /* Cast to narrower integer types — should emit $cast instructions */
    int8_t  a = (int8_t)  big;   /* $cast ($i8)  dest ($i64) src */
    int16_t b = (int16_t) big;   /* $cast ($i16) dest ($i64) src */
    int32_t c = (int32_t) big;   /* $cast ($i32) dest ($i64) src */

    g_i8  = a;   /* store narrowed values to globals */
    g_i16 = b;
    g_i32 = c;

    return (int)(a + b + c);
}

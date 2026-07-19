/* d01: every sub-word integer type: arrays, sign/zero extension, arithmetic */
long long results[8];
char         c8[4]  = {-3, 7, -128, 127};
unsigned char u8[4]  = {3, 200, 255, 9};
short        s16[4] = {-300, 500, -32768, 32767};
unsigned short u16[4] = {60000, 12, 65535, 800};
int          i32[4] = {-70000, 12345, -2147483647, 100000};
unsigned int u32[4] = {4000000000u, 77, 123456789u, 5};
int main() {
    long long a = 0, b = 0, c = 0, d = 0, e = 0, f = 0;
    for (int i = 0; i < 4; i++) {
        a += c8[i];  b += u8[i];  c += s16[i];
        d += u16[i]; e += i32[i]; f += u32[i];
    }
    results[0] = a; results[1] = b; results[2] = c;
    results[3] = d; results[4] = e; results[5] = f;
    c8[1] = (char)(a + 100);  u16[2] = (unsigned short)(d + 7);
    results[6] = c8[1] * 2 - u16[2];
    char lc = -5; unsigned char lu = 250;   /* local sub-word stores/loads */
    short ls = -1000; unsigned short lus = 50000;
    results[7] = lc + lu + ls + lus;
    return 1;
}

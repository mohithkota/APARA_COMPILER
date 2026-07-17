long long results[8];
int main(){
    unsigned long long u = 0xF000000000000000ull;
    long long s = (long long)0xF000000000000000ull;   /* negative */
    unsigned int u32 = 0xF0000000u;
    int s32 = (int)0xF0000000;                          /* negative */
    results[0] = u >> 4;        /* logical -> 0x0F00000000000000 */
    results[1] = s >> 4;        /* arithmetic -> 0xFF00000000000000 */
    results[2] = u32 >> 4;      /* logical 32 -> 0x0F000000 */
    results[3] = s32 >> 4;      /* arithmetic 32 -> 0xFFFFFFFFFF000000 (sign-ext) */
    results[4] = u >> 1;        /* 0x7800000000000000 */
    results[5] = s >> 1;        /* 0xF800000000000000 */
    results[6] = (0xFFu) >> 2;  /* small unsigned */
    results[7] = u32 >> 28;     /* 0xF */
    return 1;
}

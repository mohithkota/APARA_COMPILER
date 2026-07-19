/* d05: $v vector ALU — ALL of +,-,* at vi8/vu8/vi16/vu16/vi32/vu32, + $replicate */
long long results[8];
long long __vadd_vi8(long long, long long);  long long __vadd_vu8(long long, long long);
long long __vsub_vi8(long long, long long);  long long __vsub_vu8(long long, long long);
long long __vmul_vi8(long long, long long);  long long __vmul_vu8(long long, long long);
long long __vadd_vi16(long long, long long); long long __vadd_vu16(long long, long long);
long long __vsub_vi16(long long, long long); long long __vsub_vu16(long long, long long);
long long __vmul_vi16(long long, long long); long long __vmul_vu16(long long, long long);
long long __vadd_vi32(long long, long long); long long __vadd_vu32(long long, long long);
long long __vsub_vi32(long long, long long); long long __vsub_vu32(long long, long long);
long long __vmul_vi32(long long, long long); long long __vmul_vu32(long long, long long);
long long __vadd_vu8_rep(long long, long long);
long long ga = 0x0102030405060708LL, gb = 0x1011121314151617LL;
long long g2 = 0x0202020202020202LL, g16 = 0x0002000200020002LL;
long long g32 = 0x0000000200000002LL;
int main() {
    results[0] = __vadd_vi8(ga, gb) ^ __vadd_vu8(ga, ga);
    results[1] = __vsub_vi8(gb, ga) + __vsub_vu8(gb, ga);
    results[2] = __vmul_vi8(ga, g2) ^ __vmul_vu8(ga, g2);
    results[3] = __vadd_vi16(ga, gb) - __vadd_vu16(gb, ga)
               + __vsub_vi16(gb, ga) - __vsub_vu16(gb, ga);
    results[4] = __vmul_vi16(ga, g16) ^ __vmul_vu16(ga, g16);
    results[5] = __vadd_vi32(ga, gb) ^ __vadd_vu32(gb, ga)
               ^ __vsub_vi32(gb, ga) ^ __vsub_vu32(gb, ga);
    results[6] = __vmul_vi32(ga, g32) ^ __vmul_vu32(ga, g32);
    results[7] = __vadd_vu8_rep(ga, 5);
    return 1;
}

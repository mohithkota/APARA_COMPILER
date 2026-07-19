/* d07: $dot and $dot $accumulate at i8/u8/i16/u16 */
long long results[6];
long long __dot_vi8(long long, long long);  long long __dot_vu8(long long, long long);
long long __dot_vi16(long long, long long); long long __dot_vu16(long long, long long);
long long __dot_acc_vi8(long long, long long, long long);
long long __dot_acc_vu16(long long, long long, long long);
long long ga = 0x0102030405060708LL, gb = 0x0201020102010201LL;
int main() {
    results[0] = __dot_vi8(ga, gb);
    results[1] = __dot_vu8(ga, gb);
    results[2] = __dot_vi16(ga, gb);
    results[3] = __dot_vu16(ga, gb);
    results[4] = __dot_acc_vi8(100, ga, gb);
    results[5] = __dot_acc_vu16(7, ga, gb);
    return 1;
}

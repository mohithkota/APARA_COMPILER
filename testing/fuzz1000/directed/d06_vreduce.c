/* d06: $vreduce + and $max at every width */
long long results[8];
long long __vreduce_vi8(long long);  long long __vreduce_vu8(long long);
long long __vreduce_vi16(long long); long long __vreduce_vu16(long long);
long long __vreduce_vi32(long long); long long __vreduce_vu32(long long);
long long __vreduce_max_vi8(long long);  long long __vreduce_max_vu8(long long);
long long __vreduce_max_vi16(long long); long long __vreduce_max_vu16(long long);
long long ga = 0x01f2030405060708LL;
int main() {
    results[0] = __vreduce_vi8(ga);
    results[1] = __vreduce_vu8(ga);
    results[2] = __vreduce_vi16(ga);
    results[3] = __vreduce_vu16(ga);
    results[4] = __vreduce_vi32(ga);
    results[5] = __vreduce_vu32(ga);
    results[6] = __vreduce_max_vi8(ga) + __vreduce_max_vu8(ga);
    results[7] = __vreduce_max_vi16(ga) + __vreduce_max_vu16(ga);
    return 1;
}

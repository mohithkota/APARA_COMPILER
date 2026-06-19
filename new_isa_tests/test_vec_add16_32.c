// $v add for the previously-untested widths vu16, vu32
int main() {
    long long a16 = 0x0001000200030004LL;
    long long b16 = 0x0005000600070008LL;
    long long a32 = 0x0000000300000004LL;
    long long b32 = 0x0000000500000006LL;
    long long r;

    r = __vadd_vu16(a16, b16);  if (r != 0x00060008000a000cLL) return -1;
    r = __vadd_vu32(a32, b32);  if (r != 0x000000080000000aLL) return -2;

    return 1;
}

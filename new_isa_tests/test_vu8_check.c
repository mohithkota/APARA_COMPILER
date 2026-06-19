// Minimal hand-verifiable vu8 check.
// a = 0x0102030405060708 -> bytes 01,02,03,04,05,06,07,08
// b = 0x1010101010101010 -> bytes all 0x10 (16 decimal)
// vu8 add is element-wise per byte: 01+10=11, 02+10=12, ..., 08+10=18, no overflow (range 0..255)
// expected g = 0x1112131415161718
long long g;
int main() {
    long long a = 0x0102030405060708LL;
    long long b = 0x1010101010101010LL;
    g = __vadd_vu8(a, b);
    return 0;
}

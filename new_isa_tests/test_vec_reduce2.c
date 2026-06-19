// $vreduce across i8/i16/i32/u8/u16/u32
int main() {
    long long a8  = 0x0102030405060708LL;
    long long a16 = 0x0001000200030004LL;
    long long a32 = 0x0000000300000004LL;
    long long r;

    r = __vreduce_vi8(a8);    if (r != 36) return -1;
    r = __vreduce_vi16(a16);  if (r != 10) return -2;
    r = __vreduce_vi32(a32);  if (r != 7)  return -3;
    r = __vreduce_vu8(a8);    if (r != 36) return -4;
    r = __vreduce_vu16(a16);  if (r != 10) return -5;
    r = __vreduce_vu32(a32);  if (r != 7)  return -6;

    return 1;
}

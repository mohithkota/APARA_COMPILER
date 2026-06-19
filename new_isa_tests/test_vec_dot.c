// $dot / $dot $accumulate across i8/i16/u8/u16 (ISA doc 5.4: dot only defined for <=16-bit elements)
int main() {
    long long a8  = 0x0102030405060708LL;
    long long b8  = 0x0101010101010101LL;
    long long a16 = 0x0001000200030004LL;
    long long b16 = 0x0005000600070008LL;
    long long r;

    r = __dot_vi8(a8, b8);              if (r != 36)   return -1;
    r = __dot_acc_vi8(100, a8, b8);     if (r != 136)  return -2;
    r = __dot_vi16(a16, b16);           if (r != 70)   return -3;
    r = __dot_acc_vi16(1000, a16, b16); if (r != 1070) return -4;
    r = __dot_vu8(a8, b8);              if (r != 36)   return -5;
    r = __dot_acc_vu8(100, a8, b8);     if (r != 136)  return -6;
    r = __dot_vu16(a16, b16);           if (r != 70)   return -7;
    r = __dot_acc_vu16(1000, a16, b16); if (r != 1070) return -8;

    return 1;
}

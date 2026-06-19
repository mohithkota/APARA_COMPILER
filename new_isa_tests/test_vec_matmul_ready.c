// Comprehensive check: $dot, $dot $accumulate, $vreduce, $v across i8/i16/i32/u8/u16/u32
// (vi4/vu4 skipped per explicit direction -- not used frequently)
// Note: ISA doc 5.4 -- $dot is only defined for element widths <= 16 bits, so no
// vi32/vu32 dot checks (would be testing something the ISA itself doesn't define).
// Returns -N for the first failing check, 1 if everything passes.

int main() {
    long long a8  = 0x0102030405060708LL;   // elements (MSB->LSB): 1,2,3,4,5,6,7,8
    long long b8  = 0x0101010101010101LL;   // all 1s
    long long a16 = 0x0001000200030004LL;   // elements: 1,2,3,4
    long long b16 = 0x0005000600070008LL;   // elements: 5,6,7,8
    long long a32 = 0x0000000300000004LL;   // elements: 3,4
    long long b32 = 0x0000000500000006LL;   // elements: 5,6

    long long r;

    // ---- $dot (sum of element-wise products), widths <=16 only per ISA doc 5.4 ----
    r = __dot_vi8(a8, b8);            if (r != 36)   return -1;   // 1+2+...+8
    r = __dot_acc_vi8(100, a8, b8);   if (r != 136)  return -2;   // 100+36
    r = __dot_vi16(a16, b16);         if (r != 70)   return -3;   // 1*5+2*6+3*7+4*8
    r = __dot_acc_vi16(1000, a16, b16); if (r != 1070) return -4; // 1000+70
    r = __dot_vu8(a8, b8);            if (r != 36)   return -5;
    r = __dot_acc_vu8(100, a8, b8);   if (r != 136)  return -6;
    r = __dot_vu16(a16, b16);         if (r != 70)   return -7;
    r = __dot_acc_vu16(1000, a16, b16); if (r != 1070) return -8;

    // ---- $vreduce (sum of elements), no width restriction in the ISA doc ----
    r = __vreduce_vi8(a8);    if (r != 36) return -9;    // 1+2+...+8
    r = __vreduce_vi16(a16);  if (r != 10) return -10;   // 1+2+3+4
    r = __vreduce_vi32(a32);  if (r != 7)  return -11;   // 3+4
    r = __vreduce_vu8(a8);    if (r != 36) return -12;
    r = __vreduce_vu16(a16);  if (r != 10) return -13;
    r = __vreduce_vu32(a32);  if (r != 7)  return -14;

    // ---- $v add, filling in the only previously-untested widths (vu16, vu32) ----
    // vu16: (1+5)=6,(2+6)=8,(3+7)=10,(4+8)=12 -> 0x00060008000a000c
    r = __vadd_vu16(a16, b16);  if (r != 0x00060008000a000cLL) return -15;
    // vu32: (3+5)=8,(4+6)=10 -> 0x000000080000000a
    r = __vadd_vu32(a32, b32);  if (r != 0x000000080000000aLL) return -16;

    return 1;
}

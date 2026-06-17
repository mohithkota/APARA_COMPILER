/*
 * test_spill.c — forces register spilling via right-nested function calls.
 *
 * ir_gen visits LEFT before RIGHT. For f01()+(f02()+(f03()+...)):
 *   _t1 = f01()    — stays live until outermost +
 *   _t2 = f02()    — stays live until second + (with _t1 still live)
 *   ...
 * By the time f29() is called, _t1.._t28 are ALL live in registers (pool full).
 * Calling f29() triggers the pool-full spill path in _gen_IRCall.
 *
 * Expected: r1 = 1+2+...+30 = 465 = 0x1d1
 */

long long f01(void) { return  1; }
long long f02(void) { return  2; }
long long f03(void) { return  3; }
long long f04(void) { return  4; }
long long f05(void) { return  5; }
long long f06(void) { return  6; }
long long f07(void) { return  7; }
long long f08(void) { return  8; }
long long f09(void) { return  9; }
long long f10(void) { return 10; }
long long f11(void) { return 11; }
long long f12(void) { return 12; }
long long f13(void) { return 13; }
long long f14(void) { return 14; }
long long f15(void) { return 15; }
long long f16(void) { return 16; }
long long f17(void) { return 17; }
long long f18(void) { return 18; }
long long f19(void) { return 19; }
long long f20(void) { return 20; }
long long f21(void) { return 21; }
long long f22(void) { return 22; }
long long f23(void) { return 23; }
long long f24(void) { return 24; }
long long f25(void) { return 25; }
long long f26(void) { return 26; }
long long f27(void) { return 27; }
long long f28(void) { return 28; }
long long f29(void) { return 29; }
long long f30(void) { return 30; }

long long main(void) {
    /*
     * Right-nested: ir_gen emits calls f01..f30 in order, keeping each
     * return value live until all inner calls are done.
     * At f29(): _t1.._t28 are simultaneously live (28 regs full) → SPILL.
     */
    return
        f01() + (f02() + (f03() + (f04() + (f05() +
        (f06() + (f07() + (f08() + (f09() + (f10() +
        (f11() + (f12() + (f13() + (f14() + (f15() +
        (f16() + (f17() + (f18() + (f19() + (f20() +
        (f21() + (f22() + (f23() + (f24() + (f25() +
        (f26() + (f27() + (f28() + (f29() + f30()
        ))))))))))))))))))))))))))));
}

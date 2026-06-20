// Comprehensive scalar ISA test
// Tests: +, -, *, /, %, |, &, ^, <<, >>, ~, unary-
// Tests: ==, !=, >, <, >=, <=
// Tests: &&, ||, !
// Tests: +=, -=, *=, /=, &=, |=, ^=, <<=, >>=
// Tests: ++, --, ternary, if/else, while, for, do-while, switch
// Tests: local vars, global vars, function calls
//
// Each section's result is written into results[] -- see
// isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20 for why.
#define N_RESULTS 11
long long results[N_RESULTS];

int add3(int a, int b, int c) {
    return a + b + c;
}

int max2(int a, int b) {
    if (a > b)
        return a;
    return b;
}

int fact(int n) {
    int r;
    r = 1;
    while (n > 1) {
        r = r * n;
        n = n - 1;
    }
    return r;
}

int main() {
    int a;
    int b;
    int c;
    int r;

    // ── 1. Basic arithmetic ──────────────────────────────────────────
    a = 30;
    b = 7;
    r = a + b;     // 37
    r = r - 4;     // 33
    r = r * 3;     // 99
    r = r / 9;     // 11
    r = r % 3;     // 2
    results[0] = r; // expect 2

    // ── 2. Bitwise ───────────────────────────────────────────────────
    a = 60;        // 0b00111100
    b = 13;        // 0b00001101
    r = a | b;     // 0b00111101 = 61
    r = r & 15;    // 0b00001101 = 13
    r = r ^ 5;     // 0b00001000 = 8
    r = r << 2;    // 32
    r = r >> 1;    // 16
    c = ~r;        // bitwise NOT of 16 = -17 (signed 64-bit)
    results[1] = r; // expect 16
    results[2] = c; // expect -17

    // ── 3. Comparisons (each must produce 1 if true) ─────────────────
    r = 0;
    if (10 == 10) r = r + 1;
    if (10 != 5)  r = r + 1;
    if (10 > 5)   r = r + 1;
    if (5 < 10)   r = r + 1;
    if (10 >= 10) r = r + 1;
    if (5 <= 10)  r = r + 1;
    results[3] = r; // expect 6

    // ── 4. Logical operators ─────────────────────────────────────────
    r = 0;
    if (1 && 1)  r = r + 1;  // true
    if (0 || 1)  r = r + 1;  // true
    if (!0)      r = r + 1;  // true
    if (0 && 1)  r = r + 10; // should NOT run
    if (1 || 0)  r = r + 1;  // true
    results[4] = r; // expect 4

    // ── 5. Compound assignments ──────────────────────────────────────
    r = 100;
    r += 10;    // 110
    r -= 20;    // 90
    r *= 2;     // 180
    r /= 6;     // 30
    r %= 7;     // 2
    r &= 3;     // 2 & 3 = 2
    r |= 5;     // 2 | 5 = 7
    r ^= 4;     // 7 ^ 4 = 3
    r <<= 3;    // 3 << 3 = 24
    r >>= 1;    // 24 >> 1 = 12
    results[5] = r; // expect 12

    // ── 6. Increment/decrement + loops ──────────────────────────────
    // while loop
    r = 0;
    a = 5;
    while (a > 0) {
        r = r + a;
        a--;
    }
    // r = 5+4+3+2+1 = 15

    // for loop
    b = 0;
    int i;
    for (i = 1; i <= 5; i++) {
        b += i;
    }
    // b = 15

    // do-while
    c = 0;
    i = 0;
    do {
        c++;
        i++;
    } while (i < 3);
    // c = 3

    results[6] = r + b + c; // 15 + 15 + 3 = 33

    // ── 7. Ternary ───────────────────────────────────────────────────
    a = 10;
    b = 20;
    r = (a > b) ? a : b;
    results[7] = r; // expect 20

    // ── 8. Switch ────────────────────────────────────────────────────
    r = 0;
    a = 2;
    switch (a) {
        case 1: r = 100; break;
        case 2: r = 200; break;
        case 3: r = 300; break;
        default: r = 999; break;
    }
    results[8] = r; // expect 200

    // ── 9. Function calls ────────────────────────────────────────────
    r = add3(10, 20, 30);         // 60
    r = r + max2(15, 25);         // 85
    r = r + fact(5);              // 85 + 120 = 205
    results[9] = r;               // expect 205

    // ── 10. Final aggregate (matches the original test's return value) ──
    results[10] = results[0] + results[3] + results[4]; // 2 + 6 + 4 = 12

    return 1;
}

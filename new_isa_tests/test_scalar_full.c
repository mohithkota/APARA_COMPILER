// Comprehensive scalar ISA test
// Tests: +, -, *, /, %, |, &, ^, <<, >>, ~, unary-
// Tests: ==, !=, >, <, >=, <=
// Tests: &&, ||, !
// Tests: +=, -=, *=, /=, &=, |=, ^=, <<=, >>=
// Tests: ++, --, ternary, if/else, while, for, do-while, switch
// Tests: local vars, global vars, function calls

int g_arith;
int g_bitwise;
int g_compare;
int g_logical;
int g_compound;
int g_loop;
int g_ternary;
int g_switch_res;
int g_func_res;

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
    g_arith = r;   // expect 2

    // ── 2. Bitwise ───────────────────────────────────────────────────
    a = 60;        // 0b00111100
    b = 13;        // 0b00001101
    r = a | b;     // 0b00111101 = 61
    r = r & 15;    // 0b00001101 = 13
    r = r ^ 5;     // 0b00001000 = 8
    r = r << 2;    // 32
    r = r >> 1;    // 16
    c = ~r;        // bitwise NOT of 16 = -17 (signed 64-bit)
    g_bitwise = r; // expect 16

    // ── 3. Comparisons (each must produce 1 if true) ─────────────────
    r = 0;
    if (10 == 10) r = r + 1;
    if (10 != 5)  r = r + 1;
    if (10 > 5)   r = r + 1;
    if (5 < 10)   r = r + 1;
    if (10 >= 10) r = r + 1;
    if (5 <= 10)  r = r + 1;
    g_compare = r; // expect 6

    // ── 4. Logical operators ─────────────────────────────────────────
    r = 0;
    if (1 && 1)  r = r + 1;  // true
    if (0 || 1)  r = r + 1;  // true
    if (!0)      r = r + 1;  // true
    if (0 && 1)  r = r + 10; // should NOT run
    if (1 || 0)  r = r + 1;  // true
    g_logical = r; // expect 4

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
    g_compound = r; // expect 12

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

    g_loop = r + b + c; // 15 + 15 + 3 = 33

    // ── 7. Ternary ───────────────────────────────────────────────────
    a = 10;
    b = 20;
    r = (a > b) ? a : b;
    g_ternary = r; // expect 20

    // ── 8. Switch ────────────────────────────────────────────────────
    r = 0;
    a = 2;
    switch (a) {
        case 1: r = 100; break;
        case 2: r = 200; break;
        case 3: r = 300; break;
        default: r = 999; break;
    }
    g_switch_res = r; // expect 200

    // ── 9. Function calls ────────────────────────────────────────────
    r = add3(10, 20, 30);         // 60
    r = r + max2(15, 25);         // 85
    r = r + fact(5);              // 85 + 120 = 205
    g_func_res = r;               // expect 205

    return g_arith + g_compare + g_logical; // 2 + 6 + 4 = 12
}

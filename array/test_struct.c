/*
 * test_struct.c — struct member access tests for APARA compiler
 *
 * Returns 0 on full success; negative on the first failing sub-test.
 *
 * Tests:
 *  1. Local struct: write + read fields
 *  2. Global struct: write + read fields
 *  3. Struct initializer
 *  4. Pointer-to-struct (->)
 *  5. Struct passed as pointer argument and read inside function
 *  6. Struct passed as pointer argument and written inside function
 *  7. Nested struct (struct containing struct)
 *  8. Chained field access a.b.c
 */

struct Point {
    long long x;
    long long y;
};

struct Line {
    struct Point start;
    struct Point end;
    long long id;
};

struct Point gPt;

long long get_x(struct Point *p) {
    return p->x;
}

void set_xy(struct Point *p, long long x, long long y) {
    p->x = x;
    p->y = y;
}

long long line_len_sq(struct Line *ln) {
    long long dx;
    long long dy;
    dx = ln->end.x - ln->start.x;
    dy = ln->end.y - ln->start.y;
    return dx * dx + dy * dy;
}

long long main() {

    /* ── Test 1: local struct write + read ─────────────────────────────────── */
    struct Point p1;
    p1.x = 10;
    p1.y = 20;
    if (p1.x != 10) return -1;
    if (p1.y != 20) return -1;

    /* ── Test 2: global struct write + read ────────────────────────────────── */
    gPt.x = 55;
    gPt.y = 77;
    if (gPt.x != 55) return -2;
    if (gPt.y != 77) return -2;

    /* ── Test 3: struct initializer ─────────────────────────────────────────── */
    struct Point p2;
    p2.x = 3;
    p2.y = 4;
    if (p2.x != 3) return -3;
    if (p2.y != 4) return -3;

    /* ── Test 4: pointer-to-struct (->) ────────────────────────────────────── */
    struct Point *pp;
    pp = &p1;
    if (pp->x != 10) return -4;
    if (pp->y != 20) return -4;
    pp->x = 99;
    if (p1.x != 99) return -4;

    /* ── Test 5: struct read via pointer param ─────────────────────────────── */
    p1.x = 42;
    long long v;
    v = get_x(&p1);
    if (v != 42) return -5;

    /* ── Test 6: struct write via pointer param ────────────────────────────── */
    set_xy(&p2, 100, 200);
    if (p2.x != 100) return -6;
    if (p2.y != 200) return -6;

    /* ── Test 7: nested struct (Line contains two Points) ───────────────────── */
    struct Line ln;
    ln.start.x = 0;
    ln.start.y = 0;
    ln.end.x   = 3;
    ln.end.y   = 4;
    ln.id      = 7;
    if (ln.start.x != 0) return -7;
    if (ln.end.x   != 3) return -7;
    if (ln.end.y   != 4) return -7;
    if (ln.id      != 7) return -7;

    /* ── Test 8: function computing with nested struct via pointer ──────────── */
    long long lsq;
    lsq = line_len_sq(&ln);   /* (3-0)^2 + (4-0)^2 = 9 + 16 = 25 */
    if (lsq != 25) return -8;

    return 0;
}

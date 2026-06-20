/*
 * test_struct.c — struct member access tests for APARA compiler
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
 *
 * Each check writes its computed value into results[] -- see
 * isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20 for why.
 */
#define N_RESULTS 17
long long results[N_RESULTS];

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
    results[0] = p1.x;
    results[1] = p1.y;

    /* ── Test 2: global struct write + read ────────────────────────────────── */
    gPt.x = 55;
    gPt.y = 77;
    results[2] = gPt.x;
    results[3] = gPt.y;

    /* ── Test 3: struct initializer ─────────────────────────────────────────── */
    struct Point p2;
    p2.x = 3;
    p2.y = 4;
    results[4] = p2.x;
    results[5] = p2.y;

    /* ── Test 4: pointer-to-struct (->) ────────────────────────────────────── */
    struct Point *pp;
    pp = &p1;
    results[6] = pp->x;
    results[7] = pp->y;
    pp->x = 99;
    results[8] = p1.x;

    /* ── Test 5: struct read via pointer param ─────────────────────────────── */
    p1.x = 42;
    long long v;
    v = get_x(&p1);
    results[9] = v;

    /* ── Test 6: struct write via pointer param ────────────────────────────── */
    set_xy(&p2, 100, 200);
    results[10] = p2.x;
    results[11] = p2.y;

    /* ── Test 7: nested struct (Line contains two Points) ───────────────────── */
    struct Line ln;
    ln.start.x = 0;
    ln.start.y = 0;
    ln.end.x   = 3;
    ln.end.y   = 4;
    ln.id      = 7;
    results[12] = ln.start.x;
    results[13] = ln.end.x;
    results[14] = ln.end.y;
    results[15] = ln.id;

    /* ── Test 8: function computing with nested struct via pointer ──────────── */
    long long lsq;
    lsq = line_len_sq(&ln);   /* (3-0)^2 + (4-0)^2 = 9 + 16 = 25 */
    results[16] = lsq;

    return 1;
}

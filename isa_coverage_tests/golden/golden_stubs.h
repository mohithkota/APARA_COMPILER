/* golden_stubs.h -- faithful plain-C reference implementations of the
 * APARA intrinsics used in the scalar test files, for compiling each
 * test natively with gcc to get an independent ground truth.
 *
 * "No bias" rule: every function here is derived directly from the ISA
 * specification (isa.txt) and/or empirically CONFIRMED hardware behavior
 * recorded in compiler/STATUS.md -- never by reading ir_gen.py/codegen.py
 * and mirroring their logic. If the compiler and this file independently
 * arrive at the same answer, that's real corroboration, not an echo.
 *
 * Source for each:
 *   __nand/__nor/__xnor : isa.txt ALU section (~&/~|/~^ are bitwise NOT
 *                         of AND/OR/XOR, full 64-bit, 2's complement).
 *   __cmov_*            : isa.txt CMOV section -- "if (rs2 cond 0) rd :=
 *                         rs1 end if" (rd otherwise keeps its current
 *                         value, modeled here as the explicit falseval
 *                         parameter the test harness already uses).
 *   __pack              : isa.txt PACK section + empirically confirmed
 *                         bit order (STATUS.md 2026-06-20, test_pack_full.c):
 *                         arg1 lands in the HIGH bits, arg2 in the LOW
 *                         bits when 2 source registers are needed;
 *                         degenerate (arg2 ignored) when total/word == 1.
 *   __slice              : isa.txt SLICE section -- rd := rs2[hi:lo],
 *                         zero-extended.
 *   __ld128/256,
 *   __st128/256          : isa.txt LOAD/STORE section -- u128/u256 are
 *                         just a contiguous 2/4-word memory copy; no
 *                         element-wise math, unambiguous either way.
 *   __vadd/__vsub/__vmul  : isa.txt VALU section -- element-wise op across
 *                         64/nbits packed elements; replicate broadcasts
 *                         rs2's low nbits to every element. Signed vs
 *                         unsigned does NOT change add/sub/mul at the bit
 *                         level (confirmed empirically, STATUS.md
 *                         2026-06-20/test_valu_full.c) -- only element
 *                         width matters for wraparound, so vi and vu
 *                         widths share one generic helper.
 *   __vreduce_*          : isa.txt VREDUCE section -- sum of all elements,
 *                         sign-extended for signed types, zero-extended
 *                         for unsigned. NOTE: the real simulator has a
 *                         CONFIRMED bug (McodeOperations.cpp
 *                         __vreduce_operation__) where unsigned vreduce
 *                         sign-extends instead of zero-extending --
 *                         deliberately NOT replicated here. This file
 *                         encodes the architecturally-correct answer per
 *                         the user's explicit "no bias" instruction; the
 *                         resulting mismatch against the simulator's
 *                         actual (buggy) output is expected and
 *                         documented in test_vreduce_full.c, not a flaw
 *                         in this golden model.
 *   __dot_*, __dot_acc_* : isa.txt DOT section -- <rd> := <rs1>.<rs2> +
 *                         (accumulate ? <rd> : 0), per-element sign- or
 *                         zero-extend before multiply, based on the
 *                         source element type (confirmed correct on real
 *                         hardware, STATUS.md 2026-06-20/test_dot_full.c
 *                         -- unlike vreduce, dot has no known bug here).
 */
#ifndef GOLDEN_STUBS_H
#define GOLDEN_STUBS_H

/* vu8_t is one of compiler.py's _FAKE_TYPEDEFS (an opt-in marker requesting
 * natural/packed array stride on the APARA side -- a DMEM-layout detail
 * with no bearing on native memory layout or values). For gcc, it is
 * simply unsigned char; test sources never redefine it themselves so
 * there's no conflict either way. */
typedef unsigned char vu8_t;

/* ---- scalar ALU: nand/nor/xnor (no C operator reaches these) ---- */
long long __nand(long long a, long long b) { return ~(a & b); }
long long __nor (long long a, long long b) { return ~(a | b); }
long long __xnor(long long a, long long b) { return ~(a ^ b); }

/* ---- CMOV: dest = falseval unless (check cond 0), then dest = trueval ---- */
int __cmov_gt(int check, int trueval, int falseval) { return (check >  0) ? trueval : falseval; }
int __cmov_lt(int check, int trueval, int falseval) { return (check <  0) ? trueval : falseval; }
int __cmov_eq(int check, int trueval, int falseval) { return (check == 0) ? trueval : falseval; }
int __cmov_ne(int check, int trueval, int falseval) { return (check != 0) ? trueval : falseval; }
int __cmov_ge(int check, int trueval, int falseval) { return (check >= 0) ? trueval : falseval; }
int __cmov_le(int check, int trueval, int falseval) { return (check <= 0) ? trueval : falseval; }

/* ---- PACK: arg1 -> high word_nbits bits, arg2 -> low word_nbits bits;
 * degenerate (arg2 ignored) when only one source register is needed. ---- */
long long __pack(long long a, long long b, int result_nbits, int word_nbits) {
    int n_src = result_nbits / word_nbits;
    if (n_src <= 1) return a;
    unsigned long long mask = (word_nbits >= 64) ? ~0ULL : (((unsigned long long)1 << word_nbits) - 1);
    unsigned long long hi = ((unsigned long long)a & mask) << word_nbits;
    unsigned long long lo = (unsigned long long)b & mask;
    return (long long)(hi | lo);
}

/* ---- SLICE: rd := rs2[hindex:lindex], zero-extended ---- */
long long __slice(long long x, int hindex, int lindex) {
    int width = hindex - lindex + 1;
    unsigned long long mask = (width >= 64) ? ~0ULL : (((unsigned long long)1 << width) - 1);
    return (long long)(((unsigned long long)x >> lindex) & mask);
}

/* ---- wide load/store: plain contiguous memory copy ---- */
void __ld128(long long *dst, long long *src) { dst[0] = src[0]; dst[1] = src[1]; }
void __ld256(long long *dst, long long *src) { dst[0] = src[0]; dst[1] = src[1]; dst[2] = src[2]; dst[3] = src[3]; }
void __st128(long long *dst, long long *src) { dst[0] = src[0]; dst[1] = src[1]; }
void __st256(long long *dst, long long *src) { dst[0] = src[0]; dst[1] = src[1]; dst[2] = src[2]; dst[3] = src[3]; }

/* ---- VALU: element-wise add(0)/sub(1)/mul(2) across 64/nbits elements ---- */
static long long __v_generic(long long a, long long b, int nbits, int op, int replicate) {
    int n = 64 / nbits;
    unsigned long long mask = (nbits >= 64) ? ~0ULL : (((unsigned long long)1 << nbits) - 1);
    unsigned long long result = 0;
    for (int i = 0; i < n; i++) {
        unsigned long long ea = ((unsigned long long)a >> (i * nbits)) & mask;
        unsigned long long eb = replicate ? ((unsigned long long)b & mask)
                                           : (((unsigned long long)b >> (i * nbits)) & mask);
        unsigned long long r;
        switch (op) {
            case 0:  r = ea + eb; break;
            case 1:  r = ea - eb; break;
            default: r = ea * eb; break;
        }
        result |= (r & mask) << (i * nbits);
    }
    return (long long) result;
}
long long __vadd_vi8 (long long a, long long b) { return __v_generic(a, b, 8,  0, 0); }
long long __vadd_vu8 (long long a, long long b) { return __v_generic(a, b, 8,  0, 0); }
long long __vsub_vi8 (long long a, long long b) { return __v_generic(a, b, 8,  1, 0); }
long long __vsub_vu8 (long long a, long long b) { return __v_generic(a, b, 8,  1, 0); }
long long __vmul_vi8 (long long a, long long b) { return __v_generic(a, b, 8,  2, 0); }
long long __vmul_vu8 (long long a, long long b) { return __v_generic(a, b, 8,  2, 0); }
long long __vadd_vi8_rep(long long a, long long b) { return __v_generic(a, b, 8, 0, 1); }
long long __vadd_vu8_rep(long long a, long long b) { return __v_generic(a, b, 8, 0, 1); }

long long __vadd_vi16(long long a, long long b) { return __v_generic(a, b, 16, 0, 0); }
long long __vadd_vu16(long long a, long long b) { return __v_generic(a, b, 16, 0, 0); }
long long __vsub_vi16(long long a, long long b) { return __v_generic(a, b, 16, 1, 0); }
long long __vsub_vu16(long long a, long long b) { return __v_generic(a, b, 16, 1, 0); }
long long __vmul_vi16(long long a, long long b) { return __v_generic(a, b, 16, 2, 0); }
long long __vmul_vu16(long long a, long long b) { return __v_generic(a, b, 16, 2, 0); }
long long __vadd_vi16_rep(long long a, long long b) { return __v_generic(a, b, 16, 0, 1); }
long long __vadd_vu16_rep(long long a, long long b) { return __v_generic(a, b, 16, 0, 1); }

long long __vadd_vi32(long long a, long long b) { return __v_generic(a, b, 32, 0, 0); }
long long __vadd_vu32(long long a, long long b) { return __v_generic(a, b, 32, 0, 0); }
long long __vsub_vi32(long long a, long long b) { return __v_generic(a, b, 32, 1, 0); }
long long __vsub_vu32(long long a, long long b) { return __v_generic(a, b, 32, 1, 0); }
long long __vmul_vi32(long long a, long long b) { return __v_generic(a, b, 32, 2, 0); }
long long __vmul_vu32(long long a, long long b) { return __v_generic(a, b, 32, 2, 0); }
long long __vadd_vi32_rep(long long a, long long b) { return __v_generic(a, b, 32, 0, 1); }
long long __vadd_vu32_rep(long long a, long long b) { return __v_generic(a, b, 32, 0, 1); }

/* ---- VREDUCE: sum of all elements, sign/zero-extended per element type.
 * Architecturally correct -- see header note on the confirmed simulator
 * bug for the unsigned cases. ---- */
static long long __vreduce_generic(long long a, int nbits, int is_unsigned) {
    int n = 64 / nbits;
    unsigned long long mask = (nbits >= 64) ? ~0ULL : (((unsigned long long)1 << nbits) - 1);
    long long sum = 0;
    for (int i = 0; i < n; i++) {
        unsigned long long e = ((unsigned long long)a >> (i * nbits)) & mask;
        long long ev;
        if (is_unsigned) {
            ev = (long long) e;
        } else {
            int shift = 64 - nbits;
            ev = ((long long)(e << shift)) >> shift;
        }
        sum += ev;
    }
    return sum;
}
long long __vreduce_vi8 (long long a) { return __vreduce_generic(a, 8,  0); }
long long __vreduce_vu8 (long long a) { return __vreduce_generic(a, 8,  1); }
long long __vreduce_vi16(long long a) { return __vreduce_generic(a, 16, 0); }
long long __vreduce_vu16(long long a) { return __vreduce_generic(a, 16, 1); }
long long __vreduce_vi32(long long a) { return __vreduce_generic(a, 32, 0); }
long long __vreduce_vu32(long long a) { return __vreduce_generic(a, 32, 1); }

/* ---- DOT: sum of element-wise products, sign/zero-extended per element
 * type before multiply, optionally accumulating into a prior result. ---- */
static long long __dot_generic(long long a, long long b, int nbits, int is_unsigned,
                                long long acc, int accumulate) {
    int n = 64 / nbits;
    unsigned long long mask = (nbits >= 64) ? ~0ULL : (((unsigned long long)1 << nbits) - 1);
    long long sum = accumulate ? acc : 0;
    for (int i = 0; i < n; i++) {
        unsigned long long ea = ((unsigned long long)a >> (i * nbits)) & mask;
        unsigned long long eb = ((unsigned long long)b >> (i * nbits)) & mask;
        long long va, vb;
        if (is_unsigned) {
            va = (long long) ea;
            vb = (long long) eb;
        } else {
            int shift = 64 - nbits;
            va = ((long long)(ea << shift)) >> shift;
            vb = ((long long)(eb << shift)) >> shift;
        }
        sum += va * vb;
    }
    return sum;
}
long long __dot_vi8 (long long a, long long b) { return __dot_generic(a, b, 8,  0, 0, 0); }
long long __dot_vu8 (long long a, long long b) { return __dot_generic(a, b, 8,  1, 0, 0); }
long long __dot_vi16(long long a, long long b) { return __dot_generic(a, b, 16, 0, 0, 0); }
long long __dot_vu16(long long a, long long b) { return __dot_generic(a, b, 16, 1, 0, 0); }
long long __dot_acc_vi8 (long long acc, long long a, long long b) { return __dot_generic(a, b, 8,  0, acc, 1); }
long long __dot_acc_vu8 (long long acc, long long a, long long b) { return __dot_generic(a, b, 8,  1, acc, 1); }
long long __dot_acc_vi16(long long acc, long long a, long long b) { return __dot_generic(a, b, 16, 0, acc, 1); }
long long __dot_acc_vu16(long long acc, long long a, long long b) { return __dot_generic(a, b, 16, 1, acc, 1); }

/* ---- 128-bit-wide fused dot: 16 unsigned-8-bit elements, direct from memory ---- */
long long __dot128_direct_vu8(unsigned char *a, unsigned char *b) {
    long long sum = 0;
    for (int i = 0; i < 16; i++) sum += (long long)a[i] * (long long)b[i];
    return sum;
}

#endif

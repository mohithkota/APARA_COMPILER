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
 */
#ifndef GOLDEN_STUBS_H
#define GOLDEN_STUBS_H

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

#endif

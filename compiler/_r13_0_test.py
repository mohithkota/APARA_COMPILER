#!/usr/bin/env python3
"""
_r13_0_test.py -- R13.0 Phases 1-4: structural matcher tests for
`matmul_access` (the generic `invariant_base + IV*elem_bytes` representation).

ANALYSIS ONLY. Nothing here enables production lowering; every test runs the
matcher over freshly built IR and asserts ACCEPT/REJECT plus the specific
predicate responsible.

The purpose is to prove the matcher is STRUCTURAL:

  * positive variants that differ only in spelling -- temporary names, variable
    names, expression grouping, constant placement, pre-hoisted vs inline
    invariant base, signedness -- must classify IDENTICALLY;
  * negative controls must be rejected by the PREDICATE THAT OWNS the defect,
    not merely rejected somehow.

Run: python3 _r13_0_test.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matmul_access
import matmul_probe

FAILURES = []
COUNT = [0]


# ── helpers ────────────────────────────────────────────────────────────────────

def probe(src):
    """Build IR for a C snippet and return the first matmul loop's probe."""
    fd, path = tempfile.mkstemp(suffix='.c')
    os.write(fd, src.encode())
    os.close(fd)
    try:
        ps = matmul_probe.probe_source(path)
    finally:
        os.unlink(path)
    return ps[0] if ps else None


def check(name, cond, detail=''):
    COUNT[0] += 1
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: {detail}")
        FAILURES.append(name)


def expect_accept(name, src):
    p = probe(src)
    if p is None:
        check(name, False, "detector did not classify any loop as 'matmul'")
        return None
    check(name, p.form.ok, f"rejected: {p.form.reason}")
    return p


def expect_reject(name, src, predicate):
    """Reject, AND by the named predicate -- so a test cannot pass for the
    wrong reason (e.g. a typo making the kernel undetectable)."""
    p = probe(src)
    if p is None:
        check(f"{name} [{predicate}]", False,
              "no matmul loop detected at all (test would pass vacuously)")
        return None
    if p.form.ok:
        check(f"{name} [{predicate}]", False, "unexpectedly ACCEPTED")
        return p
    got = (p.form.reason or '').split(':')[0]
    check(f"{name} [{predicate}]", got == predicate,
          f"rejected by {p.form.reason!r}, expected predicate {predicate}")
    return p


# ── source templates ───────────────────────────────────────────────────────────

def mm(T='vu8_t', K=16, M=16, N=16, body=None, decl=None, init=None):
    """Canonical transposed-B matmul; `body` overrides the inner statement."""
    nn = max(M * K, N * K)
    body = body or "s += A[i * {K} + k] * Bt[j * {K} + k];".format(K=K)
    decl = decl or f"{T} A[{nn}], Bt[{nn}]; int i, j, k, s;"
    init = init or (f"for (i = 0; i < {nn}; i++) "
                    f"{{ A[i] = ({T})((i + 1) & 0x3f); "
                    f"Bt[i] = ({T})((i + 3) & 0x1f); }}")
    return f"""
long long results[4];
int main(void) {{
    {decl}
    {init}
    for (i = 0; i < {M}; i++)
        for (j = 0; j < {N}; j++) {{
            s = 0;
            for (k = 0; k < {K}; k++) {body}
            results[0] = s;
        }}
    return 0;
}}
"""


# ── Phase 4a: positive structural variants ─────────────────────────────────────

def test_positive_variants():
    print("\n[positive] equivalent spellings must classify identically")

    base = expect_accept("canonical A[i*K+k] * Bt[j*K+k]", mm())

    # different variable AND temporary names: nothing may key on identifiers
    expect_accept("renamed variables (p/q/r/acc, X/Y)", mm(
        decl="vu8_t X[256], Y[256]; int p, q, r, acc;",
        init=("for (p = 0; p < 256; p++) "
              "{ X[p] = (vu8_t)((p + 1) & 0x3f); Y[p] = (vu8_t)((p + 3) & 0x1f); }"),
        body="acc += X[p * 16 + r] * Y[q * 16 + r];").replace(
        "for (i = 0; i < 16; i++)", "for (p = 0; p < 16; p++)").replace(
        "for (j = 0; j < 16; j++)", "for (q = 0; q < 16; q++)").replace(
        "s = 0;", "acc = 0;").replace(
        "for (k = 0; k < 16; k++)", "for (r = 0; r < 16; r++)").replace(
        "results[0] = s;", "results[0] = acc;"))

    # operand order swapped: multiplication is commutative
    expect_accept("swapped multiplicand order",
                  mm(body="s += Bt[j * 16 + k] * A[i * 16 + k];"))

    # expression grouping / explicit parenthesisation
    expect_accept("parenthesised index expressions",
                  mm(body="s += A[(i * 16) + k] * Bt[(j * 16) + k];"))

    # constant placement: k + i*K instead of i*K + k
    expect_accept("commuted index terms (k + i*K)",
                  mm(body="s += A[k + i * 16] * Bt[k + j * 16];"))

    # signed and unsigned 8/16-bit are all supported by $dot
    for T in ('vi8_t', 'vu16_t', 'vi16_t'):
        expect_accept(f"datatype {T}", mm(T=T))

    # size independence: the matcher must not care about the logical dimensions
    for K in (8, 24, 32):
        expect_accept(f"K={K}", mm(K=K, M=K, N=K))

    # rectangular
    expect_accept("rectangular 32x16x16", mm(M=32, K=16, N=16))

    return base


def test_spelling_independence(base):
    """Two source spellings of the same operation must produce the same
    structural facts, not merely both be accepted."""
    print("\n[anti-bias] identical structure => identical classification")
    if base is None:
        check("spelling independence", False, "baseline missing")
        return
    other = probe(mm(body="s += A[(i * 16) + k] * Bt[(j * 16) + k];"))
    if other is None or not other.form.ok:
        check("spelling independence", False, "variant not accepted")
        return
    a, b = base.form, other.form
    same = (a.lanes == b.lanes and a.trip == b.trip and a.chunks == b.chunks
            and a.remainder == b.remainder and a.vtype == b.vtype
            and [c[0] for c in a.checks] == [c[0] for c in b.checks]
            and [c[1] for c in a.checks] == [c[1] for c in b.checks])
    check("spelling independence (same facts, same predicate results)", same,
          f"{a!r} vs {b!r}")

    coeffs_a = sorted((x.coeff, x.elem_bytes) for x in a.accesses)
    coeffs_b = sorted((x.coeff, x.elem_bytes) for x in b.accesses)
    check("spelling independence (same resolved accesses)",
          coeffs_a == coeffs_b, f"{coeffs_a} vs {coeffs_b}")


# ── Phase 4b: negative controls, each owned by a specific predicate ────────────

def test_known_limitations():
    """Behaviours that are NOT what we would want, asserted so they cannot
    change silently. These are reported as limitations, not passes."""
    print("\n[limitation] alignment provability is spelling-dependent")
    src = mm(decl="vu8_t A[256], Bt[256]; int i, j, k, s, ra, rb;",
             body="s += A[ra + k] * Bt[rb + k];").replace(
        "s = 0;", "ra = i * 16; rb = j * 16; s = 0;")
    p = probe(src)
    check("pre-hoisted base is still detected as matmul",
          p is not None and p.kind == 'matmul',
          f"kind={p.kind if p else None}")
    if p is None:
        return
    # Structurally identical to the inline form, and p1-p5 agree...
    upto = {n: ok for n, ok, _ in p.form.checks}
    check("pre-hoisted: p2/p3/p4/p5 all still pass",
          all(upto.get(n) for n in ('p2_same_k_iv', 'p3_coeff_is_elem_bytes',
                                    'p4_invariant_base',
                                    'p5_base_stable_across_vector_loop')),
          str(p.form.checks))
    # ...but divisibility of the base is lost through the stack slot, so
    # alignment cannot be PROVEN and legality (not R13) rejects it.
    check("pre-hoisted: rejected for unprovable alignment, by legality",
          (not p.form.ok) and 'unaligned-packed-access' in (p.form.reason or ''),
          f"reason={p.form.reason!r}")
    check("pre-hoisted: the lost fact is base divisibility (sym_div == 1)",
          all(a.sym_div == 1 for a in p.form.accesses),
          str([(a.const_off, a.sym_div) for a in p.form.accesses]))


def test_negative_controls():
    print("\n[negative] each defect rejected by the predicate that owns it")

    # B NOT transposed: the K coefficient becomes row_len*elem_bytes.
    expect_reject("non-contiguous K (un-transposed B)",
                  mm(body="s += A[i * 16 + k] * Bt[k * 16 + j];"), 'p3')

    # explicit wrong stride: k*2 walks every other element
    expect_reject("wrong IV coefficient (k*2)",
                  mm(body="s += A[i * 16 + k * 2] * Bt[j * 16 + k * 2];"), 'p3')

    # runtime-varying stride: a gather through an index array
    expect_reject("runtime-varying stride (gather)", mm(
        decl="vu8_t A[256], Bt[256]; int idx[256]; int i, j, k, s;",
        init=("for (i = 0; i < 256; i++) { A[i] = (vu8_t)((i + 1) & 0x3f);"
              " Bt[i] = (vu8_t)((i + 3) & 0x1f); idx[i] = (i * 7) & 0xff; }"),
        body="s += A[i * 16 + idx[k]] * Bt[j * 16 + k];"), 'p6')

    # base not invariant: the row base is recomputed inside the loop.
    # OWNED BY p6, NOT p4: a varying base makes the whole offset unresolvable,
    # so the resolver reports UNKNOWN before p4's invariance question is even
    # asked. p4 is retained as defence in depth; see its docstring.
    expect_reject("non-invariant base (base written in loop)", mm(
        decl="vu8_t A[256], Bt[256]; int i, j, k, s, rb;",
        body="{ rb = rb + 16; s += A[i * 16 + k] * Bt[rb + k]; }").replace(
        "s = 0;", "rb = j * 16; s = 0;"), 'p6')

    # unsupported datatype: the ISA has no 32-bit $dot
    expect_reject("unsupported datatype (vi32 / no-32bit-dot)",
                  mm(T='vi32_t'), 'p9')
    expect_reject("unsupported datatype (vu32 / no-32bit-dot)",
                  mm(T='vu32_t'), 'p9')

    # too small to fill one vector word: 4 elements of 8-bit vs 8 lanes
    expect_reject("trip smaller than lanes (4x4 vu8)",
                  mm(T='vu8_t', K=4, M=4, N=4), 'p7')

    # not a dot at all: single load accumulation is a sum-reduction, and the
    # detector must NOT hand it to the matmul path
    p = probe(mm(body="s += A[i * 16 + k];"))
    check("sum-reduction is not claimed as matmul",
          p is None or p.kind != 'matmul',
          f"classified as {p.kind if p else None}")


# ── Phase 3: convertibility ────────────────────────────────────────────────────

def test_plan_parity(base):
    print("\n[phase 3] accepted form converts to the dot planner's shape")
    if base is None or not base.form.ok:
        check("plan parity", False, "no accepted baseline")
        return
    par = matmul_access.dot_plan_parity(base.form)
    check("parity computed", par.ok, repr(par))
    check("only array addressing is a genuine extension",
          set(par.extensions) == {'array_slots', 'peel'},
          f"extensions={par.extensions}")
    check("every other LoweringPlan field is supplied or already shared",
          len(par.supplied) + len(par.shared) + len(par.extensions)
          == len(matmul_access.DOT_PLAN_FIELDS),
          f"{len(par.supplied)}+{len(par.shared)}+{len(par.extensions)}")
    # PHASE 5: the existing planner now ACCEPTS it (this is the whole point).
    # Before Phase 5 this asserted the rejection 'array-bases-not-extracted'.
    check("existing dot planner now ACCEPTS the matmul form",
          base.dot_plan_reason is None,
          f"dot planner said {base.dot_plan_reason!r}")


# ── predicate coverage ─────────────────────────────────────────────────────────

def test_predicate_coverage():
    print("\n[coverage] all ten predicates exist and are exercised")
    check("ten predicates declared", len(matmul_access.PREDICATES) == 10,
          str(matmul_access.PREDICATES))
    for name in matmul_access.PREDICATES:
        check(f"predicate {name} is a real function",
              callable(getattr(matmul_access, name, None)))
    p = probe(mm())
    names = [c[0] for c in p.form.checks] if p else []
    check("an accepted kernel evaluates all ten", len(names) == 10, str(names))


# ── Phase 5: production lowering ───────────────────────────────────────────────

def test_phase5_both_multiplicands():
    """THE correctness guard for the `need` change.

    `vector_lowering.py` used to compute `need = 2 if kind in ('dot-product',)
    else 1`, which would have handed matmul ONE array slot and silently dropped
    the second multiplicand -- a wrong-answer bug. `need` is now derived from
    the reduction structure, and this test pins that both slots survive."""
    print("\n[phase 5] both matmul multiplicands reach the lowering")
    p = probe(mm())
    if p is None or p.dot_plan is None or not p.dot_plan.ok:
        check("matmul plans", False,
              f"plan reason={p.dot_plan_reason if p else 'no probe'}")
        return
    pl = p.dot_plan
    check("exactly two array slots", len(pl.array_slots) == 2,
          f"array_slots={pl.array_slots}")
    check("the two slots are DISTINCT arrays",
          len(set(pl.array_slots)) == 2, f"array_slots={pl.array_slots}")
    check("both carry an invariant row base",
          len(pl.array_offs) == 2 and all(o is not None for o in pl.array_offs),
          f"array_offs={pl.array_offs}")
    check("both row-base address temps materialised",
          len(pl.array_addr) == 2 and all(a is not None for a in pl.array_addr),
          f"array_addr={pl.array_addr}")
    check("a row-base prologue was emitted", len(pl.array_base_pre) > 0,
          f"{len(pl.array_base_pre)} instrs")


def test_phase5_existing_kinds_unchanged():
    """The shared planner must treat the pre-R13 kinds exactly as before:
    two operands for a dot product, one for a sum reduction, and NO invariant
    base on either (their offsets are bare IV terms)."""
    print("\n[phase 5] existing dot-product / sum-reduction plans unchanged")
    dotp = """
long long results[2];
int main(void){ vi16_t a[64], b[64]; int i, s;
  for(i=0;i<64;i++){ a[i]=(vi16_t)(i&7); b[i]=(vi16_t)((i+1)&7); }
  s=0; for(i=0;i<64;i++) s += a[i]*b[i];
  results[0]=s; return 0; }
"""
    redu = """
long long results[2];
int main(void){ vi16_t a[64]; int i, s;
  for(i=0;i<64;i++) a[i]=(vi16_t)(i&7);
  s=0; for(i=0;i<64;i++) s += a[i];
  results[0]=s; return 0; }
"""
    for tag, src, want in (('dot-product', dotp, 2), ('sum-reduction', redu, 1)):
        fd, path = tempfile.mkstemp(suffix='.c'); os.write(fd, src.encode()); os.close(fd)
        try:
            ps = matmul_probe.probe_source(path, kinds=None)
        finally:
            os.unlink(path)
        sel = [x for x in ps if x.kind == tag and x.dot_plan is not None
               and x.dot_plan.ok]
        if not sel:
            check(f"{tag} still plans", False, f"kinds seen: {[x.kind for x in ps]}")
            continue
        pl = sel[0].dot_plan
        check(f"{tag}: {want} array slot(s), as before",
              len(pl.array_slots) == want, f"array_slots={pl.array_slots}")
        check(f"{tag}: no invariant base (bare IV form preserved)",
              all(o is None for o in pl.array_offs), f"array_offs={pl.array_offs}")
        check(f"{tag}: no row-base prologue emitted",
              not pl.array_base_pre, f"{len(pl.array_base_pre)} instrs")


def test_phase5_dot_is_emitted():
    """MANDATORY: a positive case must emit real $dot in the mcode. A simulator
    PASS with a scalar fallback is NOT acceptance."""
    print("\n[phase 5] $dot actually reaches the emitted mcode")
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    cases = [('vu8_t', 16, 8), ('vi16_t', 16, 4), ('vi8_t', 24, 8)]
    for T, N, lanes in cases:
        src = mm(T=T, K=N, M=N, N=N)
        fd, path = tempfile.mkstemp(suffix='.c'); os.write(fd, src.encode()); os.close(fd)
        out = path[:-2] + '.mcode'
        try:
            subprocess.run([sys.executable, os.path.join(here, 'compiler.py'),
                            '--preprocess', path, '-o', out],
                           capture_output=True, text=True, timeout=1800)
            txt = open(out, errors='replace').read() if os.path.exists(out) else ''
        finally:
            for f in (path, out):
                if os.path.exists(f):
                    os.unlink(f)
        n = txt.count('$dot')
        check(f"{T} {N}x{N}: $dot emitted", n > 0, "zero $dot in mcode")
        check(f"{T} {N}x{N}: one $dot per chunk ({N // lanes})",
              n == N // lanes, f"found {n}, expected {N // lanes}")


def main():
    print("=" * 74)
    print(" R13.0 Phases 1-4 -- structural matcher tests (analysis only)")
    print("=" * 74)
    base = test_positive_variants()
    test_spelling_independence(base)
    test_known_limitations()
    test_negative_controls()
    test_plan_parity(base)
    test_predicate_coverage()
    test_phase5_both_multiplicands()
    test_phase5_existing_kinds_unchanged()
    test_phase5_dot_is_emitted()
    print("\n" + "=" * 74)
    if FAILURES:
        print(f" RESULT: FAIL -- {len(FAILURES)} of {COUNT[0]} checks failed")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print(f" RESULT: PASS -- {COUNT[0]}/{COUNT[0]} checks")
    return 0


if __name__ == '__main__':
    sys.exit(main())

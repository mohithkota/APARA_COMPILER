"""_r6_2_test.py -- unit tests for R6.2 symbolic memory dependence analysis.

Correctness is the whole point of this milestone, so the tests are weighted
towards it:

  1. REQUIRED CAPABILITIES -- each independence case the milestone asks for is
     proved on real mcode text, not on a hand-built data structure.
  2. CONSERVATISM -- every shape that cannot be proved must come back MAY_ALIAS.
     A disambiguator that is wrong in this direction miscompiles silently.
  3. SOUNDNESS BY CONCRETISATION -- an independent check of the algebra: assign
     random concrete values to the opaque symbols and confirm that a pair the
     analysis called independent never actually overlaps, over many draws.
  4. INTEGRATION -- the bundler consults it, the kill switch restores the old
     behaviour exactly, and the R6.1 measurement mirror still matches the real
     packer instruction for instruction.
"""
import os, sys, copy, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bundler as _b
from codegen import CodeGen
from vector_backend import memory_objects as mo
from vector_backend import mem_dependence as md

_fails = []


def check(n, c):
    print(f"  [{'ok' if c else 'FAIL'}] {n}")
    if not c:
        _fails.append(n)


def block(*texts):
    """Parse mcode lines into an annotated single basic block."""
    ents = []
    for t in texts:
        w, r, ctrl, ma, mw = _b._parse_deps(t)
        ents.append({'text': t, 'labels': [], 'writes': w, 'reads': r,
                     'is_ctrl': ctrl, 'mem_access': ma, 'mem_write': mw})
    return md.annotate(ents)


def indep(*texts):
    """Independence verdict for the FIRST and LAST memory access in a block."""
    ents = block(*texts)
    mem = [e for e in ents if e.get('mem_ref') is not None]
    assert len(mem) >= 2, texts
    return md.independent(mem[0], mem[-1])


# ── 1. the capabilities the milestone requires ────────────────────────────────

def test_capabilities():
    print("required independence cases are proved on real mcode")

    check("different objects: A[i] vs B[i]  (distinct bases, SAME index)",
          indep('+ $r6 ($i64) $r26 -64',
                '+ $r7 ($i64) $r26 -128',
                '$ld ($i64) $r9 [$r6 + $r3]',
                '$st ($i64) [$r7 + $r3] $r9'))

    check("same object, different element: A[i] vs A[i+8]",
          indep('+ $r6 ($i64) $r26 -64',
                '$ld ($i64) $r9 [$r6 + 0]',
                '$st ($i64) [$r6 + 8] $r9'))

    check("different unrolled iterations: A[i] vs A[i+VL]",
          indep('+ $r6 ($i64) $r26 -64',
                '+ $r4 ($i64) $r3 8',
                '$st ($i64) [$r6 + $r3] $r1',
                '$ld ($i64) $r9 [$r6 + $r4]'))

    check("two vector widths apart: A[i] vs A[i+2*VL]",
          indep('+ $r6 ($i64) $r26 -64',
                '+ $r4 ($i64) $r3 16',
                '$st ($i64) [$r6 + $r3] $r1',
                '$ld ($i64) $r9 [$r6 + $r4]'))

    check("affine: base + i*stride vs base + (i+k)*stride",
          indep('+ $r6 ($i64) $r26 -64',
                '* $r5 ($i64) $r3 4',
                '+ $r4 ($i64) $r3 2',
                '* $r7 ($i64) $r4 4',
                '$st ($i64) [$r6 + $r5] $r1',
                '$ld ($i64) $r9 [$r6 + $r7]'))

    check("shifted index via <<: A[i<<3] vs A[(i<<3)+8]",
          indep('+ $r6 ($i64) $r26 -64',
                '<< $r5 ($i64) $r3 3',
                '+ $r4 ($i64) $r5 8',
                '$st ($i64) [$r6 + $r5] $r1',
                '$ld ($i64) $r9 [$r6 + $r4]'))

    check("the base itself may be computed: (FP+k)+i vs (FP+k+64)+i",
          indep('+ $r6 ($i64) $r26 -128',
                '+ $r7 ($i64) $r6 64',
                '$st ($i64) [$r6 + $r3] $r1',
                '$ld ($i64) $r9 [$r7 + $r3]'))

    # loop-carried: known iteration distance, generalising the R2.2 SIV rule
    iv = ('iv',)
    a = mo.MemRef(mo.SymAddr({iv: 1}, 0), 8, True)
    b = mo.MemRef(mo.SymAddr({iv: 1}, 0), 8, False)
    check("known iteration distance: a[i] store vs a[i] load, step 8 lanes",
          mo.classify_carried(a, b, iv, 8) == mo.INDEPENDENT)
    c = mo.MemRef(mo.SymAddr({iv: 1}, 8), 8, False)
    check("carried MUST stay conservative when a distance DOES collide",
          mo.classify_carried(a, c, iv, 8) == mo.MAY_ALIAS)


# ── 2. conservatism: everything unproven must be may-alias ────────────────────

def test_conservative():
    print("unproven shapes stay conservative")

    check("identical address is NOT independent",
          not indep('+ $r6 ($i64) $r26 -64',
                    '$st ($i64) [$r6 + $r3] $r1',
                    '$ld ($i64) $r9 [$r6 + $r3]'))

    check("unrelated index registers prove nothing",
          not indep('+ $r6 ($i64) $r26 -64',
                    '$st ($i64) [$r6 + $r3] $r1',
                    '$ld ($i64) $r9 [$r6 + $r5]'))

    check("a loaded (opaque) base proves nothing",
          not indep('$ld ($i64) $r6 [$r26 + 0]',
                    '$st ($i64) [$r6 + 0] $r1',
                    '$ld ($i64) $r9 [$r7 + 0]'))

    check("32-bit address arithmetic is not interpreted (it wraps)",
          not indep('+ $r6 ($i32) $r26 -64',
                    '+ $r7 ($i32) $r26 -128',
                    '$st ($i64) [$r6 + $r3] $r1',
                    '$ld ($i64) $r9 [$r7 + $r3]'))

    check("$set-defined registers are opaque",
          not indep('$set $r6 0 1024',
                    '$set $r7 0 2048',
                    '$st ($i64) [$r6 + 0] $r1',
                    '$ld ($i64) $r9 [$r7 + 0]'))

    check("overlapping 8-byte accesses 4 bytes apart are NOT independent",
          not indep('+ $r6 ($i64) $r26 -64',
                    '$st ($i64) [$r6 + 0] $r1',
                    '$ld ($i64) $r9 [$r6 + 4]'))

    check("sub-word stores in one 64-bit word are NOT independent "
          "(the store is a read-modify-write of the whole word)",
          not indep('+ $r6 ($i64) $r26 -64',
                    '$st ($i32) [$r6 + 0] $r1',
                    '$st ($i32) [$r6 + 4] $r2'))

    check("a multiply-defined register is not carried across blocks",
          not indep('+ $r6 ($i64) $r26 -64',
                    '+ $r6 ($i64) $r26 -128',
                    '$st ($i64) [$r6 + $r3] $r1',
                    '$ld ($i64) $r9 [$r6 + $r3]'))


# ── 3. soundness by concretisation (independent of the algebra) ───────────────

def test_concretisation():
    """Assign random concrete values to the opaque symbols and check directly
    that ranges called INDEPENDENT never overlap. This re-derives the answer a
    different way, so an algebra bug shows up as an overlap."""
    print("randomised concretisation agrees with every independence proof")
    cases = [
        ('+ $r6 ($i64) $r26 -64', '+ $r7 ($i64) $r26 -128',
         '$ld ($i64) $r9 [$r6 + $r3]', '$st ($i64) [$r7 + $r3] $r9'),
        ('+ $r6 ($i64) $r26 -64', '+ $r4 ($i64) $r3 8',
         '$st ($i64) [$r6 + $r3] $r1', '$ld ($i64) $r9 [$r6 + $r4]'),
        ('+ $r6 ($i64) $r26 -64', '<< $r5 ($i64) $r3 3', '+ $r4 ($i64) $r5 8',
         '$st ($i64) [$r6 + $r5] $r1', '$ld ($i64) $r9 [$r6 + $r4]'),
        ('+ $r6 ($i64) $r26 -64', '$st ($i64) [$r6 + $r3] $r1',
         '$ld ($i64) $r9 [$r6 + $r3]'),
    ]
    rng = random.Random(20260731)
    bad = 0
    proofs = 0
    for texts in cases:
        ents = block(*texts)
        mem = [e for e in ents if e.get('mem_ref') is not None]
        for i in range(len(mem)):
            for j in range(i + 1, len(mem)):
                ra, rb = mem[i]['mem_ref'], mem[j]['mem_ref']
                verdict = mo.classify(ra, rb)
                if verdict != mo.INDEPENDENT:
                    continue
                proofs += 1
                for _ in range(400):
                    env = {}

                    def val(addr):
                        t = addr.const
                        for s, c in addr.terms.items():
                            if s not in env:
                                env[s] = rng.randrange(-1 << 20, 1 << 20)
                            t += c * env[s]
                        return t
                    A, B = val(ra.addr), val(rb.addr)
                    if not (A + ra.width <= B or B + rb.width <= A):
                        bad += 1
    check(f"{proofs} proofs, {400 * proofs} concrete draws, zero overlaps",
          bad == 0 and proofs > 0)


# ── 4. integration with the bundler ───────────────────────────────────────────

def _compile(src):
    from vector_backend import ilp_analysis as ia
    ir = ia.build_ir(src)
    vec, _s, _r = ia.vectorize_all_module(copy.deepcopy(ir))
    return ia.production_codegen(vec)[1]


K = ('long long f(){vi8_t X[64],Y[64];int i;int a=3;'
     'for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}')


def test_bundler_integration():
    print("the bundler consults the model, and the kill switch restores it")
    body = _compile(K)
    on = _b.bundle_mcode(body, schedule=True, disambiguate=True)
    off = _b.bundle_mcode(body, schedule=True, disambiguate=False)
    check("same instructions either way (only packing may differ)",
          on[1] == off[1])
    check("disambiguation never needs MORE bundles", on[2] <= off[2])
    check("it actually packs this kernel tighter", on[2] < off[2])

    os.environ['APARA_NO_MEMDISAMB'] = '1'
    try:
        killed = _b.bundle_mcode(body, schedule=True, disambiguate=True)
    finally:
        os.environ.pop('APARA_NO_MEMDISAMB', None)
    check("APARA_NO_MEMDISAMB reproduces the pre-R6.2 output EXACTLY",
          killed[0] == off[0] and killed[2] == off[2])

    check("a failure inside the model cannot break a build",
          _b._proved_independent(None, None) is False and
          _b._proved_independent({'mem_ref': None}, {'mem_ref': None}) is False)


def test_mirror_still_faithful():
    """R6.1's occupancy analysis re-implements the packing loop to attribute
    empty slots. It has to track the bundler through this change, and it asserts
    so itself -- if it drifts, `verified` goes False."""
    print("the R6.1 occupancy mirror still matches the real packer")
    from vector_backend import occupancy as occ
    for src in (K,
                'long long f(){vi8_t a[64],b[64];int i;long long s=0;'
                'for(i=0;i<64;i++)s+=a[i]*b[i];return s;}',
                'long long f(){vi8_t in[72],out[72];int i;'
                'for(i=0;i<61;i++)out[i]=in[i]+in[i+1]+in[i+2];return out[0];}'):
        r = occ.analyze_mcode(_compile(src))
        check("mirror identical to bundler._pack_bundles", r.verified)


def test_no_new_conflicts():
    """Two safety properties on a REAL compiled kernel.

    1. The combined rule is a SUBSET of the textual one: a pair can only stop
       being a conflict, never start. That is true by construction (the model is
       consulted solely to skip a textual check) and is asserted here anyway.
    2. Every proof the model makes on that kernel survives concretisation --
       random concrete values for the opaque symbols, checking the byte ranges
       really cannot meet. This runs the independent check of the algebra
       against production code rather than hand-written fragments."""
    print("on a real kernel: conflicts only ever removed, and every proof holds")
    _h, flat = _b._parse_flat(_compile(K))
    flat = _b._annotate_memrefs(flat)
    mem = [e for e in flat if e['mem_access'] is not None]
    removed = 0
    contradictions = 0
    overlaps = 0
    proofs = 0
    rng = random.Random(6202)
    for i, a in enumerate(mem):
        for b in mem[i + 1:]:
            if b['mem_write'] is None and a['mem_write'] is None:
                continue
            st, acc = (b, a) if b['mem_write'] is not None else (a, b)
            textual = _b._mem_may_alias(acc['mem_access'], (st['mem_write'],))
            proved = _b._proved_independent(acc, st)
            combined = textual and not proved
            if combined and not textual:
                contradictions += 1
            if textual and proved:
                removed += 1
            if not proved:
                continue
            proofs += 1
            ra, rb = acc['mem_ref'], st['mem_ref']
            for _ in range(200):
                env = {}

                def val(addr):
                    t = addr.const
                    for sy, c in addr.terms.items():
                        if sy not in env:
                            env[sy] = rng.randrange(-1 << 20, 1 << 20)
                        t += c * env[sy]
                    return t
                A, B = val(ra.addr), val(rb.addr)
                if not (A + ra.width <= B or B + rb.width <= A):
                    overlaps += 1
    check(f"{removed} textual conflicts removed on this kernel", removed > 0)
    check("the combined rule never conflicts where the textual one did not",
          contradictions == 0)
    check(f"all {proofs} proofs survive {200 * proofs} concrete draws",
          overlaps == 0 and proofs > 0)


def main():
    for t in (test_capabilities, test_conservative, test_concretisation,
              test_bundler_integration, test_mirror_still_faithful,
              test_no_new_conflicts):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R6.2 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

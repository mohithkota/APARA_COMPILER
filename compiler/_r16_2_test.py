#!/usr/bin/env python3
"""
_r16_2_test.py -- R16.2: vector lowering materializes an array base
polymorphically, so a kernel reads the same whether its arrays live on the
stack or at a fixed DMEM address.

Before R16.2 the lowering could only emit `IRLoadAddr(slot)`, i.e. FP + offset.
A global array has no stack slot at all -- `IRGlobalLoad` carries its DMEM
address directly and never produces a base temp -- so every global-array kernel
fell out of vectorization silently and ran scalar.

These tests assert the STRUCTURAL property (the base form follows the array's
storage, nothing else), not a benchmark: the same source vectorizes in both
storage classes, the legality rules that rejected a kernel before still reject
it, and the R14.2 / R14.8 base-sharing passes still compose on the global form.

Run: python3 _r16_2_test.py      (needs APARA_TOOLS)
"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import ArrayBase, emit_array_base, IRLoadAddr, IRGlobalAddrOf, Temp

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FAIL, N = [], [0]

#: a load whose base is a register and whose displacement is an immediate
LD_BASE = re.compile(r'\$ld \(\$\w+\) \$r\d+ \[(\$r\d+) \+ -?\d+\]')
ST_BASE = re.compile(r'\$st \(\$\w+\) \[(\$r\d+) \+ -?\d+\]')


def check(name, cond, detail=''):
    N[0] += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ('' if cond else f": {detail}"))
    if not cond:
        FAIL.append(name)


# ── source generators ──────────────────────────────────────────────────────────

def matmul(T='vu8_t', n=16, jt=4, storage='global', names=('A', 'Bt'),
           k_coeff=1, k_stride_var=False, b_row=None, pad=1):
    """A J-tiled dot-product matmul, emitted with the arrays either as globals
    or as `main`-local stack arrays.  Only STORAGE changes between the two.

    `pad` over-allocates the operand arrays.  The negative controls deliberately
    use a non-unit k stride, which walks past `n*n`; without the padding the C
    itself would be out of bounds and the run would fail for a reason that has
    nothing to do with the vectorization decision under test."""
    A, B = names
    nn = n * n
    na = nn * pad
    ts = list(range(jt))
    dec = ", ".join(f"s{t}" for t in ts)
    acc = " ".join(f"s{t}=0;" for t in ts)
    kx = 'k*st' if k_stride_var else (f'k*{k_coeff}' if k_coeff != 1 else 'k')
    brow = b_row if b_row is not None else f'(j+{{t}})*{n}'
    body = "\n".join(
        f"        s{t} += {A}[i*{n}+{kx}] * {B}[{brow.format(t=t)}+{kx}];" for t in ts)
    sto = "\n".join(f"      results[i*{n}+j+{t}] = (long long)s{t};" for t in ts)
    arrays = f"{T} {A}[{na}], {B}[{na}];"
    decl_g = arrays if storage == 'global' else ''
    decl_l = arrays if storage == 'local' else ''
    stv = "int st = 1; if (results[0] == 12345) st = 2;\n  " if k_stride_var else ""
    return f"""
{decl_g}
long long results[{nn}];
int main(void) {{
  {decl_l}
  int i, j, k, {dec};
  {stv}for (i=0;i<{na};i++) {{ {A}[i]=({T})((i*7+3)%17); {B}[i]=({T})((i*11+5)%19); }}
  for (i=0;i<{n};i++)
    for (j=0;j<{n};j+={jt}) {{
      {acc}
      for (k=0;k<{n};k++) {{
{body}
      }}
{sto}
    }}
  return 0; }}
"""


def build(src, env=None):
    d = tempfile.mkdtemp()
    open(os.path.join(d, 'm.c'), 'w').write(src)
    e = dict(os.environ, **(env or {}))
    r = subprocess.run(['bash', os.path.join(REPO, 'apara-cc'),
                        os.path.join(d, 'm.c'), '--run'],
                       capture_output=True, text=True, env=e, timeout=900)
    out = r.stdout + r.stderr
    mc, lg = os.path.join(d, 'm', 'm.mcode'), os.path.join(d, 'm', 'm.log')
    txt = open(mc, errors='replace').read() if os.path.exists(mc) else ''
    ticks = checks = errs = 0
    if os.path.exists(lg):
        L = open(lg, errors='replace').read()
        m = re.search(r'Stopped after (\d+) ticks', L)
        ticks = int(m.group(1)) if m else 0
        checks = L.count('PostCondition')
        errs = len(re.findall(r'^Error', L, re.M))
    return dict(mcode=txt, ticks=ticks, checks=checks, errs=errs,
                passed='== PASS' in out, dots=txt.count('$dot'))


def kernel_block(txt):
    """The labelled block that contains the `$dot`s."""
    blocks, cur = {}, None
    for line in txt.splitlines():
        m = re.match(r'^([A-Za-z_]\w*):', line)
        if m:
            cur = m.group(1)
            blocks[cur] = []
        elif cur:
            blocks[cur].append(line)
    for lbl, body in blocks.items():
        if any('$dot' in l for l in body):
            return "\n".join(body)
    return ''


# ── unit level: the base abstraction itself ────────────────────────────────────

def test_arraybase_unit():
    print("\n[unit] ArrayBase emits the right instruction for each storage class")
    t = Temp('_x')
    s = emit_array_base(t, ArrayBase.stack(-24))
    g = emit_array_base(t, ArrayBase.glob(0x400))
    check("stack base emits IRLoadAddr", type(s).__name__ == 'IRLoadAddr',
          type(s).__name__)
    check("stack base keeps the FP offset", getattr(s, 'fp_offset', None) == -24,
          repr(getattr(s, 'fp_offset', None)))
    check("global base emits IRGlobalAddrOf", type(g).__name__ == 'IRGlobalAddrOf',
          type(g).__name__)
    check("global base keeps the DMEM address",
          getattr(g, 'dmem_addr', None) == 0x400,
          repr(getattr(g, 'dmem_addr', None)))
    # back-compatibility: a bare int still means a stack slot, so every
    # pre-R16.2 caller and every dict keyed by a slot keeps working.
    b = emit_array_base(t, -24)
    check("a bare int is still accepted as a stack slot",
          type(b).__name__ == 'IRLoadAddr' and b.fp_offset == -24, repr(b))
    check("ArrayBase.stack(x) == x compares equal to the bare slot",
          ArrayBase.stack(-24) == -24, 'not equal')
    check("stack and global bases never compare equal",
          ArrayBase.stack(-24) != ArrayBase.glob(-24), 'compared equal')


# ── Phase 4: positive -- globals vectorize ─────────────────────────────────────

def test_globals_vectorize():
    print("\n[positive] a global-array kernel vectorizes, for every supported marker")
    for T in ('vu8_t', 'vi8_t', 'vu16_t', 'vi16_t'):
        r = build(matmul(T=T, storage='global'))
        check(f"{T} global: emits $dot", r['dots'] > 0, f"dots={r['dots']}")
        check(f"{T} global: correct (256 checks, 0 errors)",
              r['passed'] and r['checks'] == 256 and r['errs'] == 0,
              f"passed={r['passed']} checks={r['checks']} errs={r['errs']}")


def test_storage_class_is_not_a_legality_input():
    """The SAME kernel must reach the SAME vectorization decision whether its
    arrays are global or stack-local.  Storage is a lowering detail."""
    print("\n[positive] storage class does not change the decision")
    g = build(matmul(storage='global'))
    l = build(matmul(storage='local'))
    check("global and stack-local both vectorize",
          g['dots'] > 0 and l['dots'] > 0, f"global={g['dots']} local={l['dots']}")
    check("both emit the same number of $dot",
          g['dots'] == l['dots'], f"{g['dots']} vs {l['dots']}")
    check("both correct", g['passed'] and l['passed'] and
          g['errs'] == 0 and l['errs'] == 0, '')


def test_scales_with_tile():
    print("\n[positive] the global form scales with the tile, like the stack form")
    seen = {}
    for jt in (1, 2, 4):
        r = build(matmul(storage='global', jt=jt))
        seen[jt] = r['dots']
        check(f"J_TILE={jt} global: $dot emitted", r['dots'] > 0, f"{r['dots']}")
        check(f"J_TILE={jt} global: correct",
              r['passed'] and r['errs'] == 0 and r['checks'] == 256, '')
    check("$dot count grows with the tile (one reduction per accumulator)",
          seen[1] < seen[2] < seen[4], repr(seen))


def test_anti_bias():
    print("\n[anti-bias] renaming the arrays does not change the decision")
    a = build(matmul(storage='global'))
    b = build(matmul(storage='global', names=('Xmat', 'Ymat')))
    check("renamed globals: same $dot count", a['dots'] == b['dots'],
          f"{a['dots']} vs {b['dots']}")
    check("renamed globals: same ticks", a['ticks'] == b['ticks'],
          f"{a['ticks']} vs {b['ticks']}")
    check("renamed globals: correct", b['passed'] and b['errs'] == 0, '')


# ── Phase 5: negative -- legality is unchanged ─────────────────────────────────

def test_negative_controls():
    """Each of these was already rejected when the arrays were stack-local.
    Moving them to globals must NOT make them vectorizable -- R16.2 changed how
    a base is MATERIALIZED, not what is legal."""
    print("\n[negative] global storage does not weaken any legality rule")
    cases = [
        ("non-contiguous k (stride 2)", dict(storage='global', k_coeff=2, pad=4)),
        ("wrong IV coefficient (stride 3)", dict(storage='global', k_coeff=3, pad=4)),
        ("runtime-varying stride", dict(storage='global', k_stride_var=True)),
        ("non-affine B row (k-dependent)",
         dict(storage='global', b_row='(j+{t})*16+k*16', pad=4)),
    ]
    for name, kw in cases:
        r = build(matmul(**kw))
        check(f"{name}: NOT vectorized", r['dots'] == 0, f"dots={r['dots']}")
        check(f"{name}: still correct", r['passed'] and r['errs'] == 0,
              f"passed={r['passed']} errs={r['errs']}")


def test_unsupported_datatype():
    """The ISA has no 32-bit `$dot`.  A plain `int` matrix must stay scalar even
    as a global."""
    print("\n[negative] an unsupported element width stays scalar")
    r = build(matmul(T='int', storage='global'))
    check("int global: NOT vectorized", r['dots'] == 0, f"dots={r['dots']}")
    check("int global: still correct", r['passed'] and r['errs'] == 0, '')


# ── Phase 7: composition with R14.2 and R14.8 ─────────────────────────────────

def test_composes_with_base_sharing():
    """R14.2 shares one address between reductions; R14.8 groups the result
    stores.  Both were written against a stack base and must still fire when
    the base is a global."""
    print("\n[composition] R14.2 / R14.8 base sharing still fires on globals")
    r = build(matmul(storage='global', jt=4))
    k = kernel_block(r['mcode'])
    ld_bases = set(LD_BASE.findall(k))
    n_lds = len(LD_BASE.findall(k))
    check("R14.2: every kernel load reads through ONE shared base register",
          n_lds > 1 and len(ld_bases) == 1,
          f"{n_lds} loads over {len(ld_bases)} bases")
    off = build(matmul(storage='global', jt=4),
                {'APARA_NO_STORE_BASE_SHARE': '1'})
    disp = sum(1 for m in ST_BASE.finditer(r['mcode']))
    disp_off = sum(1 for m in ST_BASE.finditer(off['mcode']))
    check("R14.8: the pass changes the global build (kill switch is live)",
          disp != disp_off or r['ticks'] != off['ticks'],
          f"on={disp}/{r['ticks']} off={disp_off}/{off['ticks']}")
    check("R14.8 disabled: still correct", off['passed'] and off['errs'] == 0, '')


def main():
    print("=" * 74)
    print(" R16.2 -- generic global array-base support for vector lowering")
    print("=" * 74)
    if not os.environ.get('APARA_TOOLS'):
        print("  FAIL APARA_TOOLS available: set APARA_TOOLS to run this")
        return 1
    test_arraybase_unit()
    test_globals_vectorize()
    test_storage_class_is_not_a_legality_input()
    test_scales_with_tile()
    test_anti_bias()
    test_negative_controls()
    test_unsupported_datatype()
    test_composes_with_base_sharing()
    print("\n" + "=" * 74)
    if FAIL:
        print(f" RESULT: FAIL -- {len(FAIL)} of {N[0]} checks")
        for f in FAIL:
            print(f"   - {f}")
        return 1
    print(f" RESULT: PASS -- {N[0]}/{N[0]} checks")
    return 0


if __name__ == '__main__':
    sys.exit(main())

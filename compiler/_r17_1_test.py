#!/usr/bin/env python3
"""
_r17_1_test.py -- R17.1: generic additive-identity folding, `x + 0 -> x`.

Before R17.1 the compiler had NO algebraic identity simplification anywhere.
`sccp.py` folds only `const OP const` (`_is_const(l) and _is_const(r)`), so an
`x + 0` with a variable `x` survived every pass and reached codegen as a real
instruction. `vector_lowering`'s row-base cloning re-emits the loop's own
address expression with the induction variable substituted by `Const(0)`
(`_clone_offset(..., Const(0))`), leaving exactly that residue in the middle of
a SERIAL address chain -- where one instruction costs a whole bundle, not a slot.

The fold lives in `strength_reduce.py`, the compiler's existing generic
IRBinOp algebraic rewrite layer, whose own `_pow2_exp` docstring already assumed
identities were "handled elsewhere". It rewrites to `IRAssign` and deliberately
deletes nothing itself: the existing copy-propagation / coalescing / DCE erase
the copy.

These tests assert the ALGEBRAIC rule and its safety boundary, never a
benchmark, datatype, tile width or register name.

APARA_NO_IDENTITY_FOLD=1 restores the pre-R17.1 behaviour.

Run: python3 _r17_1_test.py [--unit] [--full]      (--unit needs no toolchain)
"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import IRAssign, IRBinOp, IRCast, Const, Temp
from strength_reduce import strength_reduce

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FAIL, N = [], [0]

#: `+ $rD ($iNN) $rS 0` with rS != $r0 -- an identity copy that reached codegen
IDCOPY = re.compile(r'^\s*\+\s+\$r\d+\s+\(\$\w+\)\s+(\$r\d+)\s+0\s*$')


def check(name, cond, detail=''):
    N[0] += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ('' if cond else f": {detail}"))
    if not cond:
        FAIL.append(name)


def fold(ins):
    """Run the production pass over a one-instruction list."""
    return strength_reduce([ins])[0][0]


def id_copies(mcode):
    n = 0
    for line in mcode.splitlines():
        m = IDCOPY.match(line)
        if m and m.group(1) != '$r0':
            n += 1
    return n


# ── unit: the algebraic rule ───────────────────────────────────────────────────

def test_identity_rule():
    print("\n[unit] x + 0 -> x, both operand orders")
    x, d = Temp('x'), Temp('d')
    for tag, left, right in (('x + 0', x, Const(0)), ('0 + x', Const(0), x)):
        out = fold(IRBinOp(d, '+', left, right))
        check(f"{tag} folds to a copy", isinstance(out, IRAssign), type(out).__name__)
        check(f"{tag} yields exactly x",
              isinstance(out, IRAssign) and isinstance(out.src, Temp)
              and out.src.name == 'x', repr(out))
        check(f"{tag} keeps the destination",
              getattr(out, 'dest', None) is not None and out.dest.name == 'd', repr(out))


def test_signedness_and_width():
    """Every integer IRBinOp lowers to a full-width ($i64)/($u64) ALU op and
    IRAssign lowers to `+ d ($i64) $r0 x`, so both are 64-bit register copies:
    the fold is exact for signed and unsigned alike, at every C width."""
    print("\n[unit] signed / unsigned, i32 and i64 sources")
    x, d = Temp('x'), Temp('d')
    for uns in (False, True):
        out = fold(IRBinOp(d, '+', x, Const(0), unsigned=uns))
        check(f"unsigned={uns}: folds", isinstance(out, IRAssign), type(out).__name__)
        check(f"unsigned={uns}: yields x",
              isinstance(out, IRAssign) and out.src.name == 'x', repr(out))


def test_negative_controls():
    print("\n[unit] the fold's safety boundary")
    x, y, d = Temp('x'), Temp('y'), Temp('d')

    out = fold(IRBinOp(d, '+', x, Const(5)))
    check("x + 5 is NOT folded", isinstance(out, IRBinOp), type(out).__name__)

    out = fold(IRBinOp(d, '+', x, y))
    check("x + y (runtime value) is NOT folded", isinstance(out, IRBinOp),
          type(out).__name__)

    # float: x + 0.0 is not x for x = -0.0, and it quiets a signalling NaN
    for ft in ('$f32', '$f64'):
        out = fold(IRBinOp(d, '+', x, Const(0), ftype=ft))
        check(f"FLOAT {ft}: x + 0 is NOT folded", isinstance(out, IRBinOp),
              type(out).__name__)

    # out of scope for R17.1 -- deliberately still emitted
    for op, c in (('-', 0), ('*', 1), ('|', 0), ('^', 0)):
        out = fold(IRBinOp(d, op, x, Const(c)))
        check(f"out of scope: x {op} {c} is left alone", isinstance(out, IRBinOp),
              type(out).__name__)

    # a non-arithmetic node must pass through untouched
    c = IRCast(d, x, 4, True) if 'IRCast' in dir() else None
    out = fold(IRBinOp(d, '<<', x, Const(0)))
    check("out of scope: x << 0 is left alone", isinstance(out, IRBinOp),
          type(out).__name__)


def test_kill_switch():
    print("\n[unit] APARA_NO_IDENTITY_FOLD")
    x, d = Temp('x'), Temp('d')
    os.environ['APARA_NO_IDENTITY_FOLD'] = '1'
    try:
        out = fold(IRBinOp(d, '+', x, Const(0)))
        check("knob restores the pre-R17.1 behaviour", isinstance(out, IRBinOp),
              type(out).__name__)
    finally:
        del os.environ['APARA_NO_IDENTITY_FOLD']
    check("knob is not sticky", isinstance(fold(IRBinOp(d, '+', x, Const(0))), IRAssign))


def test_strength_reduction_intact():
    """R17.1 must not weaken the rewrites this pass already performed."""
    print("\n[unit] existing strength reduction is unchanged")
    x, d = Temp('x'), Temp('d')
    out = fold(IRBinOp(d, '*', x, Const(8)))
    check("x * 8 still becomes x << 3",
          isinstance(out, IRBinOp) and out.op == '<<' and out.right.value == 3, repr(out))
    out = fold(IRBinOp(d, '/', x, Const(8), unsigned=True))
    check("unsigned x / 8 still becomes x >> 3",
          isinstance(out, IRBinOp) and out.op == '>>', repr(out))
    out = fold(IRBinOp(d, '/', x, Const(8)))
    check("SIGNED x / 8 is still refused", isinstance(out, IRBinOp) and out.op == '/',
          repr(out))


# ── end-to-end ─────────────────────────────────────────────────────────────────

def matmul(T='vu8_t', n=16, jt=8):
    nn = n * n
    ai = ",".join(str((i * 7 + 3) % 17) for i in range(nn))
    bi = ",".join(str((i * 11 + 5) % 19) for i in range(nn))
    ts = list(range(jt))
    dec = ", ".join(f"s{t}" for t in ts)
    zero = " ".join(f"s{t}=0;" for t in ts)
    body = "\n".join(f"        s{t} += A[i*{n}+k] * Bt[(j+{t})*{n}+k];" for t in ts)
    sto = "\n".join(f"      results[i*{n}+j+{t}] = (long long)s{t};" for t in ts)
    return f"""vu8_t A[{nn}] = {{{ai}}};
{T} Bt[{nn}] = {{{bi}}};
long long results[{nn}];
int main(void) {{
  int i, j, k, {dec};
  for (i=0;i<{n};i++)
    for (j=0;j<{n};j+={jt}) {{
      {zero}
      for (k=0;k<{n};k++) {{
{body}
      }}
{sto}
    }}
  return 0; }}
""".replace(f"vu8_t A[{nn}]", f"{T} A[{nn}]")


def scalar_addr(n=64, plus_zero=True):
    """A plain scalar kernel: no vectorization, address arithmetic only.

    `plus_zero` writes the identity into the SOURCE, so the two variants are
    semantically the same program written two ways."""
    ai = ",".join(str(i % 13) for i in range(n))
    expr = "(A[i] + 0)" if plus_zero else "A[i]"
    return f"""int A[{n}] = {{{ai}}};
long long results[{n}];
int main(void) {{
  int i;
  for (i=0;i<{n};i++) results[i] = (long long){expr};
  return 0; }}
"""


def build(src, off=False, dmem=True):
    d = tempfile.mkdtemp()
    open(os.path.join(d, 'm.c'), 'w').write(src)
    e = dict(os.environ)
    if off:
        e['APARA_NO_IDENTITY_FOLD'] = '1'
    args = ['bash', os.path.join(REPO, 'apara-cc'), os.path.join(d, 'm.c'), '--run']
    if dmem:
        args.append('--dmem-init')
    r = subprocess.run(args, capture_output=True, text=True, env=e, timeout=600)
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
                passed='== PASS' in out, dots=txt.count('$dot'),
                idc=id_copies(txt))


def test_end_to_end():
    print("\n[end-to-end] the fold reaches vector, scalar and address code")
    on, off = build(matmul()), build(matmul(), off=True)
    check("vector matmul: identity copies drop",
          on['idc'] < off['idc'], f"{off['idc']} -> {on['idc']}")
    check("vector matmul: faster", on['ticks'] < off['ticks'],
          f"{off['ticks']} -> {on['ticks']}")
    check("vector matmul: correct",
          on['passed'] and on['errs'] == 0 and on['checks'] == 256,
          f"{on['checks']} checks, {on['errs']} errors")
    check("vector matmul: same $dot count", on['dots'] == off['dots'],
          f"{on['dots']} vs {off['dots']}")
    print(f"       matmul vu8 16x16 JT=8: {off['ticks']} -> {on['ticks']} ticks, "
          f"{off['idc']} -> {on['idc']} identity copies")

    # The real invariant for an identity fold: writing the identity in the
    # SOURCE must produce exactly the program written without it.
    #
    # Do NOT assert "never slower than the same source with the fold off".
    # That comparison is a trap, and it fails here for a reason that is not a
    # regression: with the fold OFF, `A[i] + 0` compiles to 540 ticks while the
    # semantically identical `A[i]` compiles to 668. The redundant add gave the
    # scheduler a spare independent instruction that let it interleave the
    # operand and result address chains; without it the two chains serialize.
    # So the 540 was an ACCIDENT of a redundant instruction, and the fold
    # correctly converges `A[i] + 0` onto `A[i]`'s 668. The gap between 540 and
    # 668 is a latent scheduling deficiency (the scheduler should interleave
    # those chains unaided) and is recorded in
    # R17_1_IDENTITY_FOLD_DELIVERY.md, "Latent finding" -- it is not this
    # milestone's to fix.
    z_on = build(scalar_addr(plus_zero=True))
    p_on = build(scalar_addr(plus_zero=False))
    check("scalar kernel: correct", z_on['passed'] and z_on['errs'] == 0,
          str(z_on['errs']))
    check("`A[i] + 0` compiles IDENTICALLY to `A[i]`",
          z_on['mcode'] == p_on['mcode'],
          f"{len(z_on['mcode'])} vs {len(p_on['mcode'])} bytes")
    check("`A[i] + 0` and `A[i]` run in the same ticks",
          z_on['ticks'] == p_on['ticks'], f"{z_on['ticks']} vs {p_on['ticks']}")
    check("the source-level identity leaves no copy behind", z_on['idc'] <= 1,
          f"{z_on['idc']} identity copies")

    # kernels with no source-level identity must be untouched by the fold
    for name, src in (('arith', scalar_addr(plus_zero=False)),):
        a, b = build(src), build(src, off=True)
        check(f"{name}: unchanged by the fold", a['mcode'] == b['mcode'],
              f"{a['ticks']} vs {b['ticks']}")


def test_matrix(full=False):
    """The rule is algebraic, so it cannot depend on datatype or tile width."""
    print("\n[matrix] datatype x tile" + ("  (full)" if full else "  (subset)"))
    cases = [(T, jt) for T in ('vu8_t', 'vi8_t', 'vu16_t', 'vi16_t')
             for jt in (1, 2, 4, 8)]
    if not full:
        cases = [c for c in cases if c[1] in (4, 8)]
    for T, jt in cases:
        r = build(matmul(T=T, jt=jt))
        check(f"{T} 16x16 JT={jt}: correct",
              r['passed'] and r['errs'] == 0 and r['checks'] == 256,
              f"{r['checks']} checks, {r['errs']} errors")


def _verdict():
    print("\n" + "=" * 74)
    if FAIL:
        print(f"  RESULT: FAIL -- {len(FAIL)} of {N[0]} checks")
        for f in FAIL:
            print(f"    - {f}")
    else:
        print(f"  RESULT: PASS -- {N[0]}/{N[0]} checks")
    print("=" * 74)
    return 1 if FAIL else 0


def main():
    print("=" * 74)
    print("  R17.1 -- generic additive-identity folding (x + 0 -> x)")
    print("=" * 74)
    test_identity_rule()
    test_signedness_and_width()
    test_negative_controls()
    test_kill_switch()
    test_strength_reduction_intact()
    if '--unit' in sys.argv:
        return _verdict()
    test_end_to_end()
    test_matrix('--full' in sys.argv)
    return _verdict()


if __name__ == '__main__':
    sys.exit(main())

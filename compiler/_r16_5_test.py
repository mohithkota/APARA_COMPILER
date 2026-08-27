#!/usr/bin/env python3
"""
_r16_5_test.py -- R16.5: a loop-carried value that is loaded into a temp,
updated in place and stored back is promoted INTO THAT TEMP, instead of into a
fresh register bridged to it by a move at each end.

Before R16.5 `loop_reg.promote_loop_counters` always minted a fresh vreg, so a
closed round trip through one temp became

    _vac34 = _lr242            # copy IN  (the rewritten load)
    _vac34 = dot(...) ...      # in-place accumulate
    _lr242 = _vac34            # copy OUT (the rewritten store)

Both registers hold the same value for the whole span. Nothing downstream
removes the pair: `coalesce.py` excludes `IRVecDot` from its coalesceable
producers (`$dot $accumulate` reads its own destination) and the copy-in's
source has other users. A j-tiled vector matmul pays it per accumulator --
sixteen registers for eight accumulators at J_TILE=8.

These tests assert the STRUCTURAL property -- the promoted register is the
value's own temp exactly when the round trip is closed -- and never a benchmark,
a datatype, a tile width or a matrix size.

APARA_NO_ACC_DIRECT=1 restores the pre-R16.5 behaviour, so every effect below
is measured against the same compiler with only that knob changed.

Run: python3 _r16_5_test.py      (needs APARA_TOOLS)
"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import Temp, Const, IRLabel, IRLoad, IRStore, IRAssign, IRBinOp, IRVecDot
from loop_reg import _closed_roundtrip_temp

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FAIL, N = [], [0]

#: a register-to-register move, `+ $rD ($i64) $r0 $rS` in either operand order
MOVE = re.compile(r'^\+\s+\$r\d+\s+\(\$\w+\)\s+(\$r\d+)\s+(\$r\d+)\s*$')


def check(name, cond, detail=''):
    N[0] += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ('' if cond else f": {detail}"))
    if not cond:
        FAIL.append(name)


# ── source generators ──────────────────────────────────────────────────────────

def matmul(T='vu8_t', n=16, jt=4, storage='global', names=('A', 'Bt'),
           accname='s'):
    """A J-tiled dot matmul with STATIC initializers, so `--dmem-init` preloads
    the operands and the measured ticks are the kernel, not an init loop."""
    A, B = names
    nn = n * n
    ai = ",".join(str((i * 7 + 3) % 17) for i in range(nn))
    bi = ",".join(str((i * 11 + 5) % 19) for i in range(nn))
    ts = list(range(jt))
    dec = ", ".join(f"{accname}{t}" for t in ts)
    zero = " ".join(f"{accname}{t}=0;" for t in ts)
    body = "\n".join(
        f"        {accname}{t} += {A}[i*{n}+k] * {B}[(j+{t})*{n}+k];" for t in ts)
    sto = "\n".join(f"      results[i*{n}+j+{t}] = (long long){accname}{t};" for t in ts)
    arrays = f"{T} {A}[{nn}] = {{{ai}}};\n{T} {B}[{nn}] = {{{bi}}};"
    g = arrays if storage == 'global' else ''
    l = arrays if storage == 'local' else ''
    return f"""{g}
long long results[{nn}];
int main(void) {{
  {l}
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
"""


def matmul_runtime_init(T='vu8_t', n=16, jt=4):
    """The stack-local counterpart: arrays on the stack, filled by a loop."""
    ts = list(range(jt))
    dec = ", ".join(f"s{t}" for t in ts)
    zero = " ".join(f"s{t}=0;" for t in ts)
    body = "\n".join(
        f"        s{t} += A[i*{n}+k] * Bt[(j+{t})*{n}+k];" for t in ts)
    sto = "\n".join(f"      results[i*{n}+j+{t}] = (long long)s{t};" for t in ts)
    nn = n * n
    return f"""
long long results[{nn}];
int main(void) {{
  {T} A[{nn}], Bt[{nn}]; int i, j, k, {dec};
  for (i=0;i<{nn};i++) {{ A[i]=({T})((i*7+3)%17); Bt[i]=({T})((i*11+5)%19); }}
  for (i=0;i<{n};i++)
    for (j=0;j<{n};j+={jt}) {{
      {zero}
      for (k=0;k<{n};k++) {{
{body}
      }}
{sto}
    }}
  return 0; }}
"""


def dotprod(T='vu8_t', n=64):
    """A SINGLE-reduction dot product -- the R4.1 kernel. Its lowering chains
    each chunk into a FRESH temp, so the store value differs from the load
    destination and R16.5 must decline."""
    ai = ",".join(str((i * 7 + 3) % 17) for i in range(n))
    bi = ",".join(str((i * 11 + 5) % 19) for i in range(n))
    return f"""{T} A[{n}] = {{{ai}}};
{T} B[{n}] = {{{bi}}};
long long results[1];
int main(void) {{
  int k, s = 0;
  for (k=0;k<{n};k++) s += A[k] * B[k];
  results[0] = (long long)s;
  return 0; }}
"""


def summation(T='vu8_t', n=64):
    """A SINGLE-operand sum reduction -- the other R4.1 kernel."""
    ai = ",".join(str((i * 7 + 3) % 17) for i in range(n))
    return f"""{T} A[{n}] = {{{ai}}};
long long results[1];
int main(void) {{
  int k, s = 0;
  for (k=0;k<{n};k++) s += A[k];
  results[0] = (long long)s;
  return 0; }}
"""


def build(src, off=False, dmem=True):
    d = tempfile.mkdtemp()
    open(os.path.join(d, 'm.c'), 'w').write(src)
    e = dict(os.environ)
    if off:
        e['APARA_NO_ACC_DIRECT'] = '1'
    args = ['bash', os.path.join(REPO, 'apara-cc'), os.path.join(d, 'm.c'), '--run']
    if dmem:
        args.append('--dmem-init')
    r = subprocess.run(args, capture_output=True, text=True, env=e, timeout=1800)
    out = r.stdout + r.stderr
    mc, lg = os.path.join(d, 'm', 'm.mcode'), os.path.join(d, 'm', 'm.log')
    txt = open(mc, errors='replace').read() if os.path.exists(mc) else ''
    ticks = checks = bad = 0
    if os.path.exists(lg):
        L = open(lg, errors='replace').read()
        m = re.search(r'Stopped after (\d+) ticks', L)
        ticks = int(m.group(1)) if m else 0
        checks = L.count('PostCondition')
        bad = len(re.findall(r'expected', L))
    return dict(mcode=txt, ticks=ticks, checks=checks, bad=bad,
                passed='== PASS' in out, dots=txt.count('$dot'))


def dot_block(txt):
    """The labelled block that contains the `$dot`s, as a list of lines."""
    blocks, cur = {}, None
    for line in txt.splitlines():
        m = re.match(r'^([A-Za-z_]\w*):', line)
        if m:
            cur = m.group(1)
            blocks[cur] = []
        elif cur is not None:
            blocks[cur].append(line.strip())
    for lbl, body in blocks.items():
        if any(l.startswith('$dot') for l in body):
            return body
    return []


def moves_in(lines):
    return sum(1 for l in lines if MOVE.match(l) and '$r0' in MOVE.match(l).groups())


# ── unit level: the predicate itself ───────────────────────────────────────────

def _pack(loads, stores):
    return {'loads': loads, 'stores': stores, 'ebs': {8}}


def test_predicate_unit():
    """The predicate is a statement about IR shape. Four synthetic regions."""
    print("\n[unit] _closed_roundtrip_temp")
    T = Temp('_vac1')
    base = Temp('_vaa1')
    dot = IRVecDot(T, Temp('a'), Temp('b'), '$vu8', accumulate=True, accum=T)

    # (1) closed round trip: load -> in-place update -> store the SAME temp
    ins = [IRLabel('L'), IRLoad(T, base, Const(0), 8, True), dot,
           IRStore(base, Const(0), T, 8), IRLabel('E')]
    d = _pack([('_vaa1', 1, T, 8, True)], [('_vaa1', 3, T, 8)])
    r = _closed_roundtrip_temp(ins, d, 0, 4, 0, 4)
    check("fires on a closed self-update round trip",
          r is not None and r[0].name == '_vac1', repr(r))
    if r:
        check("returns the load and store indices to drop", r[1:] == (1, 3), repr(r[1:]))

    # (2) R14.6 / Phase 3: the store writes a DIFFERENT temp -- the move is real
    U = Temp('_vacc9')
    ins2 = [IRLabel('L'), IRLoad(T, base, Const(0), 8, True),
            IRVecDot(U, Temp('a'), Temp('b'), '$vu8', accumulate=True, accum=T),
            IRStore(base, Const(0), U, 8), IRLabel('E')]
    d2 = _pack([('_vaa1', 1, T, 8, True)], [('_vaa1', 3, U, 8)])
    check("declines when dest != accum (the copy is load-bearing)",
          _closed_roundtrip_temp(ins2, d2, 0, 4, 0, 4) is None)

    # (3) the temp is read AFTER the store -- promoting into it would hand that
    #     reader the accumulation instead of one iteration's value
    ins3 = list(ins) + [IRAssign(Temp('x'), T)]
    check("declines when the temp is used outside the round trip",
          _closed_roundtrip_temp(ins3, d, 0, 5, 0, 5) is None)

    # (4) another access to the SAME slot lands inside the span
    ins4 = [IRLabel('L'), IRLoad(T, base, Const(0), 8, True), dot,
            IRStore(base, Const(0), Temp('z'), 8),
            IRStore(base, Const(0), T, 8), IRLabel('E')]
    d4 = _pack([('_vaa1', 1, T, 8, True)],
               [('_vaa1', 3, Temp('z'), 8), ('_vaa1', 4, T, 8)])
    check("declines when another slot access falls inside the span",
          _closed_roundtrip_temp(ins4, d4, 0, 5, 0, 5) is None)

    # (5) a plain loop counter (load -> add into a NEW temp -> store) is untouched
    c0, c1 = Temp('_t1'), Temp('_t2')
    ins5 = [IRLabel('L'), IRLoad(c0, base, Const(0), 8, True),
            IRBinOp(c1, '+', c0, Const(1)), IRStore(base, Const(0), c1, 8),
            IRLabel('E')]
    d5 = _pack([('_vaa1', 1, c0, 8, True)], [('_vaa1', 3, c1, 8)])
    check("declines on an ordinary loop counter",
          _closed_roundtrip_temp(ins5, d5, 0, 4, 0, 4) is None)


# ── the shadow copies actually disappear ───────────────────────────────────────

def test_copies_removed():
    print("\n[emission] the accumulator round trip is gone")
    for jt in (4, 8):
        on = build(matmul(jt=jt))
        off = build(matmul(jt=jt), off=True)
        b_on, b_off = dot_block(on['mcode']), dot_block(off['mcode'])
        check(f"JT={jt}: baseline dot block carries accumulator moves",
              moves_in(b_off) >= jt, f"{moves_in(b_off)} moves")
        check(f"JT={jt}: R16.5 dot block carries none",
              moves_in(b_on) == 0, f"{moves_in(b_on)} moves")
        check(f"JT={jt}: the same number of $dot is emitted",
              on['dots'] == off['dots'], f"{on['dots']} vs {off['dots']}")
        check(f"JT={jt}: correct", on['passed'] and on['bad'] == 0
              and on['checks'] == 256, f"{on['checks']} checks, {on['bad']} bad")
        check(f"JT={jt}: not slower than baseline",
              on['ticks'] <= off['ticks'], f"{on['ticks']} vs {off['ticks']}")
        print(f"       JT={jt}: {off['ticks']} -> {on['ticks']} ticks, "
              f"{moves_in(b_off)} -> {moves_in(b_on)} moves in the dot block")


# ── single reductions must be untouched ────────────────────────────────────────

def test_single_reduction_unchanged():
    """Phase 4: a dot product and a sum reduction chain each chunk into a fresh
    temp, so the predicate declines and their code must be BYTE-IDENTICAL."""
    print("\n[regression] single-reduction kernels are byte-identical")
    for name, src in (('dot-product', dotprod()), ('sum-reduction', summation())):
        on, off = build(src), build(src, off=True)
        check(f"{name}: emitted mcode identical with R16.5 on and off",
              on['mcode'] == off['mcode'],
              f"{len(on['mcode'])} vs {len(off['mcode'])} bytes")
        check(f"{name}: correct", on['passed'] and on['bad'] == 0, str(on['bad']))


# ── the transform is about IR shape, not about matmul ──────────────────────────

def test_genericity():
    """Phase 16: renaming the arrays and the accumulators changes nothing."""
    print("\n[genericity] identity is structural, not lexical")
    a = build(matmul(jt=4))
    b = build(matmul(jt=4, names=('Mat', 'Wt'), accname='acc'))
    check("renaming arrays and accumulators keeps the tick count",
          a['ticks'] == b['ticks'], f"{a['ticks']} vs {b['ticks']}")
    check("renaming keeps the dot count", a['dots'] == b['dots'],
          f"{a['dots']} vs {b['dots']}")
    check("renamed program still has no accumulator moves",
          moves_in(dot_block(b['mcode'])) == 0)
    check("renamed program correct", b['passed'] and b['bad'] == 0)

    # Storage class is not an input to the rule either. The stack-local form
    # initializes at RUNTIME rather than with a static initializer: a
    # 256-element statically-initialized LOCAL array miscompiles on this
    # compiler, identically with R16.5 on and off (1272 ticks, 256/256 wrong
    # both ways), so it is a pre-existing defect and not a fixture for this
    # milestone. See R16_5_ACCUMULATOR_DIRECT_LOWERING.md, "Defect surfaced".
    loc = build(matmul_runtime_init(jt=4), dmem=False)
    check("a STACK-LOCAL matmul is correct too (storage is not an input)",
          loc['passed'] and loc['bad'] == 0, str(loc['bad']))
    check("the stack-local matmul still vectorizes", loc['dots'] > 0,
          f"{loc['dots']} dots")


# ── datatypes, sizes and tiles ─────────────────────────────────────────────────

def test_matrix(full=False):
    """Phase 11: the tile width and datatype are not part of the rule.

    Every case here is a full compile AND a simulator run, so the default is a
    representative subset -- all four datatypes at 16x16 across all four tiles,
    plus 32x32 at the widest tile. `--full` runs the whole 4x2x4 matrix."""
    print("\n[matrix] datatype x size x tile" + ("  (full)" if full else "  (subset)"))
    cases = [(T, n, jt)
             for T in ('vu8_t', 'vi8_t', 'vu16_t', 'vi16_t')
             for n in (16, 32)
             for jt in (1, 2, 4, 8)]
    if not full:
        cases = [c for c in cases if c[1] == 16 or c[2] == 8]
    for T, n, jt in cases:
        r = build(matmul(T=T, n=n, jt=jt))
        ok = r['passed'] and r['bad'] == 0 and r['checks'] == n * n
        check(f"{T} {n}x{n} JT={jt}: correct", ok,
              f"{r['checks']} checks, {r['bad']} bad")


# ── the remainder path still runs ──────────────────────────────────────────────

def test_remainder():
    """Phase 10: a row length that is not a whole number of packed chunks keeps
    its scalar remainder. 12 is not a multiple of the 8-lane vu8 chunk."""
    print("\n[remainder] non-multiple row lengths keep their peel")
    for n in (12, 20):
        r = build(matmul(n=n, jt=4))
        check(f"{n}x{n} (remainder {n % 8}): correct",
              r['passed'] and r['bad'] == 0 and r['checks'] == n * n,
              f"{r['checks']} checks, {r['bad']} bad")


def main():
    full = '--full' in sys.argv
    print("=" * 74)
    print("  R16.5 -- promote a closed accumulator round trip into its own temp")
    print("=" * 74)
    test_predicate_unit()
    if '--unit' in sys.argv:                # no toolchain, no simulator
        return _verdict()
    test_copies_removed()
    test_single_reduction_unchanged()
    test_genericity()
    test_matrix(full)
    test_remainder()
    return _verdict()


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


if __name__ == '__main__':
    sys.exit(main())

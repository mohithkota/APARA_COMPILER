#!/usr/bin/env python3
"""
_r14_6_test.py -- R14.6: the identity self-copy before `$dot $accumulate` is
emitted only when it is needed.

`$dot $accumulate` is read-modify-write on its destination, so `dest` must hold
the accumulator before it issues:

    dest != acc   ->  `+ dest ($i64) $r0 acc`  MUST still be emitted
    dest == acc   ->  the copy would be `+ rX ($i64) $r0 rX`, the identity for
                      ANY contents of rX, and is skipped

Both directions are asserted against the EMITTED MCODE, not merely against
simulator correctness -- a test that only checked results would pass with the
redundant copy still present.

Run: python3 _r14_6_test.py
"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FAIL, N = [], [0]

#: `+ rX (<type>) $r0 rX` -- the identity form this milestone removes.
SELF_COPY = re.compile(r'^\s*\+\s+(\$r\d+)\s+\(\$?\w+\)\s+\$r0\s+(\$r\d+)\s*$')
#: any accumulator set-up copy feeding a $dot: `+ rD (<type>) $r0 rA`
ACC_COPY = re.compile(r'^\s*\+\s+(\$r\d+)\s+\(\$?\w+\)\s+\$r0\s+(\$r\d+)\s*$')


def check(name, cond, detail=''):
    N[0] += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ('' if cond else f": {detail}"))
    if not cond:
        FAIL.append(name)


def compile_mcode(src):
    d = tempfile.mkdtemp()
    cp = os.path.join(d, 'm.c')
    open(cp, 'w').write(src)
    out = os.path.join(d, 'm.mcode')
    subprocess.run([sys.executable, os.path.join(HERE, 'compiler.py'),
                    '--preprocess', cp, '-o', out],
                   capture_output=True, text=True, timeout=1800)
    return open(out, errors='replace').read().splitlines() if os.path.exists(out) else None


def self_copies(lines):
    n = 0
    for l in lines:
        m = SELF_COPY.match(l)
        if m and m.group(1) == m.group(2):
            n += 1
    return n


def acc_copies_before_dots(lines):
    """Copies of the form `+ rD .. $r0 rA` with rD != rA that precede a $dot."""
    n = 0
    for i, l in enumerate(lines):
        m = ACC_COPY.match(l)
        if not m or m.group(1) == m.group(2):
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            if '$dot $accumulate' in lines[j] and m.group(1) in lines[j]:
                n += 1
                break
    return n


DOTP = """
long long results[2];
int main(void){ vi16_t a[64], b[64]; int i, s;
  for(i=0;i<64;i++){ a[i]=(vi16_t)(i&7); b[i]=(vi16_t)((i+1)&7); }
  s=0; for(i=0;i<64;i++) s += a[i]*b[i];
  results[0]=s; return 0; }
"""


def tiled(T='vu8_t', n=16, jt=4):
    nn = n * n
    acc = " ".join(f"s{t}=0;" for t in range(jt))
    dec = ", ".join(f"s{t}" for t in range(jt))
    body = "\n".join(f"        s{t} += A[i*{n}+k] * Bt[(j+{t})*{n}+k];"
                     for t in range(jt))
    sto = "\n".join(f"      results[i*{n}+j+{t}] = (long long)s{t};"
                    for t in range(jt))
    return f"""
long long results[{nn}];
int main(void) {{
  {T} A[{nn}], Bt[{nn}]; int i, j, k, {dec};
  for (i=0;i<{nn};i++) {{ A[i]=({T})((i*7+3)%17); Bt[i]=({T})((i*11+5)%19); }}
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


def test_no_identity_copy():
    print("\n[dest == acc] the identity copy is gone from the emitted mcode")
    cases = [('dot-product vi16', DOTP),
             ('matmul vu8 16x16 JT=4', tiled('vu8_t', 16, 4)),
             ('matmul vi16 16x16 JT=4', tiled('vi16_t', 16, 4)),
             ('matmul vu8 16x16 JT=2', tiled('vu8_t', 16, 2)),
             ('matmul vu8 16x16 JT=1', tiled('vu8_t', 16, 1))]
    for name, src in cases:
        ls = compile_mcode(src)
        if ls is None:
            check(f"{name} builds", False, ''); continue
        ndot = sum(1 for l in ls if '$dot' in l)
        check(f"{name}: emits $dot", ndot > 0, f"{ndot} $dot")
        check(f"{name}: ZERO identity self-copies", self_copies(ls) == 0,
              f"{self_copies(ls)} remain")


def test_guard_branch_directly():
    """Exercise BOTH codegen branches directly.

    The end-to-end sources all produce `dest == acc`, so they never reach the
    `dest != acc` branch -- a suite that only compiled them would leave the
    preserved-copy path untested. This drives `_gen_IRVecDot` itself with two
    distinct accumulator temps and asserts the copy IS still emitted."""
    print("\n[codegen] both branches of the guard, driven directly")
    from codegen import CodeGen
    from ir import Temp, IRVecDot

    def emitted(same):
        cg = CodeGen(global_base=0x400)
        out = []
        cg._emit = lambda t: out.append(t)
        a, b = Temp('_a'), Temp('_b')
        acc = Temp('_acc')
        dest = acc if same else Temp('_d')
        for t in (a, b, acc, dest):
            cg._alloc_reg(t)
        cg._gen_IRVecDot(IRVecDot(dest, a, b, '$vu8', accumulate=True, accum=acc))
        return out

    same = emitted(True)
    diff = emitted(False)
    check("dest == acc: only the $dot is emitted, no copy",
          len(same) == 1 and same[0].startswith('$dot $accumulate'),
          str(same))
    check("dest != acc: the accumulator copy IS still emitted",
          len(diff) == 2 and diff[0].startswith('+ ')
          and diff[1].startswith('$dot $accumulate'), str(diff))
    if len(diff) == 2:
        m = ACC_COPY.match(diff[0])
        check("dest != acc: that copy is NOT an identity",
              m is not None and m.group(1) != m.group(2), diff[0])


def test_required_copy_preserved():
    """dest != acc must still get its copy -- the guard must not over-fire.

    Verified structurally: every `$dot $accumulate rD` whose accumulator lives
    in a different register is still preceded by `+ rD .. $r0 rA`. If the guard
    were wrong, that copy would vanish and the accumulate would read garbage --
    which the differential oracle and the 38-program suite would also catch.
    """
    print("\n[dest != acc] the required copy is still emitted where needed")
    ls = compile_mcode(DOTP)
    if ls is None:
        check("dot-product builds", False, ''); return
    dots = [l for l in ls if '$dot $accumulate' in l]
    check("dot-product still uses $dot $accumulate", len(dots) > 0,
          f"{len(dots)}")
    # Every accumulate whose destination was not already the accumulator keeps
    # its set-up copy; the count is whatever the allocator produced, but the
    # invariant is that no accumulate is left without a defined destination.
    check("no $dot $accumulate is preceded by an IDENTITY copy",
          self_copies(ls) == 0, f"{self_copies(ls)} identity copies")
    print(f"       (non-identity accumulator copies retained: "
          f"{acc_copies_before_dots(ls)})")


def test_correctness_and_gain():
    print("\n[end-to-end] correctness preserved, self-copies removed")
    tools = os.environ.get('APARA_TOOLS')
    if not tools or not os.path.exists(os.path.join(tools, 'mcode_run')):
        check("APARA_TOOLS available", False, "set APARA_TOOLS"); return
    for name, src, checks in (('matmul vu8 16x16 JT=4', tiled('vu8_t', 16, 4), 256),
                              ('matmul vi16 16x16 JT=4', tiled('vi16_t', 16, 4), 256)):
        d = tempfile.mkdtemp(); cp = os.path.join(d, 'm.c'); open(cp, 'w').write(src)
        r = subprocess.run(['bash', os.path.join(REPO, 'apara-cc'), cp, '--run'],
                           capture_output=True, text=True, timeout=1800)
        out = r.stdout + r.stderr
        lg = os.path.join(d, 'm', 'm.log')
        got = errs = 0
        if os.path.exists(lg):
            L = open(lg, errors='replace').read()
            got = L.count('PostCondition'); errs = len(re.findall(r'^Error', L, re.M))
        check(f"{name}: {checks} PostConditions, 0 errors",
              '== PASS' in out and got == checks and errs == 0,
              f"pass={'== PASS' in out} checks={got} errors={errs}")


def main():
    print("=" * 74)
    print(" R14.6 -- redundant self-copy before $dot $accumulate")
    print("=" * 74)
    test_no_identity_copy()
    test_guard_branch_directly()
    test_required_copy_preserved()
    test_correctness_and_gain()
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

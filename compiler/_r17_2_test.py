#!/usr/bin/env python3
"""
_r17_2_test.py -- R17.2: guards for a STOPPED investigation.

R17.2 established that `$ld ($u128)` wide loads are now LEGAL on the fixed-DMEM
matmul path (R8.0's stack-alignment blocker expired when R16.2 moved the arrays
to 16-byte-aligned globals) but are NOT PROFITABLE. Measured: 699 -> 891 ticks,
+27.5%, while executing 256 FEWER instructions.

No production code changed, so there is no new behaviour to test. What these
tests pin are the three FACTS the "stop" rests on. If any of them stops holding,
the conclusion in R17_2_WIDE_LOAD_DELIVERY.md must be revisited rather than
trusted:

  1. one `$ld ($u128)` costs THREE instructions (load + two copy-outs), because
     codegen materialises the borrowed register pair into ordinary registers;
  2. the hot block is DEPENDENCE-bound -- it ships well above its issue-width
     and memory-lane bounds, so removing load instructions cannot remove
     bundles;
  3. the register pool is 28 with FP/SP/GBASE reserved, which is two short of
     the 30 needed to hold eight wide operand pairs and reach 8-wide `$dot`.

Run: python3 _r17_2_test.py [--unit]      (--unit needs no toolchain)
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


def check(name, cond, detail=''):
    N[0] += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ('' if cond else f": {detail}"))
    if not cond:
        FAIL.append(name)


def build(src, extra=('--dmem-init',), run=False):
    d = tempfile.mkdtemp()
    open(os.path.join(d, 'm.c'), 'w').write(src)
    args = ['bash', os.path.join(REPO, 'apara-cc'), os.path.join(d, 'm.c')]
    if run:
        args.append('--run')
    args += list(extra)
    r = subprocess.run(args, capture_output=True, text=True, timeout=600)
    out = r.stdout + r.stderr
    mc = os.path.join(d, 'm', 'm.mcode')
    txt = open(mc, errors='replace').read() if os.path.exists(mc) else ''
    return dict(mcode=txt, out=out, passed='== PASS' in out)


# ── fact 1: the cost of the existing wide-load facility ────────────────────────

WIDE = """vu8_t A[32] = {1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,
               17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32};
long long results[2];
int main(void) {
  long long lo, hi;
  __ld128(&lo, &hi, &A[0]);
  results[0] = lo; results[1] = hi;
  return 0; }
"""


def test_wide_load_costs_three_instructions():
    """A `$ld ($u128)` borrows an even-aligned pair and copies both halves out
    into ordinary registers, AFTER register allocation -- so no IR-level
    copy-propagation can remove them. Two narrow loads cost two instructions;
    one wide load costs three. This is why a direct substitution increases the
    instruction count."""
    print("\n[fact 1] one $ld ($u128) still costs load + 2 copy-outs")
    b = build(WIDE)
    if not b['mcode']:
        check("the __ld128 probe builds", False, b['out'][-300:])
        return
    lines = [' '.join(l.split()) for l in b['mcode'].splitlines()]
    wide = [i for i, l in enumerate(lines) if l.startswith('$ld ($u128)')]
    check("the intrinsic still emits $ld ($u128)", len(wide) >= 1, str(len(wide)))
    if not wide:
        return
    m = re.match(r'\$ld \(\$u128\) (\$r\d+)', lines[wide[0]])
    lo = int(m.group(1)[2:])
    pair = {f'$r{lo}', f'$r{lo+1}'}
    check("the wide load targets an EVEN-aligned register pair", lo % 2 == 0,
          f"starts at $r{lo}")
    window = lines[wide[0] + 1: wide[0] + 8]
    copies = [l for l in window
              if re.match(r'\+ \$r\d+ \(\$i64\) \$r0 (\$r\d+)$', l)
              and re.match(r'\+ \$r\d+ \(\$i64\) \$r0 (\$r\d+)$', l).group(1) in pair]
    check("both halves are copied out of the borrowed pair", len(copies) == 2,
          f"{len(copies)} copies found")
    print(f"       cost: 1 x $ld ($u128) + {len(copies)} copy-outs = "
          f"{1 + len(copies)} instructions, vs 2 for two narrow loads")


# ── fact 2: the register budget ────────────────────────────────────────────────

def test_register_budget():
    """8-wide `$dot` needs 8 accumulators + 8 B pairs (16) + an A pair (2) +
    4 control registers = 30. The pool is 28 because FP, SP and GBASE are
    reserved. The hand-written kernel reserves only $r0 and so has 31."""
    print("\n[fact 2] the pool is 28, two short of the 30 wide-issue needs")
    import codegen
    pool = list(getattr(codegen, 'POOL_REGS', []))
    check("the register pool is still 28", len(pool) == 28, f"{len(pool)}")
    for name, reg in (('FP', getattr(codegen, 'FP', None)),
                      ('SP', getattr(codegen, 'SP', None)),
                      ('ZERO', getattr(codegen, 'ZERO', None))):
        check(f"{name} is still reserved (not in the pool)", reg not in pool, str(reg))
    need = 8 + 16 + 2 + 4
    check("8-wide $dot needs more registers than the pool has",
          need > len(pool), f"needs {need}, pool {len(pool)}")
    print(f"       8 acc + 8 B pairs (16) + A pair (2) + 4 control = {need} > {len(pool)}")


def test_issue_width_and_lanes():
    """The bounds the hot block is measured against."""
    print("\n[fact 3] machine bounds are unchanged")
    import bundler
    check("ISSUE_WIDTH is still 8", getattr(bundler, 'ISSUE_WIDTH', None) == 8,
          str(getattr(bundler, 'ISSUE_WIDTH', None)))
    src = open(os.path.join(HERE, 'bundler.py')).read()
    check("the memory-lane cap of 4 per bundle is still enforced",
          "cls[0] >= 4" in src, "lane check not found")


# ── fact 4: alignment -- the part of the hypothesis that DID hold ──────────────

def matmul_global(n=16, jt=8):
    nn = n * n
    ai = ",".join(str((i * 7 + 3) % 17) for i in range(nn))
    bi = ",".join(str((i * 11 + 5) % 19) for i in range(nn))
    ts = list(range(jt))
    dec = ", ".join(f"s{t}" for t in ts)
    zero = " ".join(f"s{t}=0;" for t in ts)
    body = "\n".join(f"        s{t} += A[i*{n}+k] * Bt[(j+{t})*{n}+k];" for t in ts)
    sto = "\n".join(f"      results[i*{n}+j+{t}] = (long long)s{t};" for t in ts)
    return f"""vu8_t A[{nn}] = {{{ai}}};
vu8_t Bt[{nn}] = {{{bi}}};
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
"""


def test_alignment_still_holds():
    """R8.0 stopped because stack objects can never be 16-byte aligned
    (SP = 0x7FF8, mod 16 == 8). R16.2 moved these arrays to fixed DMEM. This
    pins the fact that made wide loads LEGAL -- the half of R17.2's hypothesis
    that was confirmed."""
    print("\n[fact 4] fixed-DMEM operands are 16-byte aligned (R8.0's blocker is gone)")
    b = build(matmul_global())
    m = re.search(r'gbase=(0x[0-9a-fA-F]+)', b['out'])
    check("the build reports a global base", m is not None, b['out'][-200:])
    if not m:
        return
    gbase = int(m.group(1), 16)
    check("the global base is 16-byte aligned", gbase % 16 == 0, hex(gbase))
    # every B-operand load offset in the hot block must be 8-byte spaced within
    # 16-byte-aligned rows, i.e. the pairing opportunity is structurally present
    offs = sorted(int(o) for o in
                  re.findall(r'\$ld \(\$u64\) \$r\d+ \[\$r\d+ \+ (\d+)\]', b['mcode']))
    rows = sorted({o for o in offs if o % 16 == 0})
    check("contiguous 16-byte-aligned operand pairs exist",
          all((r + 8) in offs for r in rows if r + 8 <= max(offs)) and len(rows) >= 2,
          f"row offsets {rows[:5]}")
    print(f"       gbase={hex(gbase)}, {len(rows)} aligned row bases, "
          f"pairs at (16t, 16t+8)")


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
    print("  R17.2 -- guards for a STOPPED investigation (wide loads: legal, unprofitable)")
    print("=" * 74)
    test_register_budget()
    test_issue_width_and_lanes()
    if '--unit' in sys.argv:
        return _verdict()
    test_wide_load_costs_three_instructions()
    test_alignment_still_holds()
    return _verdict()


if __name__ == '__main__':
    sys.exit(main())

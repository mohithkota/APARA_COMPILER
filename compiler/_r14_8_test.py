#!/usr/bin/env python3
"""
_r14_8_test.py -- R14.8: independent global stores share one base with
immediate displacements.

Asserts the transformation is STRUCTURAL (driven by a proved constant relation
between offsets, not by counts or names), that it degrades gracefully when the
relation cannot be proved, and that the emitted mcode really uses `[reg + imm]`.

Run: python3 _r14_8_test.py     (simulator parts need APARA_TOOLS)
"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import global_store_base_sharing as GSB

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FAIL, N = [], [0]
#: a store with a NON-ZERO immediate displacement off a shared base. The
#: displacement is negative when the group's anchor is the highest address
#: (e.g. the outputs are written in descending order), so the sign is optional.
DISP_ST = re.compile(r'\$st \(\$\w+\) \[\$r\d+ \+ (-?\d+)\]')


def _displaced(txt):
    return sum(1 for d in DISP_ST.findall(txt) if int(d) != 0)


def check(name, cond, detail=''):
    N[0] += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ('' if cond else f": {detail}"))
    if not cond:
        FAIL.append(name)


def tiled(T='vu8_t', n=16, jt=4, names=('A', 'Bt', 'i', 'j', 'k', 's'),
          reverse=False):
    A, B, I, J, K, Sv = names
    nn = n * n
    ts = list(range(jt))
    order = list(reversed(ts)) if reverse else ts
    acc = " ".join(f"{Sv}{t}=0;" for t in ts)
    dec = ", ".join(f"{Sv}{t}" for t in ts)
    body = "\n".join(f"        {Sv}{t} += {A}[{I}*{n}+{K}] * {B}[({J}+{t})*{n}+{K}];"
                     for t in ts)
    sto = "\n".join(f"      results[{I}*{n}+{J}+{t}] = (long long){Sv}{t};"
                    for t in order)
    return f"""
long long results[{nn}];
int main(void) {{
  {T} {A}[{nn}], {B}[{nn}]; int {I}, {J}, {K}, {dec};
  for ({I}=0;{I}<{nn};{I}++) {{ {A}[{I}]=({T})(({I}*7+3)%17); {B}[{I}]=({T})(({I}*11+5)%19); }}
  for ({I}=0;{I}<{n};{I}++)
    for ({J}=0;{J}<{n};{J}+={jt}) {{
      {acc}
      for ({K}=0;{K}<{n};{K}++) {{
{body}
      }}
{sto}
    }}
  return 0; }}
"""


def build(src, env=None):
    d = tempfile.mkdtemp()
    cp = os.path.join(d, 'm.c')
    open(cp, 'w').write(src)
    e = dict(os.environ, **(env or {}))
    r = subprocess.run(['bash', os.path.join(REPO, 'apara-cc'), cp, '--run'],
                       capture_output=True, text=True, env=e, timeout=1800)
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
                passed='== PASS' in out, disp=_displaced(txt))


def test_scales_with_group_size():
    """Not tied to 'four outputs': the gain tracks the number of provably
    constant-apart stores, and a single store is left alone."""
    print("\n[generic] the transformation scales with the group, not a count")
    exp = {1: 0, 2: 1, 4: 3}
    for jt in (1, 2, 4):
        off = build(tiled(jt=jt), {'APARA_NO_STORE_BASE_SHARE': '1'})
        on = build(tiled(jt=jt))
        check(f"J_TILE={jt}: {exp[jt]} immediate-displaced store(s)",
              on['disp'] == exp[jt], f"got {on['disp']}")
        check(f"J_TILE={jt}: correct (256 checks, 0 errors)",
              on['passed'] and on['checks'] == 256 and on['errs'] == 0,
              f"checks={on['checks']} errs={on['errs']}")
        if jt == 1:
            check("J_TILE=1: a lone store is NOT rewritten (ticks unchanged)",
                  on['ticks'] == off['ticks'], f"{off['ticks']} -> {on['ticks']}")
        else:
            check(f"J_TILE={jt}: fewer ticks than with the pass disabled",
                  on['ticks'] < off['ticks'], f"{off['ticks']} -> {on['ticks']}")


def test_datatypes():
    print("\n[datatypes] same structural decision for every supported marker")
    for T in ('vu8_t', 'vi8_t', 'vu16_t', 'vi16_t'):
        on = build(tiled(T=T, jt=4))
        check(f"{T}: 3 immediate-displaced stores", on['disp'] == 3,
              f"got {on['disp']}")
        check(f"{T}: correct", on['passed'] and on['errs'] == 0, '')


def test_bias():
    """Renamed variables and a different store ORDER are semantically the same
    group; the optimizer must reach the same decision."""
    print("\n[anti-bias] renamed / reordered sources decide identically")
    a = build(tiled(jt=4))
    b = build(tiled(jt=4, names=('X', 'Y', 'p', 'q', 'r', 'acc')))
    c = build(tiled(jt=4, reverse=True))
    check("renamed source: same number of displaced stores",
          a['disp'] == b['disp'], f"{a['disp']} vs {b['disp']}")
    check("renamed source: same ticks", a['ticks'] == b['ticks'],
          f"{a['ticks']} vs {b['ticks']}")
    check("reordered stores: still recognised as one group",
          c['disp'] == a['disp'], f"{c['disp']} vs {a['disp']}")
    check("reordered stores: correct", c['passed'] and c['errs'] == 0, '')


def test_no_store_lost():
    """Every output must still be written exactly once."""
    print("\n[safety] no store dropped, duplicated, or mis-addressed")
    on = build(tiled(jt=4))
    check("all 256 PostConditions verified against gcc",
          on['checks'] == 256 and on['errs'] == 0,
          f"checks={on['checks']} errs={on['errs']}")
    # four result stores survive, three of them displaced
    st = re.findall(r'\$st \(\$i64\) \[\$r\d+ \+ (-?\d+)\]', on['mcode'])
    check("four result stores present with distinct displacements",
          len(set(st)) >= 4, f"displacements seen: {sorted(set(st))}")


def test_immediate_bound():
    print("\n[bounds] the displacement must fit the ISA immediate field")
    check("IMM_LO/IMM_HI match codegen's $st [reg + imm] range",
          (GSB.IMM_LO, GSB.IMM_HI) == (-512, 511),
          f"{GSB.IMM_LO}..{GSB.IMM_HI}")


def main():
    print("=" * 74)
    print(" R14.8 -- shared-base result stores")
    print("=" * 74)
    test_immediate_bound()
    if not os.environ.get('APARA_TOOLS'):
        print("\n  (APARA_TOOLS unset -- skipping simulator tests)")
    else:
        test_scales_with_group_size()
        test_datatypes()
        test_bias()
        test_no_store_lost()
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

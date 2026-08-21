#!/usr/bin/env python3
"""
_r14_1a_test.py -- R14.1a: generic multi-reduction vectorization.

Proves the vector pipeline no longer assumes ONE reduction per loop: a
programmer-tiled matmul with N independent output columns detects, plans,
lowers and runs with EVERY reduction preserved -- while single-reduction
kernels stay byte-identical.

Run: python3 _r14_1a_test.py     (needs APARA_TOOLS for the simulator part)
"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matmul_probe
from vector_lowering import is_dot_shaped

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FAIL, N = [], [0]
LANES = {'vu8_t': 8, 'vi8_t': 8, 'vu16_t': 4, 'vi16_t': 4}


def check(name, cond, detail=''):
    N[0] += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ('' if cond else f": {detail}"))
    if not cond:
        FAIL.append(name)


def tiled(T='vu8_t', n=16, jt=1, names=('A', 'Bt', 'i', 'j', 'k', 's'),
          paren=False):
    """A j-tiled matmul. `names`/`paren` exist for the bias test."""
    A, B, I, J, K, Sv = names
    nn = n * n
    acc = " ".join(f"{Sv}{t}=0;" for t in range(jt))
    dec = ", ".join(f"{Sv}{t}" for t in range(jt))
    idxA = f"({I} * {n}) + {K}" if paren else f"{I} * {n} + {K}"
    body = "\n".join(
        f"        {Sv}{t} += {A}[{idxA}] * {B}[({J} + {t}) * {n} + {K}];"
        for t in range(jt))
    sto = "\n".join(
        f"      results[{I} * {n} + {J} + {t}] = (long long){Sv}{t};"
        for t in range(jt))
    return f"""
long long results[{nn}];
int main(void) {{
  {T} {A}[{nn}], {B}[{nn}]; int {I}, {J}, {K}, {dec};
  for ({I} = 0; {I} < {nn}; {I}++) {{ {A}[{I}] = ({T})(({I}*7+3)%17); {B}[{I}] = ({T})(({I}*11+5)%19); }}
  for ({I} = 0; {I} < {n}; {I}++)
    for ({J} = 0; {J} < {n}; {J} += {jt}) {{
      {acc}
      for ({K} = 0; {K} < {n}; {K}++) {{
{body}
      }}
{sto}
    }}
  return 0; }}
"""


def probe(src):
    fd, p = tempfile.mkstemp(suffix='.c'); os.write(fd, src.encode()); os.close(fd)
    try:
        ps = matmul_probe.probe_source(p, kinds=('matmul',))
    finally:
        os.unlink(p)
    return ps[0] if ps else None


def build_and_run(src):
    """Compile + simulate. Returns (ndot, ticks, checks, errors, passed)."""
    d = tempfile.mkdtemp()
    cp = os.path.join(d, 'mm.c'); open(cp, 'w').write(src)
    r = subprocess.run(['bash', os.path.join(REPO, 'apara-cc'), cp, '--run'],
                       capture_output=True, text=True, timeout=1800)
    out = r.stdout + r.stderr
    mc, lg = os.path.join(d, 'mm', 'mm.mcode'), os.path.join(d, 'mm', 'mm.log')
    ndot = open(mc, errors='replace').read().count('$dot') if os.path.exists(mc) else 0
    ticks = checks = errs = 0
    if os.path.exists(lg):
        L = open(lg, errors='replace').read()
        m = re.search(r'Stopped after (\d+) ticks', L)
        ticks = int(m.group(1)) if m else 0
        checks = L.count('PostCondition'); errs = len(re.findall(r'^Error', L, re.M))
    return ndot, ticks, checks, errs, ('== PASS' in out)


def test_detection():
    print("\n[detector] every reduction is found, not just the first")
    for jt in (1, 2, 4):
        p = probe(tiled(jt=jt))
        if p is None:
            check(f"J_TILE={jt} detected as matmul", False, "no matmul loop")
            continue
        pl = p.dot_plan
        check(f"J_TILE={jt}: plan carries {jt} reduction(s)",
              pl is not None and pl.ok and len(pl.reductions) == jt,
              f"reductions={len(pl.reductions) if pl and pl.ok else None} "
              f"reason={p.dot_plan_reason}")
        if pl and pl.ok:
            check(f"J_TILE={jt}: accumulator slots are DISTINCT",
                  len({r.acc_slot for r in pl.reductions}) == jt,
                  str([r.acc_slot for r in pl.reductions]))
            check(f"J_TILE={jt}: every reduction is dot-shaped (2 operands)",
                  all(len(r.array_slots) == 2 for r in pl.reductions),
                  str([r.array_slots for r in pl.reductions]))


def test_shared_operand():
    """The A row is one access shared by every column; B differs per column."""
    print("\n[sharing] identical operand accesses are materialised once")
    p = probe(tiled(jt=4))
    if p is None or not (p.dot_plan and p.dot_plan.ok):
        check("J_TILE=4 plans", False, ''); return
    pl = p.dot_plan
    keys = [tuple(r.array_key) for r in pl.reductions]
    shared = set(keys[0]) & set(keys[1]) & set(keys[2]) & set(keys[3])
    check("all four reductions share exactly one operand access (the A row)",
          len(shared) == 1, f"shared={shared}")
    distinct = {k for ks in keys for k in ks} - shared
    check("the other operand differs per output column",
          len(distinct) == 4, f"distinct={len(distinct)}")


def test_no_stream_dropped():
    print("\n[invariant] $dot count == chunks x J_TILE for every datatype")
    tools = os.environ.get('APARA_TOOLS')
    if not tools or not os.path.exists(os.path.join(tools, 'mcode_run')):
        check("APARA_TOOLS available", False, "set APARA_TOOLS"); return
    for T in ('vu8_t', 'vi8_t', 'vu16_t', 'vi16_t'):
        lanes = LANES[T]
        for jt in (1, 2, 4):
            want = (16 // lanes) * jt
            ndot, ticks, checks, errs, ok = build_and_run(tiled(T=T, jt=jt))
            check(f"{T} J_TILE={jt}: {want} $dot emitted", ndot == want,
                  f"got {ndot}")
            check(f"{T} J_TILE={jt}: correct (256 checks, 0 errors)",
                  ok and checks == 256 and errs == 0,
                  f"pass={ok} checks={checks} errors={errs}")


def test_single_reduction_unchanged():
    print("\n[regression] one-reduction kernels keep the singular description")
    dotp = """
long long results[2];
int main(void){ vi16_t a[64], b[64]; int i, s;
  for(i=0;i<64;i++){ a[i]=(vi16_t)(i&7); b[i]=(vi16_t)((i+1)&7); }
  s=0; for(i=0;i<64;i++) s += a[i]*b[i];
  results[0]=s; return 0; }
"""
    fd, p = tempfile.mkstemp(suffix='.c'); os.write(fd, dotp.encode()); os.close(fd)
    try:
        ps = matmul_probe.probe_source(p, kinds=None)
    finally:
        os.unlink(p)
    sel = [x for x in ps if x.kind == 'dot-product' and x.dot_plan
           and x.dot_plan.ok]
    if not sel:
        check("dot-product still plans", False, ''); return
    pl = sel[0].dot_plan
    check("dot-product has exactly ONE reduction", len(pl.reductions) == 1,
          str(len(pl.reductions)))
    check("its singular acc_slot aliases reductions[0]",
          pl.acc_slot == pl.reductions[0].acc_slot, '')
    check("its array_slots alias reductions[0]",
          list(pl.array_slots) == list(pl.reductions[0].array_slots), '')
    check("is_dot_shaped still true", is_dot_shaped(pl), '')


def test_bias():
    """Phase 9: equivalent spellings must classify and lower identically."""
    print("\n[anti-bias] renamed / re-parenthesised sources behave identically")
    base = probe(tiled(jt=4))
    alt = probe(tiled(jt=4, names=('X', 'Y', 'p', 'q', 'r', 'acc'), paren=True))
    if base is None or alt is None:
        check("both spellings detected", False, ''); return
    a, b = base.dot_plan, alt.dot_plan
    check("both plan successfully", a and a.ok and b and b.ok,
          f"{base.dot_plan_reason} / {alt.dot_plan_reason}")
    if not (a and a.ok and b and b.ok):
        return
    check("same reduction count", len(a.reductions) == len(b.reductions),
          f"{len(a.reductions)} vs {len(b.reductions)}")
    check("same chunks/lanes/trip",
          (a.chunks, a.lanes, a.trip) == (b.chunks, b.lanes, b.trip),
          f"{(a.chunks,a.lanes,a.trip)} vs {(b.chunks,b.lanes,b.trip)}")
    check("same operands per reduction",
          [len(r.array_slots) for r in a.reductions]
          == [len(r.array_slots) for r in b.reductions], '')


def main():
    print("=" * 74)
    print(" R14.1a -- generic multi-reduction vectorization")
    print("=" * 74)
    test_detection()
    test_shared_operand()
    test_single_reduction_unchanged()
    test_bias()
    test_no_stream_dropped()
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

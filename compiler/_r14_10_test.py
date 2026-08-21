#!/usr/bin/env python3
"""
_r14_10_test.py -- R14.10: pins the abstraction gap that STOPPED this milestone.

R14.10 proposed strength-reducing the result-store pointer across the J-tile
loop. The pointer genuinely IS an induction variable of that loop, but the
existing IVSR cannot express it, for a reason that is easy to regress silently:
its offset decomposer handles only `+` at the top level and only recognises an
IV term as `iv * Const` where the multiplied operand is DIRECTLY an IV load.

The result address is `((i*N) + j) * elem_size` -- a constant multiply applied to
a SUM -- so `_decompose` returns None and the access is never a candidate.

These tests assert that gap structurally, so a future change either closes it
deliberately or is caught. They also guard R14.8's constant-offset stores.

Run: python3 _r14_10_test.py     (simulator parts need APARA_TOOLS)
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


def src(n=16, jt=4, T='vu8_t'):
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


def test_ivsr_candidate_contract():
    """The gap, asserted against IVSR's source contract rather than prose."""
    print("\n[gap] IVSR cannot decompose a constant multiply over a sum")
    ivsr = open(os.path.join(HERE, 'ivsr.py'), errors='replace').read()
    check("_decompose handles only '+' at the top level",
          "ins.op == '+'" in ivsr and "_decompose" in ivsr,
          "the decomposer's shape changed -- re-check the R14.10 gap")
    # _iv_term requires the multiplied operand to be an IV LOAD directly
    seg = ivsr[ivsr.index('def _iv_term'):ivsr.index('def _decompose')]
    check("_iv_term requires the multiplied operand to be an IV load directly",
          '_is_iv_load_name(oth.name)' in seg, seg[:120])
    check("IVSR candidates are restricted to IRLoad/IRStore",
          "if c not in ('IRLoad', 'IRStore')" in ivsr,
          "candidate kinds changed -- re-check the R14.10 gap")
    check("candidates additionally require an INVARIANT base and a Temp offset",
          "not _inv(ins.base.name)" in ivsr
          and "isinstance(ins.offset, Temp)" in ivsr, '')


def test_pointer_is_an_iv_but_unreached():
    """The pointer really is an IV of the J-tile loop -- the gap is analysis,
    not legality."""
    print("\n[premise] the result pointer IS an induction variable of the tile loop")
    import pycparser
    import compiler as C
    from ir_gen import IRGenerator
    import global_store_base_sharing as G

    fd, p = tempfile.mkstemp(suffix='.c'); os.write(fd, src().encode()); os.close(fd)
    try:
        s, _ = C.preprocess(p)
        ast = pycparser.CParser().parse(C._FAKE_TYPEDEFS + s, filename=p)
    finally:
        os.unlink(p)
    g = IRGenerator(global_base=0x400); g.visit(ast)
    out, n = G.run(list(g.instructions))
    check("R14.8 still produces one shared base", n == 1, f"{n}")
    gs = [i for i, x in enumerate(out) if type(x).__name__ == 'IRGlobalAddrOf']
    if not gs:
        check("shared base exists", False, ''); return
    off = out[gs[0]].offset
    check("its offset is a computed temp", type(off).__name__ == 'Temp', repr(off))
    # that temp is defined by a MULTIPLY over a sum -- the unreachable shape
    dm = {}
    for i, x in enumerate(out):
        d = getattr(x, 'dest', None)
        if d is not None and hasattr(d, 'name'):
            dm[d.name] = i
    di = dm.get(off.name)
    ok = False
    if di is not None:
        ins = out[di]
        ok = type(ins).__name__ == 'IRBinOp' and ins.op == '*'
    check("the offset is defined by a MULTIPLY (not a '+') => _decompose returns None",
          ok, f"defined by {type(out[di]).__name__ if di is not None else None}")


def test_r148_intact():
    print("\n[guard] R14.8 constant-offset stores still intact")
    tools = os.environ.get('APARA_TOOLS')
    if not tools or not os.path.exists(os.path.join(tools, 'mcode_run')):
        check("APARA_TOOLS available", False, "set APARA_TOOLS"); return
    d = tempfile.mkdtemp(); cp = os.path.join(d, 'm.c'); open(cp, 'w').write(src())
    r = subprocess.run(['bash', os.path.join(REPO, 'apara-cc'), cp, '--run'],
                       capture_output=True, text=True, timeout=1800)
    out = r.stdout + r.stderr
    mc, lg = os.path.join(d, 'm', 'm.mcode'), os.path.join(d, 'm', 'm.log')
    txt = open(mc, errors='replace').read() if os.path.exists(mc) else ''
    disp = [int(x) for x in re.findall(
        r'\$st \(\$i64\) \[\$r\d+ \+ (-?\d+)\]', txt) if int(x) != 0]
    checks = errs = 0
    if os.path.exists(lg):
        L = open(lg, errors='replace').read()
        checks = L.count('PostCondition'); errs = len(re.findall(r'^Error', L, re.M))
    check("three immediate-displaced result stores", len(disp) == 3, str(disp))
    check("correct: 256 PostConditions, 0 errors",
          '== PASS' in out and checks == 256 and errs == 0,
          f"checks={checks} errs={errs}")


def main():
    print("=" * 74)
    print(" R14.10 -- result-pointer IVSR (STOPPED: IVSR abstraction gap)")
    print("=" * 74)
    test_ivsr_candidate_contract()
    test_pointer_is_an_iv_but_unreached()
    test_r148_intact()
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

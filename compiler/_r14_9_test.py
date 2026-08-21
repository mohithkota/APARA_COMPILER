#!/usr/bin/env python3
"""
_r14_9_test.py -- R14.9: pins the finding that STOPPED this milestone.

R14.9 proposed hoisting the result base out of the J-tile loop, on the premise
that it is loop-invariant there. It is NOT: the base offset is `(i*N + j) * 8`,
and `j` is the J-tile loop's own induction variable. These tests assert that
structurally, so the premise cannot be silently re-adopted, and they pin what IS
invariant (the row part) alongside what is not.

They also guard the R14.8 result-store sharing that this milestone must not
disturb.

Run: python3 _r14_9_test.py     (simulator parts need APARA_TOOLS)
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


def _ir(text):
    import pycparser
    import compiler as C
    from ir_gen import IRGenerator
    fd, p = tempfile.mkstemp(suffix='.c'); os.write(fd, text.encode()); os.close(fd)
    try:
        s, _ = C.preprocess(p)
        ast = pycparser.CParser().parse(C._FAKE_TYPEDEFS + s, filename=p)
    finally:
        os.unlink(p)
    g = IRGenerator(global_base=0x400)
    g.visit(ast)
    return list(g.instructions)


def test_base_is_not_invariant():
    """THE stop-condition test: the J-tile loop's IV is part of the address."""
    print("\n[premise] the result base is NOT invariant in the J-tile loop")
    import global_store_base_sharing as G
    from ir_utils import func_slices
    from loopopt.discovery import discover_function
    from loopopt.analysis_iv import annotate_induction_vars

    ir = _ir(src())
    out, n = G.run(list(ir))
    check("R14.8 still shares one base across the stores", n == 1, f"{n} groups")
    gs = [i for i, x in enumerate(out) if type(x).__name__ == 'IRGlobalAddrOf']
    if not gs:
        check("a shared base exists", False, ''); return
    gi = gs[0]
    check("the shared base carries a computed (non-constant) offset",
          getattr(out[gi], 'offset', None) is not None
          and type(out[gi].offset).__name__ == 'Temp',
          repr(getattr(out[gi], 'offset', None)))

    found = False
    for lo, hi in func_slices(out):
        sub = out[lo:hi + 1]
        ds = discover_function(sub, 0, len(sub) - 1)
        annotate_induction_vars(ds)
        for d in ds:
            blocks = set()
            for b in d.body_blocks:
                blk = d.cfg.blocks[b]
                blocks.update(range(blk.lo, blk.hi + 1))
            if (gi - lo) not in blocks:
                continue
            # innermost enclosing loop of the base == the J-tile loop
            addr, written = {}, set()
            for i in sorted(blocks):
                x = sub[i]
                c = type(x).__name__
                if c == 'IRLoadAddr':
                    addr[x.dest.name] = x.fp_offset
                if c == 'IRStore' and getattr(getattr(x, 'base', None), 'name', None) in addr:
                    written.add(addr[x.base.name])
            if d.primary_iv in written and not found:
                found = True
                check("the J-tile loop writes its own IV slot",
                      d.primary_iv in written, f"iv={d.primary_iv}")
                # the base offset reads that slot -> it VARIES
                reads = set()
                for i in sorted(blocks):
                    x = sub[i]
                    if type(x).__name__ == 'IRLoad' and getattr(
                            getattr(x, 'base', None), 'name', None) in addr:
                        reads.add(addr[x.base.name])
                check("the base's offset expression reads that same IV slot "
                      "=> NOT loop-invariant",
                      d.primary_iv in reads, f"iv={d.primary_iv} reads={sorted(reads)}")
                break
        if found:
            break
    check("an enclosing J-tile loop was identified", found, '')


def test_r148_sharing_intact():
    print("\n[guard] R14.8 store sharing is undisturbed")
    tools = os.environ.get('APARA_TOOLS')
    if not tools or not os.path.exists(os.path.join(tools, 'mcode_run')):
        check("APARA_TOOLS available", False, "set APARA_TOOLS"); return
    d = tempfile.mkdtemp()
    cp = os.path.join(d, 'm.c'); open(cp, 'w').write(src())
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
    check("three immediate-displaced result stores (R14.8 intact)",
          len(disp) == 3, f"{disp}")
    check("correct: 256 PostConditions, 0 errors",
          '== PASS' in out and checks == 256 and errs == 0,
          f"checks={checks} errs={errs}")


def main():
    print("=" * 74)
    print(" R14.9 -- result-base hoist premise (STOPPED: base is not invariant)")
    print("=" * 74)
    test_base_is_not_invariant()
    test_r148_sharing_intact()
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

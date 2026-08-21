#!/usr/bin/env python3
"""
_r14_3_test.py -- R14.3: pins the measured structure of the J-tiled matmul
block, i.e. the finding that DISPROVED this milestone's premise.

R14.3 set out to hoist invariant row bases out of "the inner K loop". There is
no inner K loop: the chunk dimension is fully unrolled, the vector body is
straight-line, and every base is already materialised exactly once per block
entry. These tests assert that, so a future change cannot silently reintroduce
per-chunk base recomputation -- and they pin where the cost actually is.

Run: python3 _r14_3_test.py    (needs APARA_TOOLS)
"""
import collections
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'matmul16_walkthrough', 'analysis'))

FAIL, N = [], [0]
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(name, cond, detail=''):
    N[0] += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ('' if cond else f": {detail}"))
    if not cond:
        FAIL.append(name)


SRC = """
long long results[{NN}];
int main(void) {{
  {T} A[{NN}], Bt[{NN}]; int i, j, k, s0, s1, s2, s3;
  for (i = 0; i < {NN}; i++) {{ A[i] = ({T})((i*7+3)%17); Bt[i] = ({T})((i*11+5)%19); }}
  for (i = 0; i < {N}; i++)
    for (j = 0; j < {N}; j += 4) {{
      s0=0; s1=0; s2=0; s3=0;
      for (k = 0; k < {N}; k++) {{
        s0 += A[i * {N} + k] * Bt[(j + 0) * {N} + k];
        s1 += A[i * {N} + k] * Bt[(j + 1) * {N} + k];
        s2 += A[i * {N} + k] * Bt[(j + 2) * {N} + k];
        s3 += A[i * {N} + k] * Bt[(j + 3) * {N} + k];
      }}
      results[i * {N} + j + 0] = (long long)s0;
      results[i * {N} + j + 1] = (long long)s1;
      results[i * {N} + j + 2] = (long long)s2;
      results[i * {N} + j + 3] = (long long)s3;
    }}
  return 0; }}
"""


def build(T='vu8_t', n=16):
    from bundle_stats import parse_mcode
    d = tempfile.mkdtemp()
    cp = os.path.join(d, 'mm.c')
    open(cp, 'w').write(SRC.format(T=T, N=n, NN=n * n))
    r = subprocess.run(['bash', os.path.join(REPO, 'apara-cc'), cp, '--run'],
                       capture_output=True, text=True, timeout=1800)
    out = r.stdout + r.stderr
    mc = os.path.join(d, 'mm', 'mm.mcode')
    if not os.path.exists(mc):
        return None, out
    b, _ = parse_mcode(mc)
    cur = None
    blocks = collections.OrderedDict()
    for x in b:
        if x['label']:
            cur = x['label']
        blocks.setdefault(cur, []).append(x)
    for l, bs in blocks.items():
        if any(i.startswith('$dot') for x in bs for i in x['instrs']):
            return bs, out
    return None, out


def test_no_inner_k_loop():
    """The premise check: the vector body is straight-line over chunks."""
    print("\n[premise] the vector body has no inner K loop to hoist out of")
    bs, out = build()
    if bs is None:
        check("built", False, out.strip().splitlines()[-1:] or ''); return
    branches = [i for x in bs for i in x['instrs'] if i.startswith('?')]
    check("exactly one control transfer (the block's own exit)",
          len(branches) == 1, str(branches))
    # every packed load addresses a base with a CONSTANT displacement
    lds = [i for x in bs for i in x['instrs'] if i.startswith('$ld')]
    imm = [i for i in lds if re.search(r'\[\$r\d+ \+ -?\d+\]', i)]
    check("every packed load uses [reg + immediate]",
          len(lds) > 0 and len(imm) == len(lds), f"{len(imm)}/{len(lds)}")
    bases = {re.search(r'\[(\$r\d+) \+', i).group(1) for i in imm}
    check("all packed loads share ONE base register", len(bases) == 1,
          str(bases))


def test_cost_is_the_epilogue():
    """Where the time actually goes: the scalar result stores, not addressing."""
    print("\n[bottleneck] the remaining cost is the scalar epilogue")
    for T, n in (('vu8_t', 16), ('vi16_t', 16), ('vu8_t', 32)):
        bs, out = build(T, n)
        if bs is None:
            check(f"{T} {n}x{n} built", False, ''); continue
        last = max(i for i, x in enumerate(bs)
                   if any(j.startswith('$dot') for j in x['instrs']))
        vec, epi = bs[:last + 1], bs[last + 1:]
        vi = sum(len(x['instrs']) for x in vec)
        ei = sum(len(x['instrs']) for x in epi)
        vipb = vi / len(vec) if vec else 0
        eipb = ei / len(epi) if epi else 0
        ones = sum(1 for x in epi if len(x['instrs']) == 1)
        check(f"{T} {n}x{n}: vector part packs well (IPB > 4)", vipb > 4,
              f"IPB {vipb:.2f}")
        check(f"{T} {n}x{n}: epilogue packs badly (IPB < 2)", eipb < 2,
              f"IPB {eipb:.2f}")
        check(f"{T} {n}x{n}: epilogue is mostly 1-instruction bundles",
              ones >= len(epi) * 0.75, f"{ones}/{len(epi)}")
        print(f"       vector {len(vec)} bdl/{vi} ins IPB {vipb:.2f} | "
              f"epilogue {len(epi)} bdl/{ei} ins IPB {eipb:.2f}")


def main():
    print("=" * 74)
    print(" R14.3 -- base-hoist premise check (STOPPED: nothing to hoist)")
    print("=" * 74)
    test_no_inner_k_loop()
    test_cost_is_the_epilogue()
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

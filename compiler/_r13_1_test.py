#!/usr/bin/env python3
"""
_r13_1_test.py -- R13.1: generic dot-shaped accumulator expansion.

Proves that R6.6's EXISTING multiple-accumulator machinery is now reached by
every dot-shaped reduction (dot-product AND matmul) via a structural predicate,
that the one-operand sum reduction is still excluded from the fully-unrolled
expansion, and that the resulting code is correct.

Run: python3 _r13_1_test.py
"""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matmul_probe
import reduction_accumulator_expansion as RAE
from vector_lowering import is_dot_shaped

HERE = os.path.dirname(os.path.abspath(__file__))
FAIL, N = [], [0]


def check(name, cond, detail=''):
    N[0] += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ('' if cond else f": {detail}"))
    if not cond:
        FAIL.append(name)


MM = """
long long results[{NN}];
int main(void) {{
    {T} A[{NN}], Bt[{NN}]; int i, j, k, s;
    for (i = 0; i < {NN}; i++) {{ A[i] = ({T})((i*7+3)%17); Bt[i] = ({T})((i*11+5)%19); }}
    for (i = 0; i < {N}; i++)
        for (j = 0; j < {N}; j++) {{ s = 0;
            for (k = 0; k < {N}; k++) s += A[i*{N}+k] * Bt[j*{N}+k];
            results[i*{N}+j] = (long long)s; }}
    return 0; }}
"""
DOTP = """
long long results[2];
int main(void){ vi16_t a[64], b[64]; int i, s;
  for(i=0;i<64;i++){ a[i]=(vi16_t)(i&7); b[i]=(vi16_t)((i+1)&7); }
  s=0; for(i=0;i<64;i++) s += a[i]*b[i];
  results[0]=s; return 0; }
"""
REDU = """
long long results[2];
int main(void){ vi16_t a[64]; int i, s;
  for(i=0;i<64;i++) a[i]=(vi16_t)(i&7);
  s=0; for(i=0;i<64;i++) s += a[i];
  results[0]=s; return 0; }
"""


def plans(src, kind):
    fd, p = tempfile.mkstemp(suffix='.c'); os.write(fd, src.encode()); os.close(fd)
    try:
        ps = matmul_probe.probe_source(p, kinds=None)
    finally:
        os.unlink(p)
    return [x for x in ps if x.kind == kind and x.dot_plan is not None
            and x.dot_plan.ok]


def test_predicate():
    print("\n[structural predicate] dot-shaped is about REDUCTION STRUCTURE")
    for tag, src, want in (('matmul', MM.format(T='vu8_t', N=16, NN=256), True),
                           ('dot-product', DOTP, True),
                           ('sum-reduction', REDU, False)):
        sel = plans(src, tag)
        if not sel:
            check(f"{tag} plans", False, "no plan produced")
            continue
        pl = sel[0].dot_plan
        check(f"{tag}: is_dot_shaped == {want}", is_dot_shaped(pl) is want,
              f"array_slots={pl.array_slots}")


def test_eligibility():
    print("\n[R6.6 eligibility] matmul now admitted; sum-reduction still admitted")
    mm = plans(MM.format(T='vu8_t', N=16, NN=256), 'matmul')
    rd = plans(REDU, 'sum-reduction')
    if mm:
        ok, why = RAE.eligible(mm[0].dot_plan, 2)
        check("matmul is eligible for expansion", ok, why)
    else:
        check("matmul plans", False, '')
    if rd:
        ok, why = RAE.eligible(rd[0].dot_plan, 2)
        check("sum-reduction still eligible (compact path unchanged)", ok, why)
    else:
        check("sum-reduction plans", False, '')


def test_sum_reduction_excluded_from_unrolled():
    """R8.1a's restriction must survive: the ONE-operand sum reduction does not
    get dot-shaped expansion in the fully-unrolled realisation."""
    print("\n[negative control] sum-reduction excluded from UNROLLED expansion")
    rd = plans(REDU, 'sum-reduction')
    if not rd:
        check("sum-reduction plans", False, ''); return
    pl = rd[0].dot_plan
    check("sum-reduction is NOT dot-shaped", not is_dot_shaped(pl),
          f"array_slots={pl.array_slots}")
    k = RAE.best_accumulator_count(pl.chunks) if is_dot_shaped(pl) else 1
    check("sum-reduction therefore gets k == 1 in build_vector_body", k == 1,
          f"k={k}")


def test_k_model():
    print("\n[k model] the k=1 register-copy cost is charged")
    check("chunks=2 -> k=2 (was 1: tie lost on smaller-k tie-break)",
          RAE.best_accumulator_count(2) == 2, str(RAE.best_accumulator_count(2)))
    check("chunks=3 -> k=2", RAE.best_accumulator_count(3) == 2,
          str(RAE.best_accumulator_count(3)))
    for c, want in ((1, 1), (4, 2), (8, 4), (16, 8)):
        check(f"chunks={c} -> k={want} (unchanged)",
              RAE.best_accumulator_count(c) == want,
              str(RAE.best_accumulator_count(c)))
    check("APARA_ACC_COUNT pins k for measurement",
          _pinned(4, 8) == 4 and _pinned(9, 8) == 8,
          "pin did not clamp as expected")


def _pinned(val, chunks):
    os.environ['APARA_ACC_COUNT'] = str(val)
    try:
        return RAE.best_accumulator_count(chunks)
    finally:
        os.environ.pop('APARA_ACC_COUNT', None)


def test_correctness_across_k():
    """Compile+run each datatype at every legal k. Requires $dot in the mcode:
    a simulator PASS with a scalar fallback is not acceptance."""
    print("\n[correctness] every datatype x every legal k, on the simulator")
    tools = os.environ.get('APARA_TOOLS')
    if not tools or not os.path.exists(os.path.join(tools, 'mcode_run')):
        check("APARA_TOOLS available", False, "set APARA_TOOLS to run this")
        return
    repo = os.path.dirname(HERE)
    for T, n in (('vu8_t', 16), ('vi8_t', 16), ('vu16_t', 16), ('vi16_t', 16)):
        for k in (1, 2, 4):
            d = tempfile.mkdtemp()
            cp = os.path.join(d, 'mm.c')
            open(cp, 'w').write(MM.format(T=T, N=n, NN=n * n))
            env = dict(os.environ, APARA_ACC_COUNT=str(k))
            r = subprocess.run(['bash', os.path.join(repo, 'apara-cc'), cp, '--run'],
                               capture_output=True, text=True, env=env, timeout=1800)
            out = r.stdout + r.stderr
            mc = os.path.join(d, 'mm', 'mm.mcode')
            ndot = open(mc, errors='replace').read().count('$dot') if os.path.exists(mc) else 0
            check(f"{T} {n}x{n} k={k}: simulator+gcc golden PASS",
                  '== PASS' in out, out.strip().splitlines()[-1] if out.strip() else '')
            check(f"{T} {n}x{n} k={k}: $dot emitted (no scalar fallback)",
                  ndot > 0, f"{ndot} $dot")


def main():
    print("=" * 74)
    print(" R13.1 -- generic dot-shaped accumulator expansion")
    print("=" * 74)
    test_predicate()
    test_eligibility()
    test_sum_reduction_excluded_from_unrolled()
    test_k_model()
    test_correctness_across_k()
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

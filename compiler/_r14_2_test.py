#!/usr/bin/env python3
"""
_r14_2_test.py -- R14.2: cross-reduction affine address sharing.

Part 1 tests the generic affine extension (`sym` + `constant_delta`) directly on
synthetic IR: equivalent spellings must yield the same symbolic base and the
right constant delta, and unrelated/runtime-varying forms must yield None.

Part 2 checks the compiler end-to-end.

Run: python3 _r14_2_test.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vector_affine as VA
FAIL, N = [], [0]


def check(name, cond, detail=''):
    N[0] += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ('' if cond else f": {detail}"))
    if not cond:
        FAIL.append(name)


class FakeAcc:
    """An AffineAccess stand-in for the pure algebra tests."""
    def __init__(self, coeff, const_off, sym, ok=True):
        self.ok, self.coeff, self.const_off = ok, coeff, const_off
        self.sym, self.sym_div, self.kind, self.elem_bytes = sym, 1, None, None
        self.reason = None


def test_constant_delta_algebra():
    print("\n[affine] constant_delta over symbolic parts")
    base = {('slot', -256, 0): 1}
    E = FakeAcc(1, 0, base)
    for c in (8, 16, 24):
        check(f"E vs E+{c} -> delta {c}",
              VA.constant_delta(E, FakeAcc(1, c, base)) == c,
              str(VA.constant_delta(E, FakeAcc(1, c, base))))
    check("E+16 vs E -> delta -16",
          VA.constant_delta(FakeAcc(1, 16, base), E) == -16, '')
    check("delta of an access with itself is 0",
          VA.constant_delta(E, E) == 0, '')
    print("\n[affine] negative controls -- no proof, no sharing")
    check("different IV rate -> None",
          VA.constant_delta(E, FakeAcc(2, 0, base)) is None, '')
    check("different symbolic part -> None",
          VA.constant_delta(E, FakeAcc(1, 0, {('slot', -512, 0): 1})) is None, '')
    check("extra symbolic term -> None",
          VA.constant_delta(E, FakeAcc(1, 0, {**base, ('slot', -8, 0): 1}))
          is None, '')
    check("scaled symbolic part (E*2) -> None",
          VA.constant_delta(E, FakeAcc(1, 0, {('slot', -256, 0): 2})) is None, '')
    check("unresolved access -> None",
          VA.constant_delta(E, FakeAcc(1, 0, base, ok=False)) is None, '')
    check("None operand -> None", VA.constant_delta(E, None) is None, '')


def _probe(src):
    import matmul_probe
    fd, p = tempfile.mkstemp(suffix='.c'); os.write(fd, src.encode()); os.close(fd)
    try:
        ps = matmul_probe.probe_source(p, kinds=('matmul',))
    finally:
        os.unlink(p)
    return ps[0] if ps else None


TILED = """
long long results[256];
int main(void) {{
  vu8_t A[256], Bt[256]; int i, j, k, {dec};
  for (i = 0; i < 256; i++) {{ A[i] = (vu8_t)((i*7+3)%17); Bt[i] = (vu8_t)((i*11+5)%19); }}
  for (i = 0; i < 16; i++)
    for (j = 0; j < 16; j += 4) {{
      {acc}
      for (k = 0; k < 16; k++) {{
{body}
      }}
{sto}
    }}
  return 0; }}
"""


def tiled(order='fwd'):
    ts = range(4) if order == 'fwd' else reversed(range(4))
    idx = ("(j + {t}) * 16 + k" if order != 'commuted' else "k + (j + {t}) * 16")
    body = "\n".join(f"        s{t} += A[i * 16 + k] * Bt[{idx.format(t=t)}];"
                     for t in ts)
    return TILED.format(dec=", ".join(f"s{t}" for t in range(4)),
                        acc=" ".join(f"s{t}=0;" for t in range(4)),
                        body=body,
                        sto="\n".join(f"      results[i * 16 + j + {t}] = (long long)s{t};"
                                      for t in range(4)))


def test_end_to_end_sharing():
    print("\n[compiler] the four B accesses are proven to share one base")
    p = _probe(tiled())
    if p is None or not (p.dot_plan and p.dot_plan.ok):
        check("J_TILE=4 plans", False,
              p.dot_plan_reason if p else 'no matmul loop')
        return
    pl = p.dot_plan
    check("4 reductions", len(pl.reductions) == 4, str(len(pl.reductions)))
    nbases = len(pl.shared_bases)
    check("bases materialised are fewer than 4 reductions x 2 operands",
          nbases < 8, f"{nbases} bases")
    print(f"       (materialised bases: {nbases})")


def test_structural_equivalence():
    print("\n[anti-bias] equivalent spellings share identically")
    a = _probe(tiled('fwd'))
    b = _probe(tiled('commuted'))
    if not (a and b and a.dot_plan and b.dot_plan and a.dot_plan.ok and b.dot_plan.ok):
        check("both spellings plan", False, ''); return
    check("same reduction count",
          len(a.dot_plan.reductions) == len(b.dot_plan.reductions), '')
    check("same number of materialised bases",
          len(a.dot_plan.shared_bases) == len(b.dot_plan.shared_bases),
          f"{len(a.dot_plan.shared_bases)} vs {len(b.dot_plan.shared_bases)}")


def main():
    print("=" * 74)
    print(" R14.2 -- cross-reduction affine address sharing")
    print("=" * 74)
    test_constant_delta_algebra()
    test_end_to_end_sharing()
    test_structural_equivalence()
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

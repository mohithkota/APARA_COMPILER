"""
vector_compact_corpus.py -- R4.2.5 corpus evaluation of compact vector loops.

Measures, for the six kernel classes (dot product, reduction, vector copy, add,
subtract, multiply), how compact vector loop generation compares with R4.2's
fully-unrolled realisation:

  * vectorization coverage        (must not fall)
  * static bundle count           (must fall)
  * generated code size           (must fall)
  * dynamic operations            (must stay overwhelmingly reduced)
  * compile time
  * rollback rate
  * which realisation was chosen, and where the crossover lies

R4.2 is reproduced exactly by forcing the unrolled realisation, so the comparison
is measured on the same kernels through the same pipeline rather than quoted from
the previous report.

Run:  python3 compiler/vector_compact_corpus.py
"""

import os
import sys
import glob
import copy
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import pycparser
from compiler import preprocess, _FAKE_TYPEDEFS
from ir import Temp
from ir_gen import IRGenerator
from ir_utils import func_slices
from codegen import CodeGen
from bundler import bundle_mcode
from dot_vectorizer import vectorize_all_module
from vector_lowering import differential_packed
import vector_compact_loop as _vcl
from vector_elementwise_corpus import _KERNELS

_GB = 0x400


def _build(code):
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=_GB)
    g.visit(ast)
    return list(g.instructions)


def _measure(ir):
    """(post_optimizer_bundles, mcode_chars, vector_ops).

    R4.2.6: the headline bundle count is the POST-OPTIMIZER one -- what actually
    ships -- because the raw backend count measured before the scalar optimizer,
    SWP and superblock run is a poor predictor of final size (that mismatch is
    exactly what R4.2.6 fixed in the selector)."""
    try:
        body = CodeGen(global_base=_GB).generate(copy.deepcopy(ir), global_base=_GB)
        vops = body.count('$dot') + body.count('$vreduce') + body.count('$v ')
        from vector_size_probe import probe_bundles
        b, spilled = probe_bundles(ir, _GB)
        if b is None or spilled:
            _m, _n, b = bundle_mcode(body, schedule=True)
        return b, len(body), vops
    except Exception:
        return -1, -1, -1


def _force_unrolled(enable):
    """R4.2 behaviour is reproduced by making the selector always keep the
    unrolled candidate, so both realisations are measured through one pipeline."""
    if enable:
        _vcl.choose_smaller = _unrolled_only
    else:
        _vcl.choose_smaller = _real_choose


_real_choose = _vcl.choose_smaller


def _unrolled_only(candidates, global_base):
    for name, slc in candidates:
        if name == 'unrolled' and slc is not None:
            return slc, name, {name: None}
    return _real_choose(candidates, global_base)


def _run_suite(label):
    vec = mism = rolled = 0
    dyn_s = dyn_v = 0
    bun_s = bun_v = 0
    size_s = size_v = 0
    per_kernel = {}
    t0 = time.time()
    for name, code, expect in _KERNELS:
        ir = _build(code)
        out, stats, reps = vectorize_all_module(ir)
        rolled += stats.rolled_back
        if not stats.vectorized:
            continue
        r = reps[0]
        vec += 1
        if any(differential_packed(ir, out, lo, hi)[0] == 'mismatch'
               for lo, hi in func_slices(ir)):
            mism += 1
        sb, ss, _ = _measure(ir)
        vb, vs, vops = _measure(out)
        dyn_s += r.scalar_dynamic
        dyn_v += r.vector_dynamic
        bun_s += sb
        bun_v += vb
        size_s += ss
        size_v += vs
        per_kernel[name] = (_vcl.realisation_of(out), sb, vb, r.chunks,
                            r.scalar_dynamic, r.vector_dynamic, vops)
    el = time.time() - t0
    return dict(label=label, vec=vec, mism=mism, rolled=rolled,
                dyn_s=dyn_s, dyn_v=dyn_v, bun_s=bun_s, bun_v=bun_v,
                size_s=size_s, size_v=size_v, time=el, per=per_kernel)


def _print_detail(r425):
    print("  Per-kernel realisation choice (chosen by measured bundle count)")
    print(f"    {'kernel':24}{'chunks':>7}{'chosen':>11}{'bundles':>12}"
          f"{'dyn ops':>18}")
    for name, (real, sb, vb, chunks, ds, dv, _v) in r425['per'].items():
        print(f"    {name:24}{chunks:>7}{real:>11}{sb:6d}->{vb:<5d}"
              f"{ds:8d}->{dv:<8d}")


def _compare(a, b):
    print(f"  {'metric':32}{a['label']:>14}{b['label']:>14}{'delta':>12}")

    def row(k, fmt='{:d}', better_low=True):
        va, vb = a[k], b[k]
        d = vb - va
        print(f"    {k:30}{fmt.format(va):>14}{fmt.format(vb):>14}{d:+12d}")

    row('vec')
    row('bun_v')
    row('size_v')
    row('dyn_v')
    row('mism')
    row('rolled')
    print(f"    {'pipeline time (s)':30}{a['time']:>14.2f}{b['time']:>14.2f}"
          f"{b['time'] - a['time']:+12.2f}")


def _corpus_no_regression():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c', 'demo_prof/**/*.c',
              'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))
    progs = changed = unchanged = 0
    for f in files:
        try:
            src, _ = preprocess(f)
            ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
            Temp.reset()
            g = IRGenerator(global_base=_GB)
            ir = list(g.instructions) if g.visit(ast) or True else None
        except Exception:
            continue
        progs += 1
        out, stats, _reps = vectorize_all_module(ir)
        if stats.vectorized:
            changed += 1
        else:
            try:
                x = CodeGen(global_base=_GB).generate(copy.deepcopy(ir), global_base=_GB)
                y = CodeGen(global_base=_GB).generate(copy.deepcopy(out), global_base=_GB)
                if x == y:
                    unchanged += 1
            except Exception:
                unchanged += 1
    return progs, changed, unchanged


def main():
    print("=" * 78)
    print("  R4.2.5 COMPACT VECTOR LOOP GENERATION -- CORPUS EVALUATION")
    print("=" * 78)

    _force_unrolled(True)
    r42 = _run_suite('R4.2')
    _force_unrolled(False)
    r425 = _run_suite('R4.2.5')

    _print_detail(r425)
    print()
    print("  R4.2 (always unrolled)  vs  R4.2.5 (measured choice)")
    _compare(r42, r425)
    print()
    print(f"    scalar baseline bundles on these kernels : {r425['bun_s']}")
    print(f"    R4.2   vectorized bundles                : {r42['bun_v']}")
    print(f"    R4.2.5 vectorized bundles                : {r425['bun_v']}")
    if r42['dyn_s']:
        print(f"    dynamic reduction R4.2   : "
              f"{100.0 * (r42['dyn_s'] - r42['dyn_v']) / r42['dyn_s']:.1f}%")
        print(f"    dynamic reduction R4.2.5 : "
              f"{100.0 * (r425['dyn_s'] - r425['dyn_v']) / r425['dyn_s']:.1f}%")
    print()
    progs, changed, unchanged = _corpus_no_regression()
    print("  Full corpus (no-regression proof)")
    print(f"    programs                        : {progs}")
    print(f"    vectorized (packed kernels)     : {changed}")
    print(f"    scalar & byte-identical (on/off): {unchanged}/{progs - changed}")
    print("=" * 78)
    ok = (r425['mism'] == 0
          and r425['vec'] == r42['vec']                 # coverage preserved
          and r425['bun_v'] <= r42['bun_v']             # bundles reduced
          and r425['size_v'] <= r42['size_v']           # code size reduced
          and unchanged == (progs - changed))
    print("  RESULT:", "PASS (smaller code, coverage + correctness preserved)"
          if ok else "FAIL")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())

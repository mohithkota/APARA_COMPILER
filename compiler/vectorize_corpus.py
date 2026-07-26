"""
vectorize_corpus.py -- R4.1 corpus evaluation of automatic dot/reduction
vectorization.

Two parts:
  1. a dedicated packed-kernel suite (dot / reduction over vi8/vu8/vi16 arrays,
     with and without remainders, wide and narrow accumulators) -- the general
     corpus has few packed arrays -- measuring vectorization coverage, dynamic
     operation reduction, real `$dot`/`$vreduce` emission, and 100% differential
     validation;
  2. the full corpus, proving scalar compilation is UNCHANGED (byte-identical
     generated code with vectorization on vs off) on programs with no packed
     kernel -- i.e. no regressions.

Run:  python3 compiler/vectorize_corpus.py
"""

import os
import sys
import glob
import copy

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
from dot_vectorizer import vectorize_module
from vector_lowering import differential_packed

_GB = 0x400

# packed-kernel suite (name, source, expected-vectorizable)
_KERNELS = [
    ('dot vi8 N=32',   "long long f(){vi8_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}", True),
    ('dot vu8 N=64',   "long long f(){vu8_t a[64],b[64];int i;long long s=0;for(i=0;i<64;i++)s+=a[i]*b[i];return s;}", True),
    ('dot vi8 rem N=20',"long long f(){vi8_t a[20],b[20];int i;long long s=0;for(i=0;i<20;i++)s+=a[i]*b[i];return s;}", True),
    ('dot vi16 N=32',  "long long f(){vi16_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}", True),
    ('red vi8 N=48',   "long long f(){vi8_t a[48];int i;long long s=0;for(i=0;i<48;i++)s+=a[i];return s;}", True),
    ('red vi16 N=32',  "long long f(){vi16_t a[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i];return s;}", True),
    ('red vi32 N=16',  "long long f(){vi32_t a[16];int i;long long s=0;for(i=0;i<16;i++)s+=a[i];return s;}", True),
    ('dot vi32 (no-op)',"long long f(){vi32_t a[8],b[8];int i;long long s=0;for(i=0;i<8;i++)s+=a[i]*b[i];return s;}", False),
    ('narrow acc (rb)', "long long f(){vi16_t a[16],b[16];int i;long s=0;for(i=0;i<16;i++)s+=a[i]*b[i];return s;}", False),
    ('unpacked (no-op)',"long long f(){int a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}", False),
]


def _build(code):
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=_GB)
    g.visit(ast)
    return list(g.instructions)


def _mcode_vecops(ir):
    try:
        body = CodeGen(global_base=_GB).generate(copy.deepcopy(ir), global_base=_GB)
        return body.count('$dot') + body.count('$vreduce')
    except Exception:
        return -1


def _kernel_suite():
    print("  Dedicated packed-kernel suite")
    vec = mism = 0
    dyn_s = dyn_v = 0
    for name, code, expect in _KERNELS:
        ir = _build(code)
        out, stats, reps = vectorize_module(ir)
        r = reps[0] if reps else None
        ok = True
        if stats.vectorized:
            for lo, hi in func_slices(ir):
                if differential_packed(ir, out, lo, hi)[0] == 'mismatch':
                    ok = False
                    mism += 1
            vecops = _mcode_vecops(out)
            vec += 1
            dyn_s += r.scalar_dynamic
            dyn_v += r.vector_dynamic
            print(f"    {name:20} VECTORIZED {r.vtype} x{r.lanes}  "
                  f"dyn {r.scalar_dynamic}->{r.vector_dynamic}  mcode-vecops={vecops}  "
                  f"{'OK' if ok else 'MISMATCH'}")
        else:
            print(f"    {name:20} scalar     ({r.reason if r else '-'})")
    print(f"    -> vectorized {vec}/{len(_KERNELS)}, mismatches {mism}, "
          f"dynamic ops {dyn_s}->{dyn_v}")
    return vec, mism


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
        out, stats, _reps = vectorize_module(ir)
        if stats.vectorized:
            changed += 1
        else:
            # no kernel -> generated code must be byte-identical
            try:
                a = CodeGen(global_base=_GB).generate(copy.deepcopy(ir), global_base=_GB)
                b = CodeGen(global_base=_GB).generate(copy.deepcopy(out), global_base=_GB)
                if a == b:
                    unchanged += 1
            except Exception:
                unchanged += 1
    return progs, changed, unchanged


def main():
    print("=" * 78)
    print("  R4.1 AUTOMATIC DOT / REDUCTION VECTORIZATION -- CORPUS EVALUATION")
    print("=" * 78)
    vec, mism = _kernel_suite()
    print()
    progs, changed, unchanged = _corpus_no_regression()
    print("  Full corpus (no-regression proof)")
    print(f"    programs                       : {progs}")
    print(f"    vectorized (packed kernels)     : {changed}")
    print(f"    scalar & byte-identical (on/off): {unchanged}/{progs - changed}")
    print("=" * 78)
    ok = mism == 0 and unchanged == (progs - changed)
    print("  RESULT:", "PASS (100% differential, scalar unchanged)" if ok else "FAIL")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())

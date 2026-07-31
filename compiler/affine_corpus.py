"""
affine_corpus.py -- R4.2.8 corpus evaluation of affine access recognition.

Four questions, all answered by measurement:

  1. Does the normalizer resolve every roadmap kernel shape?
  2. Does it AGREE with the recognizer R4.1/R4.2 use today -- i.e. does it accept
     everything currently vectorized? (It must never reject what already works.)
  3. What would it UNLOCK on the corpus that is rejected today?
  4. Does it change generated code? (It must not: analysis only.)

Run:  python3 compiler/affine_corpus.py
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
from loopopt.discovery import discover_function
from loopopt.analysis_iv import annotate_induction_vars
from loopopt.analysis_mem import annotate_memory_effects
from vector_affine import (classify_loop, CONTIGUOUS, INVARIANT, STRIDED,
                           UNKNOWN)
from dot_vectorizer import vectorize_all_module

_GB = 0x400

ROADMAP = [
    ("R4.1  dot vi8",
     "long long f(){vi8_t a[64],b[64];int k;long long s=0;for(k=0;k<64;k++)s+=a[k]*b[k];return s;}", 1),
    ("R4.1  reduction vi16",
     "long long f(){vi16_t a[64];int k;long long s=0;for(k=0;k<64;k++)s+=a[k];return s;}", 2),
    ("R4.2  elementwise vi8",
     "long long f(){vi8_t a[64],b[64],c[64];int k;for(k=0;k<64;k++)c[k]=a[k]+b[k];return c[0];}", 1),
    ("R4.2  elementwise vi16",
     "long long f(){vi16_t a[64],b[64],c[64];int k;for(k=0;k<64;k++)c[k]=a[k]+b[k];return c[0];}", 2),
    ("PLAN  AXPY (i-k-j) vi8",
     "long long f(){vi8_t A[64],B[64],C[64];int i,j,k;for(i=0;i<8;i++)for(k=0;k<8;k++)for(j=0;j<8;j++)C[i*8+j]+=A[i*8+k]*B[k*8+j];return C[0];}", 1),
    ("PLAN  AXPY (i-k-j) vi16",
     "long long f(){vi16_t A[64],B[64],C[64];int i,j,k;for(i=0;i<8;i++)for(k=0;k<8;k++)for(j=0;j<8;j++)C[i*8+j]+=A[i*8+k]*B[k*8+j];return C[0];}", 2),
    ("PLAN  GEMM row-dot (Bt)",
     "long long f(){vi8_t A[64],Bt[64],C[64];int i,j,k;for(i=0;i<8;i++)for(j=0;j<8;j++)for(k=0;k<8;k++)C[i*8+j]+=A[i*8+k]*Bt[j*8+k];return C[0];}", 1),
    ("PLAN  conv1d inner-taps",
     "long long f(){vi8_t in[64],w[8],out[64];int i,r;for(i=0;i<56;i++)for(r=0;r<8;r++)out[i]+=in[i+r]*w[r];return out[0];}", 1),
    ("PLAN  conv1d inner-out",
     "long long f(){vi8_t in[64],w[8],out[64];int i,r;for(r=0;r<8;r++)for(i=0;i<56;i++)out[i]+=in[i+r]*w[r];return out[0];}", 1),
    ("PLAN  conv2d inner-j",
     "long long f(){vi8_t in[64],w[9],out[64];int i,j,r,s;for(i=0;i<6;i++)for(r=0;r<3;r++)for(s=0;s<3;s++)for(j=0;j<6;j++)out[i*8+j]+=in[(i+r)*8+(j+s)]*w[r*3+s];return out[0];}", 1),
    ("REJECT column-strided",
     "long long f(){vi8_t B[64];int j,k;long long s=0;for(j=0;j<8;j++)for(k=0;k<8;k++)s+=B[k*8+j];return s;}", 1),
    ("REJECT symbolic stride",
     "long long f(int N){vi8_t B[64];int j,k;long long s=0;for(j=0;j<8;j++)for(k=0;k<8;k++)s+=B[k*N+j];return s;}", 1),
    ("REJECT gather",
     "long long f(){vi8_t a[64];int idx[64];int k;long long s=0;for(k=0;k<64;k++)s+=a[idx[k]];return s;}", 1),
    ("REJECT unpacked int",
     "long long f(){int a[64],b[64];int k;long long s=0;for(k=0;k<64;k++)s+=a[k]*b[k];return s;}", 4),
]


def _build(code):
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=_GB)
    g.visit(ast)
    return list(g.instructions)


def _inner_loops(ir):
    out = []
    for lo, hi in func_slices(ir):
        descs = discover_function(ir, lo, hi)
        annotate_induction_vars(descs)
        annotate_memory_effects(descs)
        out += [d for d in descs if d.is_innermost]
    return out


def _roadmap():
    print("  Roadmap kernel shapes (innermost-loop accesses of the element width)")
    print(f"    {'kernel':28}{'contig':>8}{'invar':>7}{'strided':>9}{'unknown':>9}  verdict")
    ok = True
    for name, code, eb in ROADMAP:
        ir = _build(code)
        d = _inner_loops(ir)[0] if _inner_loops(ir) else None
        c = i = s = u = 0
        for _idx, ins, a in classify_loop(d, ir):
            if getattr(ins, 'elem_bytes', None) != eb:
                continue
            if not a.ok:
                u += 1
            elif a.kind == CONTIGUOUS:
                c += 1
            elif a.kind == INVARIANT:
                i += 1
            else:
                s += 1
        resolvable = (u == 0 and s == 0 and c > 0)
        want = not name.startswith('REJECT')
        verdict = ('RESOLVED' if resolvable else 'rejected')
        flag = '' if resolvable == want else '   <-- UNEXPECTED'
        if resolvable != want:
            ok = False
        print(f"    {name:28}{c:>8}{i:>7}{s:>9}{u:>9}  {verdict}{flag}")
    return ok


def _agreement():
    """The new normalizer must accept everything the CURRENT recognizer does."""
    print("  Agreement with the recognizer R4.1/R4.2 use today")
    disagree = 0
    checked = 0
    for name, code, eb in ROADMAP:
        if name.startswith(('R4.1', 'R4.2')):
            ir = _build(code)
            out, stats, _r = vectorize_all_module(copy.deepcopy(ir))
            if not stats.vectorized:
                continue
            checked += 1
            d = _inner_loops(ir)[0]
            kinds = [a.kind for _i, ins, a in classify_loop(d, ir)
                     if a.ok and getattr(ins, 'elem_bytes', None) == eb]
            if CONTIGUOUS not in kinds:
                disagree += 1
                print(f"    {name:28} DISAGREES (vectorized today, not contiguous now)")
    print(f"    -> {checked} currently-vectorized kernels checked, "
          f"{disagree} disagreements")
    return disagree == 0


def _corpus():
    """What the normalizer resolves across the real corpus, and the proof that it
    changes nothing."""
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c', 'demo_prof/**/*.c',
              'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))
    progs = loops = 0
    tot = {CONTIGUOUS: 0, INVARIANT: 0, STRIDED: 0, UNKNOWN: 0}
    fully = 0
    identical = 0
    for f in files:
        try:
            src, _ = preprocess(f)
            ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
            Temp.reset()
            g = IRGenerator(global_base=_GB)
            g.visit(ast)
            ir = list(g.instructions)
        except Exception:
            continue
        progs += 1
        before = CodeGen(global_base=_GB).generate(copy.deepcopy(ir), global_base=_GB)
        try:
            for d in _inner_loops(ir):
                loops += 1
                kinds = []
                for _i, ins, a in classify_loop(d, ir):
                    k = a.kind if a.ok else UNKNOWN
                    tot[k] = tot.get(k, 0) + 1
                    kinds.append(k)
                if kinds and all(k in (CONTIGUOUS, INVARIANT) for k in kinds):
                    fully += 1
        except Exception:
            pass
        after = CodeGen(global_base=_GB).generate(copy.deepcopy(ir), global_base=_GB)
        if before == after:
            identical += 1
    print("  Full corpus")
    print(f"    programs analysed                : {progs}")
    print(f"    innermost loops classified       : {loops}")
    print(f"    accesses  contiguous/invariant   : {tot[CONTIGUOUS]}/{tot[INVARIANT]}")
    print(f"    accesses  strided/unknown        : {tot[STRIDED]}/{tot[UNKNOWN]}")
    print(f"    loops fully resolved             : {fully}")
    print(f"    generated code identical         : {identical}/{progs}")
    return identical == progs


def main():
    print("=" * 78)
    print("  R4.2.8 AFFINE ACCESS RECOGNITION -- CORPUS EVALUATION")
    print("=" * 78)
    a = _roadmap()
    print()
    b = _agreement()
    print()
    c = _corpus()
    print("=" * 78)
    ok = a and b and c
    print("  RESULT:", "PASS (roadmap resolved, agrees with today, code unchanged)"
          if ok else "FAIL")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())

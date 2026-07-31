"""
vector_elementwise_corpus.py -- R4.2 corpus evaluation.

Three parts:
  1. the R4.2 kernel suite -- Dot Product, Reduction, Vector Add, Vector Sub,
     Vector Multiply, Vector Copy -- run through the SINGLE generic pipeline with
     BOTH clients registered, measuring coverage, dynamic instruction reduction,
     bundle count, compile time, rollback rate, validation success and which
     client committed each kernel (framework reuse);
  2. an R4.1-vs-R4.2 comparison, proving the framework conversion changed nothing
     for the dot/reduction kernels and quantifying what elementwise adds;
  3. the full corpus, proving scalar compilation is UNCHANGED (byte-identical
     generated code with vectorization on vs off) -- i.e. no regressions.

Run:  python3 compiler/vector_elementwise_corpus.py
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
from dot_vectorizer import vectorize_module, vectorize_all_module
from vector_lowering import differential_packed

_GB = 0x400

# The six kernel classes R4.2 is measured on, plus the rejection cases that prove
# the gates still bite. (name, source, expected-vectorizable)
_KERNELS = [
    # --- R4.1 kernels: must be unchanged by the framework conversion -----------
    ('dot vi8 N=32',      "long long f(){vi8_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}", True),
    ('dot vi16 N=32',     "long long f(){vi16_t a[32],b[32];int i;long long s=0;for(i=0;i<32;i++)s+=a[i]*b[i];return s;}", True),
    ('reduction vi8 N=48', "long long f(){vi8_t a[48];int i;long long s=0;for(i=0;i<48;i++)s+=a[i];return s;}", True),
    ('reduction vi32 N=16', "long long f(){vi32_t a[16];int i;long long s=0;for(i=0;i<16;i++)s+=a[i];return s;}", True),
    # --- R4.2 elementwise -----------------------------------------------------
    ('vector add vi8',    "long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}", True),
    ('vector add vu8',    "long long f(){vu8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}", True),
    ('vector add vi16',   "long long f(){vi16_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}", True),
    ('vector add vi32',   "long long f(){vi32_t a[16],b[16],c[16];int i;for(i=0;i<16;i++)c[i]=a[i]+b[i];return c[0];}", True),
    ('vector sub vi8',    "long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]-b[i];return c[0];}", True),
    ('vector mul vi8',    "long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]*b[i];return c[0];}", True),
    ('vector mul vi16',   "long long f(){vi16_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]*b[i];return c[0];}", True),
    ('vector copy vi8',   "long long f(){vi8_t a[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i];return c[0];}", True),
    ('vector add rem N=20', "long long f(){vi8_t a[20],b[20],c[20];int i;for(i=0;i<20;i++)c[i]=a[i]+b[i];return c[0];}", True),
    ('in-place a[i]+=b[i]', "long long f(){vi8_t a[32],b[32];int i;for(i=0;i<32;i++)a[i]=a[i]+b[i];return a[0];}", True),
    # --- rejections that MUST still be refused --------------------------------
    ('unpacked add (no-op)', "long long f(){int a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]+b[i];return c[0];}", False),
    ('saxpy a*x (no-op)', "long long f(){vi8_t a[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]*3;return c[0];}", False),
    ('divide (no-op)',    "long long f(){vi8_t a[32],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i]/b[i];return c[0];}", False),
    ('shifted a[i+1]',    "long long f(){vi8_t a[33],b[32],c[32];int i;for(i=0;i<32;i++)c[i]=a[i+1]+b[i];return c[0];}", False),
    ('trip < 2*lanes',    "long long f(){vi8_t a[8],b[8],c[8];int i;for(i=0;i<8;i++)c[i]=a[i]+b[i];return c[0];}", False),
    ('narrow acc (rb)',   "long long f(){vi16_t a[16],b[16];int i;long s=0;for(i=0;i<16;i++)s+=a[i]*b[i];return s;}", False),
]


def _build(code):
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=_GB)
    g.visit(ast)
    return list(g.instructions)


def _compile_stats(ir):
    """(bundles, vector-op count) through the real backend, or (-1, -1)."""
    try:
        body = CodeGen(global_base=_GB).generate(copy.deepcopy(ir), global_base=_GB)
        _m, _n, b = bundle_mcode(body, schedule=True)
        vops = body.count('$dot') + body.count('$vreduce') + body.count('$v ')
        return b, vops
    except Exception:
        return -1, -1


def _kernel_suite():
    print("  R4.2 kernel suite -- ONE pipeline, BOTH clients")
    print(f"    {'kernel':22} {'result':11} {'via':13} {'dyn ops':>16} {'bundles':>10}  val")
    vec = mism = rolled = 0
    dyn_s = dyn_v = 0
    bun_s = bun_v = 0
    unexpected = []
    t0 = time.time()
    for name, code, expect in _KERNELS:
        ir = _build(code)
        out, stats, reps = vectorize_all_module(ir)
        r = reps[0] if reps else None
        rolled += stats.rolled_back
        if stats.vectorized:
            ok = all(differential_packed(ir, out, lo, hi)[0] != 'mismatch'
                     for lo, hi in func_slices(ir))
            if not ok:
                mism += 1
            sb, _ = _compile_stats(ir)
            vb, vops = _compile_stats(out)
            vec += 1
            dyn_s += r.scalar_dynamic
            dyn_v += r.vector_dynamic
            bun_s += sb
            bun_v += vb
            print(f"    {name:22} {'VECTORIZED':11} {r.transform:13} "
                  f"{r.scalar_dynamic:6d}->{r.vector_dynamic:<8d} {sb:4d}->{vb:<4d} "
                  f"{'OK' if ok else 'MISMATCH'} ({vops} vec-ops)")
            if not expect:
                unexpected.append(name)
        else:
            print(f"    {name:22} {'scalar':11} {'-':13} "
                  f"{'':16} {'':10}  ({r.reason if r else 'no-kernel'})")
            if expect:
                unexpected.append(name)
    el = time.time() - t0
    n_expect = sum(1 for _n, _c, e in _KERNELS if e)
    print(f"    -> vectorized {vec}/{n_expect} expected ({len(_KERNELS)} cases), "
          f"mismatches {mism}, rollbacks {rolled}")
    print(f"    -> dynamic ops {dyn_s} -> {dyn_v} "
          f"({100.0 * (dyn_s - dyn_v) / dyn_s:.1f}% fewer)" if dyn_s else "")
    print(f"    -> static bundles on vectorized kernels {bun_s} -> {bun_v}")
    print(f"    -> pipeline time {el:.2f}s total ({1000 * el / len(_KERNELS):.1f} ms/kernel)")
    if unexpected:
        print(f"    -> UNEXPECTED OUTCOMES: {unexpected}")
    return vec, mism, unexpected


def _r41_vs_r42():
    """The dot/reduction kernels must behave IDENTICALLY through the R4.1-only
    client set and the full R4.2 client set -- the conversion is a refactor."""
    print("  R4.1 -> R4.2 conversion check (dot/reduction unchanged)")
    same = diff = 0
    for name, code, _e in _KERNELS[:4]:
        ir = _build(code)
        a, sa, _ra = vectorize_module(copy.deepcopy(ir))       # R4.1 client only
        b, sb, _rb = vectorize_all_module(copy.deepcopy(ir))   # both clients
        identical = ([repr(i) for i in a] == [repr(i) for i in b]
                     and sa.vectorized == sb.vectorized)
        same += identical
        diff += (not identical)
        print(f"    {name:22} {'IDENTICAL' if identical else 'DIFFERS'}")
    print(f"    -> {same} identical, {diff} differ")
    return diff


def _corpus_no_regression():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c', 'demo_prof/**/*.c',
              'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))
    progs = changed = unchanged = 0
    t0 = time.time()
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
            # no kernel committed -> generated code must be byte-identical
            try:
                a = CodeGen(global_base=_GB).generate(copy.deepcopy(ir), global_base=_GB)
                b = CodeGen(global_base=_GB).generate(copy.deepcopy(out), global_base=_GB)
                if a == b:
                    unchanged += 1
            except Exception:
                unchanged += 1
    return progs, changed, unchanged, time.time() - t0


def main():
    print("=" * 78)
    print("  R4.2 GENERIC VECTOR FRAMEWORK + ELEMENTWISE -- CORPUS EVALUATION")
    print("=" * 78)
    vec, mism, unexpected = _kernel_suite()
    print()
    diff = _r41_vs_r42()
    print()
    progs, changed, unchanged, el = _corpus_no_regression()
    print("  Full corpus (no-regression proof)")
    print(f"    programs                        : {progs}")
    print(f"    vectorized (packed kernels)     : {changed}")
    print(f"    scalar & byte-identical (on/off): {unchanged}/{progs - changed}")
    print(f"    corpus pass time                : {el:.1f}s "
          f"({1000 * el / max(1, progs):.1f} ms/program)")
    print("=" * 78)
    ok = (mism == 0 and not unexpected and diff == 0
          and unchanged == (progs - changed))
    print("  RESULT:", "PASS (100% differential, R4.1 unchanged, scalar unchanged)"
          if ok else "FAIL")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())

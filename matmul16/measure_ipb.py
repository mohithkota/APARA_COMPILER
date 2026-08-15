#!/usr/bin/env python3
"""measure_ipb.py -- IPB for the 16x16 vector matmul.

Reports three different numbers, because they mean different things:

  whole-program IPB   instructions / bundles over the whole program, weighted by
                      each block's proved trip count. This is what the
                      verification harness prints, and it is dominated by the
                      256-element initialisation loop.
  vector-region IPB   the same ratio over the vectorized kernel region only.
  scalar baseline     the same program compiled with APARA_NO_VECTORIZE=1.

Machine limit is 8 (issue width). Reuses the R6.1 analysis framework, which is
verified to reproduce the production bundler instruction for instruction.
"""
import os, sys, copy, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'compiler'))

from vector_backend import ilp_analysis as ia          # noqa: E402
from vector_backend import latency as lat              # noqa: E402


def _preprocessed():
    """matmul16.c through the compiler's own preprocessor.

    `ilp_analysis.build_ir` feeds the text straight to pycparser, which rejects
    comments; production `compile_c_to_mcode` runs `gcc -E -P` first. Use the
    same front end so the measured IR is the one that actually ships."""
    from compiler import preprocess
    text, used_cpp = preprocess(os.path.join(HERE, 'matmul16.c'))
    return text


def analyse(src, vectorize=True):
    old = os.environ.get('APARA_NO_VECTORIZE')
    if not vectorize:
        os.environ['APARA_NO_VECTORIZE'] = '1'
    else:
        os.environ.pop('APARA_NO_VECTORIZE', None)
    try:
        ir = ia.build_ir(src)
        if vectorize:
            vec, st, _r = ia.vectorize_all_module(copy.deepcopy(ir))
        else:
            vec, st = ir, None
        sel, mtext, tier = ia.production_codegen(copy.deepcopy(vec))
        freq, _unknown = ia.label_frequencies(vec)
        from vector_backend import occupancy as occ
        rep = occ.analyze_mcode(mtext, label_freq=freq)
        return vec, st, rep, freq, tier
    finally:
        if old is None:
            os.environ.pop('APARA_NO_VECTORIZE', None)
        else:
            os.environ['APARA_NO_VECTORIZE'] = old


def main():
    src = _preprocessed()
    out = []
    def w(s=''):
        print(s); out.append(s)

    w('=' * 74)
    w('  16x16 VECTOR MATRIX MULTIPLY -- IPB REPORT')
    w('=' * 74)

    for tag, vecz in (('VECTORIZED', True), ('SCALAR (APARA_NO_VECTORIZE=1)', False)):
        vec, st, rep, freq, tier = analyse(src, vecz)
        stat = rep.totals()
        dyn = rep.totals(dynamic=True)
        w()
        w(f'{tag}')
        w(f'  tier selected              : {tier}')
        if st is not None:
            w(f'  kernels vectorized         : {st.vectorized}')
        w(f'  static  bundles            : {stat["bundles"]:.0f}')
        w(f'  static  instructions       : {stat["instructions"]:.0f}')
        w(f'  static  IPB                : {stat["instructions"]/stat["bundles"]:.3f}')
        w(f'  dynamic bundles            : {dyn["bundles"]:.0f}')
        w(f'  dynamic instructions       : {dyn["instructions"]:.0f}')
        w(f'  DYNAMIC IPB (whole program): {dyn["instructions"]/dyn["bundles"]:.3f}'
          f'   ({100*dyn["instructions"]/dyn["bundles"]/lat.ISSUE_WIDTH:.1f}% of the '
          f'{lat.ISSUE_WIDTH}-wide machine)')
        w(f'  occupancy                  : {dyn["occupancy"]:.1%}')

        if vecz:
            # vector region = the innermost vectorized kernel's blocks
            r = ia.analyze_kernel('matmul16', 'gemm', src)
            bb = r.body_bundles or []
            if bb:
                ents = [r.occ.flat[i] for b in bb for i in b.flat_idx]
                w(f'  --- vector region only ---')
                w(f'  realisation                : {r.realisation}')
                w(f'  bundles / instructions     : {len(bb)} / {len(ents)}')
                w(f'  VECTOR-REGION IPB          : {len(ents)/len(bb):.3f}'
                  f'   ({100*len(ents)/len(bb)/lat.ISSUE_WIDTH:.1f}% of peak)')
                w(f'  region occupancy           : {r.occ.totals(bb)["occupancy"]:.1%}')

    w()
    w('  Note: whole-program IPB is dominated by the 256-element scalar')
    w('  initialisation loop; the vector-region figure is the kernel itself.')
    w('=' * 74)

    with open(os.path.join(HERE, 'IPB_REPORT.txt'), 'w') as f:
        f.write('\n'.join(out) + '\n')
    print(f"\nwritten: {os.path.join(HERE, 'IPB_REPORT.txt')}")


if __name__ == '__main__':
    main()

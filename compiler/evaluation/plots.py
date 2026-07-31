"""plots.py -- dependency-free plots (ASCII + inline SVG) into plots/."""
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import compare

CEIL = 8.0


def _svg_bars(title, pairs, path, ceiling=None):
    w, bh, pad = 720, 18, 130
    h = 40 + bh * len(pairs) + 20
    mx = max([v for _n, v in pairs] + ([ceiling] if ceiling else [0])) or 1
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
         f'font-family="monospace" font-size="11">',
         f'<text x="8" y="18" font-size="13">{title}</text>']
    for i, (n, v) in enumerate(pairs):
        y = 34 + i * bh
        bw = int((w - pad - 60) * v / mx)
        p.append(f'<text x="8" y="{y+11}">{n[:20]}</text>')
        p.append(f'<rect x="{pad}" y="{y+2}" width="{bw}" height="{bh-6}" '
                 f'fill="#4a7fb5"/>')
        p.append(f'<text x="{pad+bw+5}" y="{y+11}">{v:.2f}</text>')
    if ceiling:
        cx = pad + int((w - pad - 60) * ceiling / mx)
        p.append(f'<line x1="{cx}" y1="28" x2="{cx}" y2="{h-16}" '
                 f'stroke="#c33" stroke-dasharray="4,3"/>')
        p.append(f'<text x="{cx+4}" y="{h-4}" fill="#c33">ceiling {ceiling}</text>')
    p.append('</svg>')
    open(path, 'w').write('\n'.join(p))
    return path


def _svg_grouped(title, rows, series, path, ceiling=None):
    """Grouped horizontal bars: `series` is [(label, key, colour)]."""
    def fv(r, k):
        try: return float(r.get(k) or 0)
        except ValueError: return 0.0
    n, k = len(rows), len(series)
    bh, pad, w = 7, 150, 760
    h = 52 + n * (bh * k + 4) + 24
    mx = max([fv(r, key) for r in rows for _l, key, _c in series]
             + ([ceiling] if ceiling else [0])) or 1
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
         f'font-family="monospace" font-size="10">',
         f'<text x="8" y="16" font-size="13">{title}</text>']
    for i, (lab, _key, col) in enumerate(series):
        p.append(f'<rect x="{8+i*130}" y="24" width="10" height="9" fill="{col}"/>')
        p.append(f'<text x="{22+i*130}" y="33">{lab}</text>')
    for i, r in enumerate(rows):
        y0 = 46 + i * (bh * k + 4)
        p.append(f'<text x="8" y="{y0+bh}">{str(r.get("benchmark",""))[:22]}</text>')
        for j, (_l, key, col) in enumerate(series):
            v = fv(r, key)
            bw = int((w - pad - 55) * v / mx)
            yy = y0 + j * bh
            p.append(f'<rect x="{pad}" y="{yy}" width="{bw}" height="{bh-1}" fill="{col}"/>')
            p.append(f'<text x="{pad+bw+4}" y="{yy+bh-1}">{v:.2f}</text>')
    if ceiling:
        cx = pad + int((w - pad - 55) * ceiling / mx)
        p.append(f'<line x1="{cx}" y1="40" x2="{cx}" y2="{h-18}" stroke="#c33" '
                 f'stroke-dasharray="4,3"/>')
        p.append(f'<text x="{cx+4}" y="{h-6}" fill="#c33">issue width {ceiling}</text>')
    p.append('</svg>')
    open(path, 'w').write('\n'.join(p))
    return path


def emit(rows, out_dir=None):
    out_dir = out_dir or os.path.join(_HERE, 'plots')
    os.makedirs(out_dir, exist_ok=True)
    made = []
    def fv(r, k):
        try: return float(r.get(k) or 0)
        except ValueError: return 0.0
    # 1. IPB per benchmark, scalar vs vector, against the 8-wide ceiling
    made.append(_svg_bars('Vector IPB per benchmark (ceiling = 8 issue slots)',
                          [(r['benchmark'], fv(r, 'vector_ipb')) for r in rows],
                          os.path.join(out_dir, 'ipb_per_benchmark.svg'), CEIL))
    # 2. gap causes
    ranked, _d, _t = compare.classify_gap(rows)
    made.append(_svg_bars('Lost issue slots by measured cause (%)',
                          [(b, pct) for b, _v, pct in ranked],
                          os.path.join(out_dir, 'gap_causes.svg')))
    # 3. ASCII distribution
    txt = ['Vector-compiler IPB distribution', '']
    for lab, n in compare.distribution(rows, 'vector_ipb'):
        txt.append(f"{lab:>10} | {'#' * n} {n}")
    txt += ['', 'Scalar-compiler IPB distribution', '']
    for lab, n in compare.distribution(rows, 'scalar_ipb'):
        txt.append(f"{lab:>10} | {'#' * n} {n}")
    p = os.path.join(out_dir, 'ipb_distribution.txt')
    open(p, 'w').write('\n'.join(txt))
    made.append(p)
    # 4. coverage
    cov = compare.coverage(rows)
    made.append(_svg_bars('Coverage (26 benchmarks)',
                          [(k, float(cov.get(k, 0))) for k in
                           ('vectorized', 'rejected', 'rolled-back',
                            'scalar-family')],
                          os.path.join(out_dir, 'coverage.svg')))
    # 5. dynamic-operation reduction per benchmark
    vec = [r for r in rows if r['decision'] == 'vectorized']
    made.append(_svg_bars('Dynamic operation reduction (%) -- vectorized kernels',
                          [(r['benchmark'], fv(r, 'dynamic_reduction'))
                           for r in vec],
                          os.path.join(out_dir, 'dynamic_reduction.svg'), 100.0))
    # 6. IPB comparison, scalar vs vector, against the 8-wide ceiling
    made.append(_svg_grouped('IPB: scalar vs vector compiler',
                             rows, [('scalar', 'scalar_ipb', '#999'),
                                    ('vector', 'vector_ipb', '#4a7fb5')],
                             os.path.join(out_dir, 'ipb_scalar_vs_vector.svg'),
                             CEIL))
    # 7. oracle comparison
    made.append(_svg_grouped('Achieved vector IPB vs oracle theoretical ceiling',
                             rows, [('vector', 'vector_ipb', '#4a7fb5'),
                                    ('oracle', 'theoretical_ipb', '#c9a227')],
                             os.path.join(out_dir, 'oracle_comparison.svg'),
                             CEIL))
    return made

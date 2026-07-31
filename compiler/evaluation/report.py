"""report.py -- renders the R4.6.5 evaluation as text + CSV summaries."""
import os, sys, csv
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import compare


def _tbl(rows, cols, widths):
    out = ['  ' + ''.join(c.ljust(w) for c, w in zip(cols, widths))]
    for r in rows:
        out.append('  ' + ''.join(str(r.get(c, ''))[:w-1].ljust(w)
                                  for c, w in zip(cols, widths)))
    return '\n'.join(out)


def render(rows):
    L = []
    A = L.append
    A('=' * 78)
    A('  R4.6.5 VECTOR PERFORMANCE CHARACTERIZATION')
    A('=' * 78)
    A('')
    A('  Per-benchmark measurements (production path: tier-1 + superblock)')
    A(_tbl(rows, ['benchmark', 'decision', 'realisation', 'scalar_ipb',
                  'vector_ipb', 'theoretical_ipb', 'dynamic_reduction'],
           [20, 13, 17, 11, 12, 17, 10]))
    A('')
    vec = [r for r in rows if r['decision'] == 'vectorized']
    sca = [r for r in rows if r['family'] == 'scalar']
    A('  IPB (static instructions / static bundle; 8 = issue-width ceiling)')
    for label, subset, key in (('scalar compiler (all)', rows, 'scalar_ipb'),
                               ('vector compiler (all)', rows, 'vector_ipb'),
                               ('vector compiler (vectorized only)', vec, 'vector_ipb'),
                               ('non-vectorizable kernels', sca, 'vector_ipb'),
                               ('oracle theoretical', rows, 'theoretical_ipb')):
        s = compare.ipb_stats(subset, key)
        if s:
            A(f"    {label:36} mean {s['mean']:<6} peak {s['peak']:<6} "
              f"min {s['minimum']:<6} median {s['median']:<6} n={s['n']}")
    A('')
    A('  Vector-compiler IPB distribution')
    for lab, n in compare.distribution(rows, 'vector_ipb'):
        A(f"    {lab:>10} | {'#' * n} {n}")
    A('')
    cov = compare.coverage(rows)
    A('  Coverage')
    for k in ('vectorized', 'rejected', 'rolled-back', 'accepted', 'scalar-family'):
        A(f"    {k:16}: {cov.get(k, 0)}")
    A(f"    total benchmarks: {len(rows)}")
    A('')
    ranked, detail, total = compare.classify_gap(rows)
    A('  Gap to oracle -- lost issue slots by measured cause')
    A(f"    total lost slots (theoretical - achieved, weighted by bundles): {total:.0f}")
    for b, v, pct in ranked:
        A(f"    {b:24} {v:9.0f}  {pct:5.1f}%  {'#' * int(pct / 2)}")
    A('')
    return '\n'.join(L)


def write_summaries(rows, out_dir=None):
    out_dir = out_dir or os.path.join(_HERE, 'results')
    os.makedirs(out_dir, exist_ok=True)
    # per-family summary
    fams = {}
    for r in rows:
        f = r['family']
        d = fams.setdefault(f, dict(family=f, n=0, vectorized=0,
                                    scalar_ipb=0.0, vector_ipb=0.0,
                                    theoretical_ipb=0.0, dyn_red=0.0))
        d['n'] += 1
        d['vectorized'] += (r['decision'] == 'vectorized')
        for k, src in (('scalar_ipb', 'scalar_ipb'), ('vector_ipb', 'vector_ipb'),
                       ('theoretical_ipb', 'theoretical_ipb'),
                       ('dyn_red', 'dynamic_reduction')):
            try:
                d[k] += float(r.get(src) or 0)
            except ValueError:
                pass
    for d in fams.values():
        for k in ('scalar_ipb', 'vector_ipb', 'theoretical_ipb', 'dyn_red'):
            d[k] = round(d[k] / max(1, d['n']), 3)
    p1 = os.path.join(out_dir, 'summary_by_family.csv')
    with open(p1, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(next(iter(fams.values())).keys()))
        w.writeheader(); w.writerows(fams.values())
    # gap CSV
    ranked, detail, total = compare.classify_gap(rows)
    p2 = os.path.join(out_dir, 'gap_analysis.csv')
    with open(p2, 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['cause', 'lost_slots', 'percent'])
        for b, v, pct in ranked:
            w.writerow([b, round(v, 1), round(pct, 1)])
    p3 = os.path.join(out_dir, 'gap_detail.csv')
    with open(p3, 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['benchmark', 'cause', 'lost_slots'])
        w.writerows(detail)
    return [p1, p2, p3]

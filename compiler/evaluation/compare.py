"""compare.py -- scalar vs vector vs oracle, and the gap classification."""
import os, sys, csv, statistics as st
_HERE = os.path.dirname(os.path.abspath(__file__))

# Where a lost issue slot goes. Every bucket is populated from a MEASURED field,
# never from an assumption: the oracle's own limiter/opportunity classification
# (R3.0) for vectorized-or-scalar loops, and the vector pipeline's recorded
# decision/reason for anything it declined.
GAP_BUCKETS = ('true data dependence', 'memory dependence', 'register pressure',
               'branch overhead', 'remainder handling', 'non-vectorized loop',
               'unsupported pattern', 'hardware restriction')

_LIMITER_TO_BUCKET = {
    'recurrence-memory': 'memory dependence',
    'recurrence-register': 'true data dependence',
    'memory-bound': 'memory dependence',
    'dependency-bound': 'true data dependence',
    'control-bound': 'branch overhead',
    'resource-divide': 'hardware restriction',
    'resource-width': 'hardware restriction',
    'resource-bound': 'hardware restriction',
    'mixed': 'true data dependence',
}

_REASON_TO_BUCKET = (
    ('unprofitable',            'non-vectorized loop'),
    ('trip-too-small',          'remainder handling'),
    ('iv-does-not-start',       'unsupported pattern'),
    ('unpacked',                'hardware restriction'),
    ('contiguous-store',        'unsupported pattern'),
    ('expression-too-deep',     'unsupported pattern'),
    ('unsupported-operator',    'unsupported pattern'),
    ('value-shape',             'unsupported pattern'),
    ('not-recognised',          'non-vectorized loop'),
    ('isa-unsupported',         'hardware restriction'),
    ('no-realisation',          'register pressure'),
    ('differential',            'unsupported pattern'),
    ('aliasing',                'memory dependence'),
)


def load(path=None):
    path = path or os.path.join(_HERE, 'results', 'benchmarks.csv')
    with open(path) as f:
        return list(csv.DictReader(f))


def _f(row, k, d=0.0):
    try:
        return float(row.get(k) or d)
    except ValueError:
        return d


def ipb_stats(rows, key):
    vals = [_f(r, key) for r in rows if _f(r, key) > 0]
    if not vals:
        return {}
    return dict(mean=round(st.mean(vals), 3), peak=round(max(vals), 3),
                minimum=round(min(vals), 3),
                median=round(st.median(vals), 3), n=len(vals))


def distribution(rows, key, edges=(1.0, 1.5, 2.0, 2.5, 3.0, 4.0)):
    vals = [_f(r, key) for r in rows if _f(r, key) > 0]
    out, prev = [], 0.0
    for e in edges:
        out.append((f"{prev:.1f}-{e:.1f}", sum(1 for v in vals if prev <= v < e)))
        prev = e
    out.append((f">={prev:.1f}", sum(1 for v in vals if v >= prev)))
    return out


def coverage(rows):
    c = {'vectorized': 0, 'rejected': 0, 'rolled-back': 0, 'scalar-family': 0}
    for r in rows:
        if r['family'] == 'scalar':
            c['scalar-family'] += 1
        c[r['decision']] = c.get(r['decision'], 0) + 1
    c['accepted'] = c['vectorized']
    return c


def classify_gap(rows):
    """Attribute each benchmark's remaining IPB gap to ONE measured cause, and
    weight it by the slots actually lost (theoretical - achieved) * bundles."""
    buckets = {b: 0.0 for b in GAP_BUCKETS}
    detail = []
    for r in rows:
        theo = _f(r, 'theoretical_ipb')
        ach = _f(r, 'vector_ipb')
        if theo <= 0:
            continue
        lost = max(0.0, theo - ach) * _f(r, 'static_bundles')
        if r['decision'] == 'vectorized':
            bucket = _LIMITER_TO_BUCKET.get(str(r.get('limiter', '')).strip(),
                                            'true data dependence')
            if _f(r, 'dynamic_reduction') and 'peeled' not in r['realisation'] \
                    and 'remainder' in r['benchmark']:
                bucket = 'remainder handling'
        else:
            reason = str(r.get('reason', ''))
            bucket = 'non-vectorized loop'
            for key, b in _REASON_TO_BUCKET:
                if key in reason:
                    bucket = b
                    break
        buckets[bucket] += lost
        detail.append((r['benchmark'], bucket, round(lost, 1)))
    total = sum(buckets.values()) or 1.0
    ranked = sorted(((b, v, 100.0 * v / total) for b, v in buckets.items()
                     if v > 0), key=lambda x: -x[1])
    return ranked, detail, total

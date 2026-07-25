"""
m11_eval.py -- Milestone M11: quantitative evaluation of the loop-opt framework.

EVALUATION ONLY. This harness OBSERVES the frozen production compiler; it changes
no pass, order, heuristic, schedule, bundling or codegen. It compiles the whole
benchmark corpus, measures per-program bundle/IPB/size/dependency metrics,
attributes the contribution of each loop optimization, classifies the dominant
bundling bottleneck from the bundler's own split-reason instrumentation, times the
framework vs the legacy passes, and emits a machine-readable results JSON plus a
text report. loopopt/m11_report.py turns the JSON into a thesis-quality figure set.

Metrics per program are computed on the generated machine code:
  * static instruction count      = non-$null machine instructions
  * bundles                       = VLIW bundles after scheduling+packing
  * IPB                           = static / bundles  (Instructions Per Bundle)
  * occupancy                     = per-bundle lane fill (max 8), for the histogram
  * load/store count              = $ld / $st machine instructions
  * split reasons (bottleneck)    = the bundler's own per-rejection taxonomy
                                    (RAW/WAW=reg deps, MemAlias/MemPhase=mem deps,
                                     MemLane/FUnit=hw lanes, Control/Call/Label=
                                     structural, BundleFull=ILP saturation)
  * compile time                  = wall time of loop-opt -> codegen -> bundle
  * verifier/rollback activity    = framework TransformStats

Pass contribution is an ABLATION on each program's strength-reduced baseline
(compiler.py's own fallback IR): baseline vs +Rotation / +LICM / +IVSR alone, plus
the true production Final tier. Framework overhead times the framework passes
against the legacy specs (byte-identical output, proven M8/M9/M10).

Run:  python3 compiler/loopopt/m11_eval.py            (whole corpus -> JSON+report)
      python3 compiler/loopopt/m11_eval.py --limit 8  (quick subset)
"""

import os
import sys
import io
import re
import glob
import json
import time
import contextlib
import statistics as st

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_COMPILER)
sys.path.insert(0, _COMPILER)

import pycparser                                                    # noqa: E402
from compiler import preprocess, _FAKE_TYPEDEFS                     # noqa: E402
from ir import Temp                                                 # noqa: E402
from ir_gen import IRGenerator                                      # noqa: E402
from codegen import CodeGen                                         # noqa: E402
import bundler                                                      # noqa: E402
import ivsr                                                         # noqa: E402
import licm2                                                        # noqa: E402
import mem2reg as _mem2reg_mod                                      # noqa: E402
import loop_reg as _loop_reg_mod                                    # noqa: E402
from strength_reduce import strength_reduce                         # noqa: E402
from loopopt.loop_ivsr import ivsr_module                           # noqa: E402
from loopopt.loop_licm import licm_module                          # noqa: E402
from loopopt.rotate import rotate_module                            # noqa: E402
from loopopt.canonicalize import LoopCanonicalizer                  # noqa: E402
from loopopt import discover                                        # noqa: E402
from loopopt.analysis_profile import annotate_profile               # noqa: E402
from loopopt.analysis_iv import annotate_induction_vars             # noqa: E402

_GB = 0x400
_STACK = 0x7FF8

_SPLIT_LEGEND = {
    'RAW': 'true data dependency', 'WAW': 'output dependency',
    'MemAlias': 'memory dependency (aliasing)', 'MemPhase': 'memory phase hazard',
    'MemLane': 'load/store lane limit (hw)', 'FUnit': 'divide/sqrt lane limit (hw)',
    'Control': 'control transfer', 'Call': 'call/SP hazard', 'Label': 'basic-block boundary',
    'BundleFull': 'bundle full (ILP saturation)', 'Other': 'other',
}
# map a split reason to the milestone's bottleneck categories
_BOTTLENECK = {
    'RAW': 'true-data-deps', 'WAW': 'true-data-deps',
    'MemAlias': 'memory/aliasing', 'MemPhase': 'memory/aliasing',
    'MemLane': 'hw-resource', 'FUnit': 'hw-resource', 'BundleFull': 'ilp-saturation',
    'Control': 'branch/control-flow', 'Call': 'branch/control-flow',
    'Label': 'branch/control-flow', 'Other': 'other',
}


# ── corpus ────────────────────────────────────────────────────────────────────

def corpus_files():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    return sorted(set(files))


def gen_ir(f):
    try:
        src, _ = preprocess(f)
        ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
        Temp.reset()
        g = IRGenerator(global_base=_GB)
        g.visit(ast)
        return g.instructions
    except Exception:
        return None


# ── pipeline reconstruction (mirrors compiler.py, order preserved) ────────────

def _reset_counters():
    ivsr._iv_n[0] = 0
    _mem2reg_mod._m2r_n[0] = 0
    _loop_reg_mod._lr_n = 0


def _sr(x):
    return strength_reduce(x)[0]


def _clean(x):
    from copyprop import copy_propagate
    from coalesce import copy_coalesce
    from dce import dead_code_eliminate
    return dead_code_eliminate(copy_coalesce(copy_propagate(x)))


def _cp(x, licm_on=False):
    """compiler.py's scalar-cleanup stage. `licm_on` mirrors the APARA_LICM gate
    on the opt-in LICM that lives inside this stage in production."""
    from dce import dead_code_eliminate
    from sccp import sparse_conditional_constant_propagation
    from gvn import global_value_numbering
    from mem2reg import mem2reg
    x = _clean(x)
    x = dead_code_eliminate(sparse_conditional_constant_propagation(x))
    x = global_value_numbering(x)
    x = mem2reg(x)
    if licm_on:
        w = list(x); licm_module(w); x = w
    x = _clean(x)
    return x


def _codegen_body(instrs):
    """(spilled, body_mcode) or (True, None) on codegen failure (register
    exhaustion), matching compiler.py's tier handling."""
    try:
        cg = CodeGen(global_base=_GB)
        body = cg.generate(instrs, global_base=_GB)
        return cg.spilled, body
    except Exception:
        return True, None


_SPLIT_RE = re.compile(r'^\s+(\w+)\s+(\d+)\s+[\d.]+%')


def _bundle_metrics(body_mcode):
    """Bundle the machine code and return
    (static, bundles, ipb, occupancy_list, ls_count, split_reasons_dict).
    Uses the bundler's OWN scheduler/packer and split-reason instrumentation
    (ground truth); nothing here changes the bundles."""
    header, flat = bundler._parse_flat(body_mcode)
    flat = bundler._schedule_within_blocks(flat)
    buf = io.StringIO()
    os.environ['APARA_BUNDLE_STATS'] = 'all'
    try:
        with contextlib.redirect_stderr(buf):
            bundles = bundler._pack_bundles(flat)
    finally:
        os.environ.pop('APARA_BUNDLE_STATS', None)
    bundles = bundler._merge_duplicate_labels(bundles)

    occ = [len(b['instrs']) for b in bundles if b['instrs']]
    static = sum(occ)
    nb = len(bundles)
    ipb = (static / nb) if nb else 0.0
    ls = sum(1 for b in bundles for t in b['instrs']
             if t.startswith('$ld') or t.startswith('$st'))

    reasons, in_global = {}, False
    for line in buf.getvalue().splitlines():
        if 'SPLIT REASONS (global)' in line:
            in_global = True
            continue
        if in_global:
            m = _SPLIT_RE.match(line)
            if m:
                reasons[m.group(1)] = int(m.group(2))
    return static, nb, ipb, occ, ls, reasons


def _full_mcode(instrs):
    """Reproduce compiler.py's backend for a selected IR: codegen body + startup
    header (so the static/bundle counts match production's `bundles: X -> Y`)."""
    cg = CodeGen(global_base=_GB)
    body = cg.generate(instrs, global_base=_GB)
    header = cg.startup_code(global_base=_GB, stack_top=_STACK)
    return header + body, cg.spilled


# ── production tier build (framework passes; compiler.py order) ───────────────

def _build_final(ir0):
    """Build the production loop-opt tiers (framework passes, order preserved) and
    return (selected_name, selected_IR, spilled, tier_records). Mirrors
    compiler.py: try most-optimized tier first, step down until one codegens
    without spilling; else the strength-reduced baseline."""
    _reset_counters()

    def _ivsr(x):
        r, _s, _rep = ivsr_module(list(x))
        return r

    licm_on = bool(os.environ.get('APARA_LICM'))
    from licm import hoist_loop_invariants
    from loop_reg import promote_loop_counters
    base = _sr(list(ir0))
    tiers = [
        ("IVSR+LICM+loop-reg", lambda: _cp(promote_loop_counters(hoist_loop_invariants(_sr(_ivsr(list(ir0))))), licm_on)),
        ("IVSR+loop-reg",      lambda: _cp(promote_loop_counters(_sr(_ivsr(list(ir0)))), licm_on)),
        ("IVSR only",          lambda: _cp(_sr(_ivsr(list(ir0))), licm_on)),
        ("LICM+loop-reg",      lambda: _cp(promote_loop_counters(hoist_loop_invariants(list(base))), licm_on)),
        ("LICM only",          lambda: _cp(hoist_loop_invariants(list(base)), licm_on)),
        ("loop-reg only",      lambda: _cp(promote_loop_counters(list(base)), licm_on)),
    ]
    for name, build in tiers:
        try:
            instrs = build()
            sp, _b = _codegen_body(instrs)
            if not sp and _b is not None:
                return name, instrs, False, base
        except Exception:
            continue
    return "baseline (SR-only)", base, True, base


# ── per-program measurement ───────────────────────────────────────────────────

def measure_program(f):
    ir0 = gen_ir(f)
    if ir0 is None:
        return None
    rel = os.path.relpath(f, _ROOT)

    # ---- loop / register-pressure facts, measured FIRST on the pristine IR ----
    # (later passes mutate shared instruction objects in place, which would corrupt
    #  loop structure if discovered afterward).
    n_loops = 0
    reg_peaks = []
    mii_facts = []
    try:
        import copy as _copy
        descs = discover(_copy.deepcopy(ir0))
        n_loops = len(descs)
        annotate_induction_vars(descs)
        annotate_profile(descs)
        for d in descs:
            if d.profile_analyzed:
                reg_peaks.append(d.reg_pressure_peak)
                mii_facts.append({'mii': d.mii, 'rec_mii': d.rec_mii,
                                  'res_mii': d.res_mii, 'ipb_ceiling': d.est_ipb,
                                  'crit': d.crit_path_true, 'n': d.body_inst_count})
    except Exception:
        pass

    # ---- production final IR + core metrics + compile time ----
    t0 = time.perf_counter()
    sel_name, final_ir, fell_back, base_ir = _build_final(ir0)
    body, body_sp = _full_mcode(final_ir)
    compile_ms = (time.perf_counter() - t0) * 1000.0
    if body is None:
        return None
    static, nb, ipb, occ, ls, reasons = _bundle_metrics(body)

    # dominant bottleneck (bundler ground truth): most frequent split reason,
    # mapped to a milestone category.
    dom_reason = max(reasons, key=reasons.get) if reasons else None
    dom_cat = _BOTTLENECK.get(dom_reason, 'none') if dom_reason else 'none'
    cat_hist = {}
    for r, n in reasons.items():
        cat_hist[_BOTTLENECK.get(r, 'other')] = cat_hist.get(_BOTTLENECK.get(r, 'other'), 0) + n

    # ---- pass-contribution ablation ----
    # Each variant is a FULL pipeline (through the identical _cp cleanup stage that
    # production always runs) differing only in the one loop pass under test, so
    # instruction/bundle counts are directly comparable and each pass's real
    # end-to-end effect is isolated (IVSR's benefit arrives via its DCE + cleanup;
    # LICM lives inside _cp in production and is measured with its gate on;
    # Rotation is NOT in the production pipeline and is measured standalone for the
    # progression the milestone requests). All variants codegen+bundle the SAME way.
    def _variant_metrics(instrs):
        sp, b = _codegen_body(instrs)
        if b is None:
            return None
        s, nn, ii, _o, _ls, _r = _bundle_metrics(b)
        return {'static': s, 'bundles': nn, 'ipb': ii, 'spilled': sp}

    _reset_counters()
    base_cp = _cp(_sr(list(ir0)), licm_on=False)          # no loop opt
    base_m = _variant_metrics(base_cp)
    # +Rotation: rotate the strength-reduced IR, then the same cleanup
    _reset_counters()
    rot_stats = None
    try:
        rir = _sr(list(ir0))
        LoopCanonicalizer().canonicalize(rir)
        rot_stats, _ = rotate_module(rir)
        rot_cp = _cp(rir, licm_on=False)
    except Exception:
        rot_cp = base_cp
    rot_m = _variant_metrics(rot_cp)
    # +LICM: cleanup with the opt-in LICM enabled (its production position)
    _reset_counters()
    licm_cp = _cp(_sr(list(ir0)), licm_on=True)
    lic_stats = None
    try:
        probe = _sr(list(ir0)); lic_stats, _ = licm_module(probe)
    except Exception:
        pass
    licm_m = _variant_metrics(licm_cp)
    # +IVSR: IVSR (framework) on the pristine IR, then SR + the same cleanup
    _reset_counters()
    ivsr_stats = None
    try:
        r, ivsr_stats, _ = ivsr_module(list(ir0))
        ivsr_cp = _cp(_sr(r), licm_on=False)
    except Exception:
        ivsr_cp = base_cp
    ivsr_m = _variant_metrics(ivsr_cp)

    # ---- framework overhead: framework vs legacy pass wall time ----
    def _time(fn, reps=3):
        best = float('inf')
        for _ in range(reps):
            t = time.perf_counter(); fn(); best = min(best, time.perf_counter() - t)
        return best * 1000.0

    _reset_counters()
    t_fw_ivsr = _time(lambda: ivsr_module(list(ir0)))
    ivsr._iv_n[0] = 0
    t_lg_ivsr = _time(lambda: ivsr.induction_strength_reduce(list(ir0)))
    # LICM overhead measured with the gate on (else both are no-ops)
    t_fw_licm = _time(lambda: licm_module(list(base_ir)))
    prev = os.environ.get('APARA_LICM'); os.environ['APARA_LICM'] = '1'
    t_lg_licm = _time(lambda: licm2.loop_invariant_code_motion(list(base_ir)))
    if prev is None:
        os.environ.pop('APARA_LICM', None)

    # framework activity (attempts/commits/rollbacks/verifier) from IVSR stats
    act = {'attempts': 0, 'commits': 0, 'rollbacks': 0, 'verifier_failures': 0}
    for s in (ivsr_stats, lic_stats):
        if s is not None:
            act['attempts'] += s.attempts
            act['commits'] += s.commits
            act['rollbacks'] += s.rollbacks
            act['verifier_failures'] += s.verifier_failures

    return {
        'file': rel,
        'selected_tier': sel_name,
        'fell_back': fell_back,
        'static': static, 'bundles': nb, 'ipb': ipb,
        'occupancy': occ, 'load_store': ls,
        'compile_ms': compile_ms,
        'split_reasons': reasons,
        'bottleneck_cat_hist': cat_hist,
        'dominant_bottleneck': dom_cat,
        'contribution': {
            'baseline': base_m, 'rotation': rot_m, 'licm': licm_m, 'ivsr': ivsr_m,
            # body-only (like the ablation variants) so the comparison to baseline
            # is apples-to-apples (the startup header is optimization-invariant).
            'final': _variant_metrics(final_ir),
        },
        'overhead_ms': {'fw_ivsr': t_fw_ivsr, 'legacy_ivsr': t_lg_ivsr,
                        'fw_licm': t_fw_licm, 'legacy_licm': t_lg_licm},
        'activity': act,
        'n_loops': n_loops,
        'reg_pressure_peak': (max(reg_peaks) if reg_peaks else None),
        'mii_facts': mii_facts,
    }


# ── aggregate + report ────────────────────────────────────────────────────────

def _summary(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {
        'min': min(vals), 'max': max(vals),
        'avg': sum(vals) / len(vals),
        'median': st.median(vals),
        'stdev': (st.pstdev(vals) if len(vals) > 1 else 0.0),
        'n': len(vals),
    }


def _fmt(s, k='avg'):
    return f"{s[k]:.2f}" if s else "n/a"


def build_report(rows):
    R = [r for r in rows if r]
    out = []
    out.append("=" * 78)
    out.append("  M11 -- LOOP-OPT FRAMEWORK QUANTITATIVE EVALUATION")
    out.append("=" * 78)
    out.append(f"  programs measured: {len(R)}")

    # corpus-wide stats
    ipb = _summary([r['ipb'] for r in R])
    nb = _summary([r['bundles'] for r in R])
    stat = _summary([r['static'] for r in R])
    ls = _summary([r['load_store'] for r in R])
    cms = _summary([r['compile_ms'] for r in R])
    allocc = [o for r in R for o in r['occupancy']]
    occ = _summary(allocc)
    out.append("")
    out.append("  CORPUS-WIDE STATISTICS (min / max / avg / median / stdev)")
    for name, s in (("IPB", ipb), ("bundles", nb), ("static instrs", stat),
                    ("load/store", ls), ("bundle occupancy (lanes)", occ),
                    ("compile ms", cms)):
        if s:
            out.append(f"    {name:<26} {s['min']:8.2f} {s['max']:9.2f} "
                       f"{s['avg']:8.2f} {s['median']:8.2f} {s['stdev']:8.2f}")

    # occupancy histogram
    out.append("")
    out.append("  BUNDLE OCCUPANCY HISTOGRAM (lanes used, max 8)")
    hist = {i: 0 for i in range(1, 9)}
    for o in allocc:
        hist[min(o, 8)] = hist.get(min(o, 8), 0) + 1
    tot = sum(hist.values()) or 1
    for lanes in range(1, 9):
        c = hist[lanes]
        bar = '#' * int(40 * c / tot)
        out.append(f"    {lanes} lane(s): {c:6d} ({100*c/tot:5.1f}%) {bar}")

    # verifier / rollback / correctness
    tot_att = sum(r['activity']['attempts'] for r in R)
    tot_com = sum(r['activity']['commits'] for r in R)
    tot_rb = sum(r['activity']['rollbacks'] for r in R)
    tot_vf = sum(r['activity']['verifier_failures'] for r in R)
    out.append("")
    out.append("  FRAMEWORK ACTIVITY / CORRECTNESS")
    out.append(f"    transform attempts     : {tot_att}")
    out.append(f"    commits (transforms)   : {tot_com}")
    out.append(f"    rollbacks              : {tot_rb}")
    out.append(f"    verifier failures      : {tot_vf}")
    out.append(f"    programs fell back      : {sum(1 for r in R if r['fell_back'])}")

    # pass contribution (aggregate: mean reduction vs baseline, over programs
    # where the pass changed anything)
    out.append("")
    out.append("  PASS CONTRIBUTION (vs strength-reduced baseline; averaged over "
               "programs the pass changed)")
    out.append(f"    {'pass':<12} {'progs':>6} {'d.static%':>10} {'d.bundles%':>11} {'d.IPB%':>9}")
    for key in ('rotation', 'licm', 'ivsr', 'final'):
        ds = []; db = []; di = []; n = 0
        for r in R:
            b = r['contribution']['baseline']; v = r['contribution'][key]
            if not b or not v:
                continue
            changed = (v['static'] != b['static'] or v['bundles'] != b['bundles'])
            if key != 'final' and not changed:
                continue
            n += 1
            if b['static']:
                ds.append(100.0 * (b['static'] - v['static']) / b['static'])
            if b['bundles']:
                db.append(100.0 * (b['bundles'] - v['bundles']) / b['bundles'])
            if b['ipb']:
                di.append(100.0 * (v['ipb'] - b['ipb']) / b['ipb'])
        out.append(f"    {key:<12} {n:>6} {(_summary(ds) or {}).get('avg', 0):>10.2f} "
                   f"{(_summary(db) or {}).get('avg', 0):>11.2f} "
                   f"{(_summary(di) or {}).get('avg', 0):>9.2f}")

    # bottleneck distribution (corpus-wide, weighted by split events)
    out.append("")
    out.append("  BOTTLENECK DISTRIBUTION (share of all bundle-split events)")
    cat_tot = {}
    for r in R:
        for c, n in r['bottleneck_cat_hist'].items():
            cat_tot[c] = cat_tot.get(c, 0) + n
    gtot = sum(cat_tot.values()) or 1
    for c, n in sorted(cat_tot.items(), key=lambda kv: -kv[1]):
        bar = '#' * int(40 * n / gtot)
        out.append(f"    {c:<22} {n:8d} ({100*n/gtot:5.1f}%) {bar}")
    out.append("")
    out.append("  DOMINANT BOTTLENECK per program (count of programs)")
    dom = {}
    for r in R:
        dom[r['dominant_bottleneck']] = dom.get(r['dominant_bottleneck'], 0) + 1
    for c, n in sorted(dom.items(), key=lambda kv: -kv[1]):
        out.append(f"    {c:<22} {n:6d} programs")

    # framework overhead
    out.append("")
    out.append("  FRAMEWORK OVERHEAD (best-of-3 wall time, ms; framework vs legacy)")
    fi = _summary([r['overhead_ms']['fw_ivsr'] for r in R])
    li = _summary([r['overhead_ms']['legacy_ivsr'] for r in R])
    fl = _summary([r['overhead_ms']['fw_licm'] for r in R])
    ll = _summary([r['overhead_ms']['legacy_licm'] for r in R])
    tfi = sum(r['overhead_ms']['fw_ivsr'] for r in R)
    tli = sum(r['overhead_ms']['legacy_ivsr'] for r in R)
    tfl = sum(r['overhead_ms']['fw_licm'] for r in R)
    tll = sum(r['overhead_ms']['legacy_licm'] for r in R)
    out.append(f"    IVSR  framework total {tfi:8.1f} ms   legacy total {tli:8.1f} ms   "
               f"ratio {tfi/tli if tli else 0:.2f}x")
    out.append(f"    LICM  framework total {tfl:8.1f} ms   legacy total {tll:8.1f} ms   "
               f"ratio {tfl/tll if tll else 0:.2f}x")
    out.append(f"    IVSR  per-program avg framework {_fmt(fi)} ms vs legacy {_fmt(li)} ms")
    out.append(f"    LICM  per-program avg framework {_fmt(fl)} ms vs legacy {_fmt(ll)} ms")

    # scaling
    out.append("")
    out.append("  SCALABILITY (compile time by program size, static instrs)")
    buckets = {'small (<50)': [], 'medium (50-200)': [], 'large (>200)': []}
    for r in R:
        k = 'small (<50)' if r['static'] < 50 else ('medium (50-200)' if r['static'] <= 200 else 'large (>200)')
        buckets[k].append(r)
    for k, rs in buckets.items():
        if not rs:
            out.append(f"    {k:<18} n=0")
            continue
        cs = _summary([x['compile_ms'] for x in rs])
        ib = _summary([x['ipb'] for x in rs])
        out.append(f"    {k:<18} n={len(rs):3d}  compile avg {cs['avg']:7.1f} ms "
                   f"(max {cs['max']:7.1f})  IPB avg {ib['avg']:.2f}")

    out.append("=" * 78)
    return "\n".join(out)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--json', default=os.path.join(_HERE, 'm11_results.json'))
    args = ap.parse_args()

    files = corpus_files()
    if args.limit:
        files = files[:args.limit]
    rows = []
    t0 = time.perf_counter()
    for i, f in enumerate(files):
        r = measure_program(f)
        rows.append(r)
        if (i + 1) % 20 == 0:
            print(f"  ... {i+1}/{len(files)} measured", file=sys.stderr)
    elapsed = time.perf_counter() - t0

    R = [r for r in rows if r]
    with open(args.json, 'w') as fh:
        json.dump({'rows': R, 'harness_elapsed_s': elapsed}, fh)
    print(build_report(rows))
    print(f"\n  [harness wall time {elapsed:.1f}s; results JSON -> {args.json}]")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

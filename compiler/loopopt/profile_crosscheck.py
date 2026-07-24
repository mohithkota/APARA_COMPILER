"""
profile_crosscheck.py -- validate M3 Profile against parallelism_profile.py.

Two independent checks:

(A) FIDELITY of reuse.  Feed the SAME mcode records to (i) parallelism_profile's
    own per-loop analysis and (ii) M3's shared core `profile_from_records`, and
    assert every metric is IDENTICAL. This proves M3 does not reimplement or
    diverge from the profiler -- it computes the exact same mathematics. Expected
    disagreements: ZERO.

(B) IR<->mcode STAGE relationship.  For each loop, compare M3's IR-level metrics
    (from the LoopDescriptor) with parallelism_profile's mcode-level metrics
    (from the compiled .mcode), matched by header label. These are DIFFERENT
    pipeline stages (pre-optimization IR vs post-bundle mcode), so they are not
    expected to be equal; this section quantifies and explains the relationship
    (it is reporting, not a pass/fail gate).

Requires compiling each program to mcode (uses compiler.compile_c_to_mcode into
a temp dir). Usage: python3 compiler/loopopt/profile_crosscheck.py
"""

import os
import sys
import glob

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_COMPILER)
sys.path.insert(0, _COMPILER)

import pycparser                                                   # noqa: E402
from compiler import preprocess, _FAKE_TYPEDEFS                    # noqa: E402
from ir import Temp                                                # noqa: E402
from ir_gen import IRGenerator                                     # noqa: E402
import parallelism_profile as PP                                  # noqa: E402
from loopopt import discover, annotate_profile, profile_from_records  # noqa: E402


def _real_mcode(path):
    return (path.endswith('.mcode') and not path.endswith('.aligned.mcode')
            and not path.endswith('.disass.mcode') and 'backup' not in path
            and 'not_used' not in path)


def _sibling_mcode(cfile):
    """The compiled mcode the toolchain writes next to a .c: <dir>/<base>/<base>.mcode."""
    d = os.path.dirname(cfile)
    base = os.path.splitext(os.path.basename(cfile))[0]
    cand = os.path.join(d, base, base + '.mcode')
    return cand if os.path.exists(cand) else None


# ── (A) fidelity: my core vs parallelism_profile on identical mcode records ────

def fidelity_on_mcode(mcode_text):
    """Return list of (label, agree, mine, theirs) for each loop in an mcode."""
    bundles = PP._parse_bundles(mcode_text)
    loops = PP._find_loops(bundles)
    rows = []
    for (head, back, lbl) in sorted(loops):
        recs = PP._records_for_body(bundles, head, back)
        mine = profile_from_records(recs)
        theirs = {
            'body_inst_count': len(recs),
            'crit_path_height': PP._critical_path(recs, PP._must_precede),
            'crit_path_true': PP._critical_path(recs, PP._true_dep),
            'res_mii': PP._res_mii_detail(recs)[0],
            'rec_mii': PP._rec_mii(recs)[0],
        }
        theirs['mii'] = max(theirs['res_mii'], theirs['rec_mii'])
        agree = all(mine[k] == theirs[k] for k in theirs)
        rows.append((lbl, agree, mine, theirs))
    return rows


# ── (B) IR-level (M3) vs mcode-level (PP) per loop, by label ───────────────────

def ir_metrics(instrs):
    descs = discover(instrs)
    annotate_profile(descs)
    return {d.label(): d for d in descs if d.label()}


def main():
    # (A) fidelity source: every real compiled .mcode already in the repo.
    mcode_files = []
    for p in ('testing/**/*.mcode', 'new_isa_tests/**/*.mcode',
              'demo_prof/**/*.mcode', 'isa_coverage_tests/**/*.mcode',
              'matmul_tests/**/*.mcode'):
        mcode_files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    mcode_files = sorted(f for f in set(mcode_files) if _real_mcode(f))

    fid_loops = fid_agree = 0
    fid_mismatch = []
    for mf in mcode_files:
        try:
            with open(mf) as fh:
                mtext = fh.read()
        except Exception:
            continue
        for lbl, agree, mine, theirs in fidelity_on_mcode(mtext):
            fid_loops += 1
            if agree:
                fid_agree += 1
            else:
                fid_mismatch.append((os.path.relpath(mf, _ROOT), lbl, mine, theirs))

    # (B) stage source: .c with a sibling compiled .mcode -> match by label.
    cfiles = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        cfiles += glob.glob(os.path.join(_ROOT, p), recursive=True)
    stage_pairs = []
    for f in sorted(set(cfiles)):
        mf = _sibling_mcode(f)
        if not mf:
            continue
        try:
            src, _ = preprocess(f)
            ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
            Temp.reset()
            g = IRGenerator(global_base=0x400)
            g.visit(ast)
            irm = ir_metrics(g.instructions)
            rows, _ = PP._analyze(mf)
        except Exception:
            continue
        mrows = {r['lbl']: r for r in rows}
        for lbl, d in irm.items():
            if lbl in mrows:
                stage_pairs.append((lbl, d, mrows[lbl]))

    # ---- report (A) ----
    print("=" * 74)
    print("  (A) FIDELITY  --  M3 profile_from_records  vs  parallelism_profile")
    print("=" * 74)
    print(f"  loops compared (identical mcode records) : {fid_loops}")
    print(f"  IDENTICAL on N/Hnow/Htrue/ResMII/RecMII/MII: {fid_agree}")
    print(f"  MISMATCH                                 : {len(fid_mismatch)}  <- must be 0")
    for fn, lbl, mine, theirs in fid_mismatch[:10]:
        print(f"    {fn} {lbl}: mine={mine} theirs={theirs}")

    # ---- report (B) ----
    print("\n" + "=" * 74)
    print("  (B) STAGE RELATIONSHIP  --  IR-level (M3)  vs  mcode-level (PP)")
    print("     (different pipeline stages; reporting, not a gate)")
    print("=" * 74)
    n = len(stage_pairs)
    if n:
        rec_both = sum(1 for _, d, m in stage_pairs
                       if (d.rec_mii > 1) == (m['rec'] > 1))
        mem_both = sum(1 for _, d, m in stage_pairs
                       if (d.profile_stats['n_mem_ops'] > 0) == (m['n_ls'] > 0))
        ir_n = sum(d.body_inst_count for _, d, _ in stage_pairs) / n
        mc_n = sum(m['N'] for _, _, m in stage_pairs) / n
        print(f"  loops matched by label                   : {n}")
        print(f"  agree on HAS-RECURRENCE (rec>1)          : {rec_both}/{n}"
              f"  ({100*rec_both/n:.0f}%)")
        print(f"  agree on HAS-MEMORY-OPS                  : {mem_both}/{n}"
              f"  ({100*mem_both/n:.0f}%)")
        print(f"  mean instr/iter : IR={ir_n:.1f}  mcode={mc_n:.1f}"
              f"  (IR is pre-IVSR/LICM/loop-reg + pre-bundle)")
    print("=" * 74)
    return 0 if not fid_mismatch else 1


if __name__ == '__main__':
    raise SystemExit(main())

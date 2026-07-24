"""
iv_crosscheck.py -- validate the M1 InductionVars analysis against ivsr.py.

Ground truth = ivsr's OWN basic-IV detection on the pristine IR. ivsr emits
`basic_iv offsets=[...]` per loop under APARA_IVSR_DEBUG; we invoke its internal
`_process_loop` once per loop on the un-mutated IR and capture that. We then run
the M1 analysis on the same IR and compare, per loop, the set of basic-IV slots.

Three outcomes per loop:
  MATCH        : same basic-IV slot set (the expected case for loops ivsr
                 actually analyzes).
  M1-SUPERSET  : ivsr detected nothing because it BAILED (a call / wide store /
                 store-through-computed-address / multi-entry) -- a TRANSFORM
                 legality bail, not an analysis fact. M1 (analysis-only, per-slot
                 cleanness) soundly reports the IV. Reported, not a bug.
  MISMATCH     : any other disagreement -- investigated and must be zero.

Usage: python3 compiler/loopopt/iv_crosscheck.py
"""

import os
import sys
import io
import re
import glob
import contextlib

# ivsr reads APARA_IVSR_DEBUG at import time -- set before importing it.
os.environ['APARA_IVSR_DEBUG'] = '1'

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_COMPILER)
sys.path.insert(0, _COMPILER)

import pycparser                                                   # noqa: E402
from compiler import preprocess, _FAKE_TYPEDEFS                    # noqa: E402
from ir import Temp                                                # noqa: E402
from ir_gen import IRGenerator                                     # noqa: E402
from ir_utils import func_slices, enclosing_slice                  # noqa: E402
from analysis import DefUse                                        # noqa: E402
from licm import _find_loops                                       # noqa: E402
import ivsr                                                        # noqa: E402
from loopopt import discover, annotate_induction_vars             # noqa: E402

_OFFSETS_RE = re.compile(r'loop @(\d+) \((\w+)\): basic_iv offsets=\[([^\]]*)\]')
_NONE_RE = re.compile(r'loop @(\d+): no basic IVs')

# ivsr detection status for a loop, keyed by header label:
#   ('offsets', set) -- ivsr ran detection and found these slots
#   ('none', None)   -- ivsr ran detection and found NO basic IVs
#   (absent)         -- ivsr BAILED before detection (call / computed store / ...)


def _ivsr_truth(instrs):
    """Run ivsr's detection once per loop on the pristine IR. Returns
    {header_label: ('offsets', set) | ('none', None)}; loops where ivsr bailed
    before detection are ABSENT (the 'no debug line at all' case). Distinguishing
    'ran and found none' from 'bailed' is what makes a superset provably a
    legality bail rather than a hidden disagreement."""
    truth = {}
    # ivsr's per-loop debug prints the header label only for the offsets line;
    # the 'no basic IVs' line prints @index. Map index->label to attach it.
    idx_to_label = {}
    for k, ins in enumerate(instrs):
        if type(ins).__name__ == 'IRLabel':
            idx_to_label[k] = ins.name
    bounds = func_slices(instrs)
    for (s, e) in _find_loops(instrs):
        fa, fb = enclosing_slice(bounds, s, e, len(instrs))
        du = DefUse(instrs, fa, fb)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ivsr._process_loop(instrs, s, e, du.single_defs(), du.multi_names(), fa, fb)
        out = buf.getvalue()
        m = _OFFSETS_RE.search(out)
        if m:
            offs = m.group(3).strip()
            truth[m.group(2)] = ('offsets',
                                 set(int(x) for x in offs.split(',')) if offs else set())
            continue
        mn = _NONE_RE.search(out)
        if mn:
            truth[idx_to_label.get(int(mn.group(1)), '?')] = ('none', None)
    return truth


def _m1_result(instrs):
    """{header_label: set(basic-IV slots)} from the M1 analysis."""
    descs = discover(instrs)
    annotate_induction_vars(descs)
    return {d.label(): set(d.basic_ivs.keys()) for d in descs if d.label()}


def main():
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    n_loops = n_match = n_superset = 0
    mismatches = []
    superset_examples = []

    for f in files:
        try:
            src, _ = preprocess(f)
            ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + src, filename=f)
            Temp.reset()
            g = IRGenerator(global_base=0x400)
            g.visit(ast)
            instrs = g.instructions
        except Exception:
            continue

        truth = _ivsr_truth(instrs)
        mine = _m1_result(instrs)

        for label, my_slots in mine.items():
            n_loops += 1
            rec = truth.get(label)          # absent => ivsr BAILED before detection
            if rec is None:
                # ivsr never ran detection (legality bail). A superset here is
                # sound (per-slot cleanness); agreement if I also found nothing.
                if my_slots:
                    n_superset += 1
                    if len(superset_examples) < 10:
                        superset_examples.append(
                            (os.path.relpath(f, _ROOT), label, sorted(my_slots)))
                else:
                    n_match += 1
            else:
                kind, ivsr_slots = rec
                if kind == 'none':
                    ivsr_slots = set()
                # ivsr RAN detection: sets must match exactly, else real mismatch
                if my_slots == ivsr_slots:
                    n_match += 1
                else:
                    mismatches.append((os.path.relpath(f, _ROOT), label,
                                       sorted(ivsr_slots), sorted(my_slots)))

    print("=" * 70)
    print("  M1 InductionVars  vs  ivsr.py  -- CROSS-CHECK")
    print("=" * 70)
    print(f"  loops compared            : {n_loops}")
    print(f"  MATCH (same slot set)     : {n_match}")
    print(f"  M1-SUPERSET (ivsr bailed) : {n_superset}")
    print(f"  MISMATCH                  : {len(mismatches)}")
    if superset_examples:
        print("\n  M1-SUPERSET examples (ivsr bailed on legality; M1 sound):")
        for fn, lbl, slots in superset_examples:
            print(f"    {fn:44s} {lbl:8s} slots={slots}")
    if mismatches:
        print("\n  MISMATCHES (must be explained/fixed):")
        for fn, lbl, iv, mi in mismatches[:30]:
            print(f"    {fn:44s} {lbl:8s} ivsr={iv} m1={mi}")
    print("=" * 70)
    return 0 if not mismatches else 1


if __name__ == '__main__':
    raise SystemExit(main())

"""
mem_crosscheck.py -- validate M2 MemEffects invariance against licm2.py.

Ground truth = the set of instructions licm2 ACTUALLY hoists. licm2 moves (never
copies) invariant instructions to the preheader, so we detect the hoisted set by
OBJECT IDENTITY: run licm2 (APARA_LICM=1) on a copy and see which instruction
objects moved from inside a loop body to before that loop's header.

Soundness property being checked:
    every instruction licm2 hoists must be in M2's invariant_insts.
If licm2 hoists something M2 does not call invariant, that is a real MISS (bug).
The reverse (M2 invariant but not hoisted by licm2) is EXPECTED -- licm2 adds
transform-legality restrictions on top of invariance -- and is categorized by
reason (not a hoistable kind / float / memory load / loop-carried dest / used
after loop / no preheader).

Usage: python3 compiler/loopopt/mem_crosscheck.py
"""

import os
import sys
import glob

os.environ['APARA_LICM'] = '1'          # licm2 is opt-in; enable for ground truth
os.environ.pop('APARA_NO_LICM', None)

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPILER = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_COMPILER)
sys.path.insert(0, _COMPILER)

import pycparser                                                   # noqa: E402
from compiler import preprocess, _FAKE_TYPEDEFS                    # noqa: E402
from ir import Temp                                                # noqa: E402
from ir_gen import IRGenerator                                     # noqa: E402
import licm2                                                       # noqa: E402
from loopopt import discover, annotate_memory_effects             # noqa: E402

_HOIST_KINDS = ('IRBinOp', 'IRUnaryOp', 'IRAssign', 'IRCast',
                'IRLoadAddr', 'IRGlobalAddrOf')


def _cname(x):
    return type(x).__name__


def _licm_hoisted_ids(instrs):
    """Object ids of instructions licm2 moves out of some loop body.

    Strategy: record, per loop, its header object and the object ids in its body.
    Run licm2 on a copy. An instruction is 'hoisted' if it is a body object whose
    new position is BEFORE its loop header's new position (i.e. it moved into the
    preheader)."""
    from analysis import build_cfg, compute_dominators, build_loop_info
    from ir_utils import func_slices
    # map each body-instruction id -> its loop header object (pre-transform)
    body_header = {}
    for (lo, hi) in func_slices(instrs):
        cfg = build_cfg(instrs, lo, hi)
        dom = compute_dominators(cfg)
        li = build_loop_info(cfg, dom)
        for loop in li.loops:
            hdr_obj = instrs[cfg.blocks[loop.header].lo]   # the IRLabel object
            for b in loop.body:
                for k in range(cfg.blocks[b].lo, cfg.blocks[b].hi + 1):
                    if k != cfg.blocks[loop.header].lo:
                        body_header.setdefault(id(instrs[k]), hdr_obj)

    after = licm2.loop_invariant_code_motion(list(instrs))
    pos = {id(x): k for k, x in enumerate(after)}
    hoisted = set()
    for oid, hdr_obj in body_header.items():
        if oid in pos and id(hdr_obj) in pos and pos[oid] < pos[id(hdr_obj)]:
            hoisted.add(oid)
    return hoisted


def _reason_not_hoisted(instrs, region, du, idx, my_invariant_ids):
    """Why licm2 would NOT hoist an M2-invariant instruction (categorization)."""
    ins = instrs[idx]
    c = _cname(ins)
    if c not in _HOIST_KINDS:
        return 'not-hoistable-kind (%s)' % ('memory-load' if c in ('IRLoad', 'IRGlobalLoad') else c)
    if getattr(ins, 'ftype', None) or (c == 'IRCast' and
            ('$f' in getattr(ins, 'dest_type', '') or '$f' in getattr(ins, 'src_type', ''))):
        return 'float (excluded for trap/NaN)'
    dest = getattr(ins, 'dest', None)
    if dest is None:
        return 'no single dest'
    dn = dest.name
    body_defs = [d for d in du.def_sites(dn) if d in region]
    if body_defs != [idx]:
        return 'loop-carried / multiply-defined dest'
    if any(u not in region for u in du.use_sites(dn)):
        return 'used after the loop'
    return 'no-preheader / position (other legality)'


def main():
    from analysis import DefUse
    files = []
    for p in ('testing/**/*.c', 'new_isa_tests/**/*.c',
              'demo_prof/**/*.c', 'isa_coverage_tests/**/*.c'):
        files += glob.glob(os.path.join(_ROOT, p), recursive=True)
    files = sorted(set(files))

    n_prog = 0
    n_hoisted = 0
    n_sound = 0          # hoisted AND in M2 invariant set
    misses = []          # hoisted but NOT in M2 invariant set  (must be 0)
    superset_reasons = {}
    n_invariant_total = 0

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
        n_prog += 1

        # M2 invariant set, as object ids (stable across the licm2 copy)
        descs = discover(instrs)
        annotate_memory_effects(descs)
        my_inv_ids = set()
        inv_by_region = []      # (region-set, du, {idx})
        for d in descs:
            region = set()
            for b in d.body_blocks:
                blk = d.cfg.blocks[b]
                region.update(range(blk.lo, blk.hi + 1))
            lo, hi = d.func_slice
            du = DefUse(instrs, lo, hi)
            inv_by_region.append((region, du, set(d.invariant_insts)))
            for i in d.invariant_insts:
                my_inv_ids.add(id(instrs[i]))
            n_invariant_total += len(d.invariant_insts)

        # licm2 ground truth
        hoisted = _licm_hoisted_ids(instrs)
        for oid in hoisted:
            n_hoisted += 1
            if oid in my_inv_ids:
                n_sound += 1
            else:
                misses.append((os.path.relpath(f, _ROOT), oid))

        # categorize M2-invariant-but-not-hoisted
        hoisted_or_missing = hoisted
        for region, du, inv_idxs in inv_by_region:
            for i in inv_idxs:
                if id(instrs[i]) in hoisted_or_missing:
                    continue
                r = _reason_not_hoisted(instrs, region, du, i, my_inv_ids)
                superset_reasons[r] = superset_reasons.get(r, 0) + 1

    print("=" * 72)
    print("  M2 MemEffects.invariant_insts  vs  licm2.py hoisted set")
    print("=" * 72)
    print(f"  programs analyzed              : {n_prog}")
    print(f"  M2 invariant instrs (total)    : {n_invariant_total}")
    print(f"  licm2 HOISTED instrs (truth)   : {n_hoisted}")
    print(f"  SOUND (hoisted in M2 invariant): {n_sound}")
    print(f"  MISSES (hoisted, M2 missed)    : {len(misses)}   <- must be 0")
    print("\n  M2-invariant but NOT hoisted by licm2 (legality reasons):")
    for r, c in sorted(superset_reasons.items(), key=lambda kv: -kv[1]):
        print(f"    {c:5d}  {r}")
    if misses:
        print("\n  MISSES:")
        for fn, oid in misses[:20]:
            print(f"    {fn}  id={oid}")
    print("=" * 72)
    return 0 if not misses else 1


if __name__ == '__main__':
    raise SystemExit(main())

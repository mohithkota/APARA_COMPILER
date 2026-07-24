"""
analysis_profile.py -- Profile analysis (Loop Optimization Framework, M3).

ANALYSIS ONLY. Populates the performance-profile portion of a LoopDescriptor
with FACTS (measurements). It answers "what performance characteristics does
this loop have?" -- never "should it be optimized?" (a cost model, which belongs
to future passes). It mutates no IR.

REUSE, NOT REIMPLEMENTATION. The metric mathematics is exactly parallelism_
profile.py's: this module imports and calls its `_critical_path`,
`_res_mii_detail`, `_rec_mii`, `_reg_pressure`, `_true_dep` (and the bundler's
`_must_precede`) unchanged. The only new code is an ADAPTER that turns an IR loop
body into the record shape those functions already consume:

    {'writes', 'reads', 'mem_access', 'mem_write', 'text', 'is_ctrl'}

STAGE. parallelism_profile.py runs on the FINAL bundled mcode (physical
registers, hardware lanes, real bundles). A LoopDescriptor is PRE-optimization,
memory-backed IR. So the numbers here are IR-STAGE estimates: they use the same
formulas but on a different, earlier representation (before IVSR / LICM /
loop-reg / register allocation / bundling). The cross-check reports this
relationship explicitly; the two are not expected to be numerically equal, and
`profile_from_records` is validated to reproduce parallelism_profile.py EXACTLY
when given the same (mcode) records -- proving the reuse is faithful.

Loop-carried recurrences in this memory-backed IR flow through STACK SLOTS
(store this iteration, reload next), so `_rec_mii`'s memory-carried path detects
them here (it becomes a register recurrence only later, after loop-reg).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import Const, Temp                                         # noqa: E402
from ir_utils import src_names, dest_names                         # noqa: E402
from analysis import DefUse                                        # noqa: E402

# Reused, unchanged, from the existing profiler / bundler:
from parallelism_profile import (_critical_path, _res_mii_detail,   # noqa: E402
                                 _rec_mii, _reg_pressure, _true_dep)
from bundler import _must_precede                                  # noqa: E402


def _cname(x):
    return type(x).__name__


def _is_zero(off):
    return isinstance(off, Const) and off.value == 0


# instruction-class -> synthetic `text` prefix so the reused resource classifier
# (_res_mii_detail / _is_div_sqrt: startswith '$ld'/'$st', '/', '$fsqrt') fires
# on IR the same way it does on mcode.
_LOAD_CLS = ('IRLoad', 'IRGlobalLoad', 'IRLoadWide')
_STORE_CLS = ('IRStore', 'IRGlobalStore', 'IRStoreWide')
_CTRL_CLS = ('IRCondJump', 'IRJump', 'IRReturn', 'IRHalt', 'IRCall', 'IRIndirectCall')


def _mem_key(ins, addr_off):
    """A (base, offset) alias key COMPATIBLE with the bundler's oracle
    (`_mem_may_alias`): a 2-tuple of strings where `offset` is a numeric string
    for a constant and a '$'-prefixed string for a variable/unknown offset. The
    bundler proves two accesses disjoint only when base is equal AND both offsets
    are DIFFERENT CONSTANTS -- exactly matching the mcode model, so the reused
    _must_precede / _true_dep / _rec_mii behave identically here.

      stack slot   -> ('$FP', str(fp_offset))            # distinct slots disjoint
      global       -> ('$GB', str(dmem_addr + const))    # distinct globals disjoint
                      ('$GB', '$g<addr>') for a variable offset (conservative)
      computed ptr -> (base_temp, str(const) | '$v')     # distinct pointers may alias
    """
    c = _cname(ins)
    if c in ('IRLoad', 'IRStore'):
        if isinstance(ins.base, Temp) and _is_zero(ins.offset) and ins.base.name in addr_off:
            return ('$FP', str(addr_off[ins.base.name]))
        base = ins.base.name if isinstance(ins.base, Temp) else '$?'
        off = str(ins.offset.value) if isinstance(ins.offset, Const) else '$v'
        return (base, off)
    if c in ('IRGlobalLoad', 'IRGlobalStore'):
        if isinstance(ins.offset, Const):
            return ('$GB', str(ins.dmem_addr + ins.offset.value))
        return ('$GB', '$g%x' % ins.dmem_addr)
    if c in ('IRLoadWide', 'IRStoreWide'):
        base = ins.base.name if isinstance(ins.base, Temp) else '$?'
        return (base, '$w')
    return None


def _synth_text(ins):
    """Synthetic instruction text driving the reused resource classifier."""
    c = _cname(ins)
    if c in _LOAD_CLS:
        return '$ld'
    if c in _STORE_CLS:
        return '$st'
    if c == 'IRFsqrt':
        return '$fsqrt'
    if c == 'IRBinOp' and ins.op == '/':
        return '/'                              # occupies the div/sqrt lane
    if c == 'IRVecArith' and getattr(ins, 'op', None) == '/':
        return '$v /'
    return c                                    # any non-resource-limited op


def _ir_records(desc):
    """Adapt the loop body's IR into parallelism_profile-style records, in
    program order. Reuses DefUse only to resolve stack-slot addresses."""
    instrs = desc.cfg.instrs
    lo, hi = desc.func_slice
    du = DefUse(instrs, lo, hi)
    addr_off = {name: instrs[k].fp_offset
                for name, k in du.single_defs().items()
                if _cname(instrs[k]) == 'IRLoadAddr'}

    region = []
    for b in sorted(desc.body_blocks):
        blk = desc.cfg.blocks[b]
        region.extend(range(blk.lo, blk.hi + 1))
    region.sort()

    recs = []
    for k in region:
        ins = instrs[k]
        c = _cname(ins)
        w = frozenset(dest_names(ins))
        r = frozenset(src_names(ins))
        mem_access = mem_write = None
        if c in _LOAD_CLS:
            mem_access = _mem_key(ins, addr_off)
        elif c in _STORE_CLS:
            mem_access = mem_write = _mem_key(ins, addr_off)
        recs.append({'writes': w, 'reads': r, 'mem_access': mem_access,
                     'mem_write': mem_write, 'text': _synth_text(ins),
                     'is_ctrl': c in _CTRL_CLS})
    return recs


def profile_from_records(recs):
    """THE shared metric core: compute the full profile bundle from a list of
    parallelism_profile-style records. Used by both the IR path (below) and the
    fidelity cross-check (fed mcode records). Every number here comes from a
    reused parallelism_profile.py function."""
    N = len(recs)
    Hnow = _critical_path(recs, _must_precede)
    Htrue = _critical_path(recs, _true_dep)
    res, n_ls, mem_term, width_term, div_term = _res_mii_detail(recs)
    rec, rec_nodes, mem_rec = _rec_mii(recs)
    mii = max(res, rec)
    peak, free = _reg_pressure(recs)
    return {
        'body_inst_count': N,
        'crit_path_height': Hnow,
        'crit_path_true': Htrue,
        'res_mii': res,
        'rec_mii': rec,
        'mii': mii,
        'est_ipb': (N / mii) if mii else 0.0,
        'reg_pressure_peak': peak,
        'reg_free': free,
        'stats': {'n_mem_ops': n_ls, 'mem_lane_term': mem_term,
                  'width_term': width_term, 'div_term': div_term,
                  'rec_nodes': rec_nodes, 'mem_recurrence': mem_rec},
    }


def analyze_profile(desc):
    """Compute IR-level profile facts for one LoopDescriptor and store them on
    its Profile fields. Returns the descriptor. Pure analysis."""
    recs = _ir_records(desc)
    p = profile_from_records(recs)
    desc.profile_analyzed = True
    desc.body_inst_count = p['body_inst_count']
    desc.crit_path_height = p['crit_path_height']
    desc.crit_path_true = p['crit_path_true']
    desc.res_mii = p['res_mii']
    desc.rec_mii = p['rec_mii']
    desc.mii = p['mii']
    desc.est_ipb = p['est_ipb']
    desc.reg_pressure_peak = p['reg_pressure_peak']
    desc.reg_free = p['reg_free']
    desc.profile_stats = p['stats']
    return desc


def annotate_profile(descs):
    """Run the Profile analysis over a list of LoopDescriptors. Returns the same
    list (Profile fields populated)."""
    for d in descs:
        analyze_profile(d)
    return descs

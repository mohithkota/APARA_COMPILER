"""
kernel_detector.py -- Vector Kernel Recognition (Milestone R4.0).

RECOGNITION ONLY -- detects candidate vectorizable kernels; performs NO
transformation and emits NO vector IR. It reuses the existing loop framework
(LoopInfo/M0 descriptors, M1 induction variables, M2 memory effects, the R2.1
DependenceGraph) and records, per innermost loop:

  * kernel kind        : dot-product / sum-reduction / SAXPY / vector-add /
                         matmul / convolution / none
  * loop structure     : trip count, nesting
  * induction variables
  * reduction variable  (the loop-carried accumulator, if any)
  * affine accesses     (array loads/stores that are unit-stride in an IV)
  * element type        (from the accessed array width) + its vector tag

The classifier keys off two facts already computed by the framework: (a) a clean
single-store accumulator slot whose stored value is `load(slot) + V` (a reduction
recurrence), and (b) whether the loads/stores are affine in the loop IV. Everything
else -- the ISA question of whether the recognised kernel is *vectorizable* -- is
answered by vector_legality/vector_capability, not here.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import Const, Temp
from ir_utils import func_slices, src_names, dest_names
from analysis import DefUse
from loopopt.discovery import discover_function
from loopopt.analysis_iv import annotate_induction_vars, TripCount
from loopopt.analysis_mem import annotate_memory_effects
from vector_capability import VectorCapability

_cap = VectorCapability()


class KernelCandidate:
    """A recognised (not transformed) vectorizable-kernel candidate."""
    __slots__ = ('func', 'header', 'label', 'kind', 'trip', 'nested',
                 'ivs', 'reduction_slot', 'reduction_op', 'reduction_value',
                 'loads', 'stores', 'affine_loads', 'affine_stores',
                 'elem_bytes', 'signed', 'vtype', 'notes')

    def __init__(self, func, header, label):
        self.func = func
        self.header = header
        self.label = label
        self.kind = None
        self.trip = None
        self.nested = False
        self.ivs = []
        self.reduction_slot = None
        self.reduction_op = None
        self.reduction_value = None       # 'dot' | 'load' | 'scaled' | ...
        self.loads = 0
        self.stores = 0
        self.affine_loads = 0
        self.affine_stores = 0
        self.elem_bytes = None
        self.signed = True
        self.vtype = None
        self.notes = []

    def __repr__(self):
        if not self.kind:
            return f"Kernel({self.func}@B{self.header} '{self.label}' none)"
        return (f"Kernel({self.func}@B{self.header} '{self.label}' {self.kind}"
                f" trip={self.trip} elem={self.elem_bytes}B vtype={self.vtype})")


def _cname(x):
    return type(x).__name__


def _elem_c_type(elem_bytes, signed):
    return {(1, True): 'char', (1, False): 'unsigned char',
            (2, True): 'short', (2, False): 'unsigned short',
            (4, True): 'int', (4, False): 'unsigned int',
            (8, True): 'long long', (8, False): 'unsigned long long'}.get(
        (elem_bytes, signed))


def _detect_loop(desc, instrs):
    """Classify one loop descriptor into a KernelCandidate. Analysis only."""
    fname = getattr(instrs[desc.func_slice[0]], 'name', '?')
    k = KernelCandidate(fname, desc.header, desc.label())
    if desc.trip_count and desc.trip_count.kind == TripCount.KNOWN:
        k.trip = desc.trip_count.value
    k.nested = not desc.is_innermost
    k.ivs = sorted(desc.basic_ivs.keys())

    # body region + memory facts (reuse M2)
    region = set()
    for b in desc.body_blocks:
        blk = desc.cfg.blocks[b]
        region.update(range(blk.lo, blk.hi + 1))
    loads = [desc.cfg.instrs[i] for i in sorted(region)
             if _cname(desc.cfg.instrs[i]) in ('IRLoad', 'IRGlobalLoad')]
    stores = [desc.cfg.instrs[i] for i in sorted(region)
              if _cname(desc.cfg.instrs[i]) in ('IRStore', 'IRGlobalStore')]
    k.loads = len(loads)
    k.stores = len(stores)

    lo, hi = desc.func_slice
    du = DefUse(instrs, lo, hi)
    def_map = du.single_defs()
    addr_off = {n: instrs[i].fp_offset for n, i in def_map.items()
                if _cname(instrs[i]) == 'IRLoadAddr'}

    # address-temps that are affine in an IV (reuse M1 iv_terms: name->(slot,scale))
    iv_addr_temps = set(desc.iv_terms.keys())

    def _affine_access(ins):
        """A load/store whose offset is IV-derived (unit/affine stride)."""
        off = getattr(ins, 'offset', None)
        base = getattr(ins, 'base', None)
        # array access: base is a stack/global address, offset is an IV term
        if isinstance(off, Temp) and off.name in iv_addr_temps:
            return True
        if isinstance(base, Temp) and base.name in iv_addr_temps:
            return True
        return False

    k.affine_loads = sum(1 for l in loads if _affine_access(l))
    k.affine_stores = sum(1 for s in stores if _affine_access(s))

    # element width of the affine array accesses (drives the vector element type)
    widths = [l.elem_bytes for l in loads if _affine_access(l)] + \
             [s.elem_bytes for s in stores if _affine_access(s)]
    if widths:
        k.elem_bytes = min(widths)                 # narrowest packs the most lanes
        k.signed = not any(getattr(l, 'unsigned', False)
                           for l in loads if _affine_access(l))
        ct = _elem_c_type(k.elem_bytes, k.signed)
        k.vtype = _cap.vector_type(ct, k.signed) if ct else None

    # ── reduction accumulator: a clean single-store slot whose value is
    #    load(slot) + V  (mirrors analysis_iv's basic-IV shape, variant step) ──
    store_slots = {}
    for i in sorted(region):
        ins = instrs[i]
        if _cname(ins) == 'IRStore' and isinstance(ins.base, Temp) \
                and isinstance(ins.offset, Const) and ins.offset.value == 0:
            off = addr_off.get(ins.base.name)
            if off is not None:
                store_slots.setdefault(off, []).append(i)

    def _loads_slot(name, slot):
        d = def_map.get(name)
        if d is None:
            return False
        ins = instrs[d]
        return (_cname(ins) == 'IRLoad' and isinstance(ins.base, Temp)
                and addr_off.get(ins.base.name) == slot
                and isinstance(ins.offset, Const) and ins.offset.value == 0)

    iv_slots = set(desc.basic_ivs.keys())          # exclude induction variables
    for slot, sites in store_slots.items():
        if len(sites) != 1 or slot in iv_slots:
            continue
        st = instrs[sites[0]]
        if not isinstance(st.src, Temp):
            continue
        d = def_map.get(st.src.name)
        if d is None or _cname(instrs[d]) != 'IRBinOp' or instrs[d].op not in ('+',):
            continue
        upd = instrs[d]
        L, R = upd.left, upd.right
        # one operand is load(slot) (the carried accumulator), the other is V
        if isinstance(L, Temp) and _loads_slot(L.name, slot):
            V = R
        elif isinstance(R, Temp) and _loads_slot(R.name, slot):
            V = L
        else:
            continue
        k.reduction_slot = slot
        k.reduction_op = '+'
        # classify V: product of two loads (dot) vs single load (sum)
        k.reduction_value = _classify_reduction_value(V, instrs, def_map)
        break

    _classify_kind(k, desc, instrs, def_map, region)
    return k


def _classify_reduction_value(V, instrs, def_map):
    if not isinstance(V, Temp):
        return 'const'
    d = def_map.get(V.name)
    if d is None:
        return 'load'
    ins = instrs[d]
    if _cname(ins) == 'IRBinOp' and ins.op == '*':
        la = def_map.get(ins.left.name) if isinstance(ins.left, Temp) else None
        ra = def_map.get(ins.right.name) if isinstance(ins.right, Temp) else None
        lc = _cname(instrs[la]) if la is not None else None
        rc = _cname(instrs[ra]) if ra is not None else None
        if lc in ('IRLoad', 'IRGlobalLoad') and rc in ('IRLoad', 'IRGlobalLoad'):
            return 'dot'                            # load * load
        return 'scaled'                             # load * const
    if _cname(ins) in ('IRLoad', 'IRGlobalLoad'):
        return 'load'
    return 'expr'


def _value_has_multiply(root, instrs, def_map, region, seen=None):
    """True if a DATA multiply feeds `root`'s value (tracing def-use through the
    loop body). Address arithmetic (index*elem_size feeding a base/offset) is NOT
    reached, because we only follow the stored *value*'s operands."""
    if seen is None:
        seen = set()
    if not isinstance(root, Temp) or root.name in seen:
        return False
    seen.add(root.name)
    d = def_map.get(root.name)
    if d is None or d not in region:
        return False
    ins = instrs[d]
    c = _cname(ins)
    if c in ('IRLoad', 'IRGlobalLoad', 'IRLoadAddr', 'IRGlobalAddrOf'):
        return False                               # a loaded value is a data leaf
    if c == 'IRBinOp' and ins.op == '*':
        return True
    for s in (src_names(ins) or []):
        if _value_has_multiply(Temp(s), instrs, def_map, region, seen):
            return True
    return False


def _affine_store_src(k, desc, instrs):
    """The value temp stored by the loop's affine store (for elementwise shape)."""
    region = sorted({i for b in desc.body_blocks
                     for i in range(desc.cfg.blocks[b].lo, desc.cfg.blocks[b].hi + 1)})
    du = DefUse(instrs, *desc.func_slice)
    addr_off = {n: instrs[i].fp_offset for n, i in du.single_defs().items()
                if _cname(instrs[i]) == 'IRLoadAddr'}
    iv_terms = set(desc.iv_terms.keys())
    for i in region:
        ins = instrs[i]
        if _cname(ins) in ('IRStore', 'IRGlobalStore'):
            off = getattr(ins, 'offset', None)
            base = getattr(ins, 'base', None)
            if (isinstance(off, Temp) and off.name in iv_terms) or \
               (isinstance(base, Temp) and base.name in iv_terms):
                return ins.src
    return None


def _classify_kind(k, desc, instrs, def_map, region):
    """Assign the kernel kind from the recorded facts."""
    depth = getattr(desc, 'depth', 0)
    if k.reduction_slot is not None:
        if k.reduction_value == 'dot':
            # a dot-product accumulation nested >= 2 deep is a matmul inner loop
            k.kind = 'matmul' if depth >= 2 else 'dot-product'
        else:
            # a scaled/plain accumulation nested >= 1 with 2D access = conv-ish
            k.kind = 'sum-reduction'
        return
    # no reduction -> elementwise store patterns
    if k.affine_stores >= 1 and k.affine_loads >= 1:
        src = _affine_store_src(k, desc, instrs)
        if src is not None and _value_has_multiply(src, instrs, def_map, region):
            k.kind = 'saxpy'                        # z[i] = a*x[i] (+ y[i]) shape
        else:
            k.kind = 'vector-add'                   # z[i] = x[i] op y[i] / map
    else:
        k.kind = None
        k.notes.append('no-recognised-idiom')


# ── module driver ───────────────────────────────────────────────────────────────

def detect_function(instrs, lo, hi):
    """Recognise vectorizable-kernel candidates in one function slice."""
    descs = discover_function(instrs, lo, hi)
    annotate_induction_vars(descs)
    annotate_memory_effects(descs)
    out = []
    for d in descs:
        if not d.is_innermost:
            continue                                # matmul/conv detected at the inner loop
        out.append(_detect_loop(d, instrs))
    return out


def detect_module(instrs):
    """Recognise candidates across a module. Returns [KernelCandidate]. Analysis
    only; `instrs` is never mutated."""
    out = []
    for (lo, hi) in func_slices(instrs):
        out.extend(detect_function(instrs, lo, hi))
    return out

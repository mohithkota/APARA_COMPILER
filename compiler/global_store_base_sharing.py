"""
global_store_base_sharing.py -- R14.8.

Independent global stores whose addresses differ by a COMPILE-TIME CONSTANT are
rewritten to share one materialised base and address themselves with immediate
displacements:

    DMEM[G + off0] = v0            addr = &G[off0]
    DMEM[G + off1] = v1     ==>    *(addr + d1) = v1
    DMEM[G + off2] = v2            *(addr + d2) = v2
    ...                            ...

WHY
---
`codegen._gen_IRGlobalStore` lowers a store with a computed offset as

    +  addr, off_reg, goff          (addr = offset + global displacement)
    +  addr, GBASE, addr            (addr = base + that)
    $st [addr + 0], value

into a BORROWED scratch register, released immediately afterwards. A group of N
such stores therefore becomes N sequential borrow/build/store chains through the
same few scratch registers -- and the bundler is a greedy forward pass that does
not reorder, so it cannot interleave them even though the stores are provably
independent. Measured on a j-tiled matmul epilogue: 20 instructions in 16
bundles, four stores finishing 12 bundles apart despite identical ASAP.

After this pass the same epilogue is 10 instructions in 3 bundles, with all four
stores issuing together (exactly the 4-per-bundle memory-lane limit).

WHAT IT IS NOT
--------------
No new IR node and no codegen change: it re-expresses the group using the
EXISTING `IRGlobalAddrOf` (materialise a global address into a temp) and
`IRStore` (which already lowers a Const offset to `$st [reg + imm]`). The
constant relation between two offsets is proved by R14.2's
`vector_affine.constant_delta` -- there is no second affine analysis here.

Nothing is reordered: each store stays exactly where it was, only its addressing
changes, so memory ordering and aliasing behaviour are untouched.

Kill switch: APARA_NO_STORE_BASE_SHARE.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ir import Const, Temp, IRGlobalStore, IRGlobalAddrOf, IRStore
import vector_affine as _va

#: ISA immediate field for `$st [reg + imm]` (codegen._gen_IRStore).
IMM_LO, IMM_HI = -512, 511

_n = [0]


def _fresh(prefix='_gsb'):
    _n[0] += 1
    return Temp(f'{prefix}{_n[0]}')


def reset():
    _n[0] = 0


def _disabled():
    return os.environ.get('APARA_NO_STORE_BASE_SHARE', '') not in ('', '0')


def _cname(x):
    return type(x).__name__


def _groups(instrs, lo, hi):
    """Maximal runs of IRGlobalStore sharing one dmem base region.

    A run is broken by anything that could alias or that transfers control:
    another memory write, a call, or a label/branch. Loads and pure arithmetic
    between the stores are fine -- they are what compute the stored values.
    """
    out, cur = [], []
    for i in range(lo, hi + 1):
        c = _cname(instrs[i])
        if c == 'IRGlobalStore':
            cur.append(i)
            continue
        if c in ('IRStore', 'IRStoreWide', 'IRCall', 'IRIndirectCall',
                 'IRLabel', 'IRJump', 'IRCondJump', 'IRReturn',
                 'IRFuncBegin', 'IRFuncEnd'):
            if len(cur) > 1:
                out.append(cur)
            cur = []
    if len(cur) > 1:
        out.append(cur)
    return out


def _offset_access(ins, ctx):
    """AffineAccess for a store's offset expression, or None."""
    off = getattr(ins, 'offset', None)
    if isinstance(off, Const):
        return 'const', off.value
    if not isinstance(off, Temp):
        return None, None
    acc = _va.resolve_offset(off, ctx)
    return ('affine', acc) if acc.ok else (None, None)


def share_group(instrs, group, ctx):
    """Rewrite one group in place. Returns the number of stores re-addressed."""
    anchor = instrs[group[0]]
    kinds = [_offset_access(instrs[i], ctx) for i in group]
    if kinds[0][0] is None:
        return 0

    plan = []                                   # (index, displacement)
    for pos, i in enumerate(group):
        k, v = kinds[pos]
        if k != kinds[0][0]:
            return 0                            # mixed forms: leave alone
        if k == 'const':
            d = (instrs[i].dmem_addr - anchor.dmem_addr) + (v - kinds[0][1])
        else:
            delta = _va.constant_delta(kinds[0][1], v)
            if delta is None:
                return 0                        # not provably a constant apart
            d = (instrs[i].dmem_addr - anchor.dmem_addr) + delta
        if not (IMM_LO <= d <= IMM_HI):
            return 0                            # outside the immediate field
        if instrs[i].elem_bytes != anchor.elem_bytes:
            return 0
        plan.append((i, d))

    if len(plan) < 2 or all(d == 0 for _, d in plan[1:]):
        return 0                                # nothing to gain

    base = _fresh()
    for i, d in plan:
        st = instrs[i]
        instrs[i] = IRStore(base, Const(d), st.src, st.elem_bytes)
    instrs.insert(group[0], IRGlobalAddrOf(base, anchor.dmem_addr,
                                           anchor.offset))
    return len(plan)


def run(instrs):
    """Rewrite every eligible group in the module. Returns (instrs, n_groups)."""
    if _disabled():
        return instrs, 0
    from ir_utils import func_slices
    from loopopt.discovery import discover_function
    from loopopt.analysis_iv import annotate_induction_vars
    from loopopt.analysis_mem import annotate_memory_effects

    out = list(instrs)
    shared = 0
    # Later insertions shift indices, so process function slices back to front
    # and groups within a slice back to front.
    for (lo, hi) in reversed(list(func_slices(out))):
        sub = out[lo:hi + 1]
        try:
            descs = discover_function(sub, 0, len(sub) - 1)
            annotate_induction_vars(descs)
            annotate_memory_effects(descs)
        except Exception:
            continue
        if not descs:
            continue
        for group in reversed(_groups(out, lo, hi)):
            ctx = None
            for d in descs:
                blocks = set()
                for b in d.body_blocks:
                    blk = d.cfg.blocks[b]
                    blocks.update(range(blk.lo, blk.hi + 1))
                if all((g - lo) in blocks for g in group):
                    try:
                        ctx = _va.LoopAffineContext(sub, d)
                    except Exception:
                        ctx = None
                    break
            if ctx is None:
                continue
            # `_offset_access` resolves against the sub-slice's numbering
            local = [g - lo for g in group]
            n = _share_in_slice(out, lo, group, sub, local, ctx)
            if n:
                shared += 1
    return out, shared


def _share_in_slice(out, lo, group, sub, local, ctx):
    """Resolve offsets in the slice's context, then rewrite in the module list."""
    anchor = out[group[0]]
    kinds = [_offset_access(sub[i], ctx) for i in local]
    if kinds[0][0] is None:
        return 0
    plan = []
    for pos, i in enumerate(group):
        k, v = kinds[pos]
        if k != kinds[0][0]:
            return 0
        if k == 'const':
            d = (out[i].dmem_addr - anchor.dmem_addr) + (v - kinds[0][1])
        else:
            delta = _va.constant_delta(kinds[0][1], v)
            if delta is None:
                return 0
            d = (out[i].dmem_addr - anchor.dmem_addr) + delta
        if not (IMM_LO <= d <= IMM_HI) or out[i].elem_bytes != anchor.elem_bytes:
            return 0
        plan.append((i, d))
    if len(plan) < 2 or all(d == 0 for _, d in plan[1:]):
        return 0
    base = _fresh()
    for i, d in plan:
        st = out[i]
        out[i] = IRStore(base, Const(d), st.src, st.elem_bytes)
    out.insert(group[0], IRGlobalAddrOf(base, anchor.dmem_addr, anchor.offset))
    return len(plan)

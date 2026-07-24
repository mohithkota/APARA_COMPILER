"""
licm2.py -- conservative Loop Invariant Code Motion (Milestone 8).

Hoists PURE register computations that are loop-invariant out of a loop into its
preheader. Memory operations are intentionally excluded (no load/store hoisting,
no alias analysis, no PRE, no speculation, no CFG edge splitting). This is
primarily an architectural consolidation onto the shared analysis framework
(CFG + Dominators + LoopInfo + DefUse); performance gains are expected to be
modest (the hot loops are recurrence/resource-bound, not invariant-bound).

NAMING: the older ad-hoc pass lives in licm.py (unchanged, still hoists
invariant loads/addresses). This independent pass is licm2.py; the two are A/B
comparable via APARA_NO_LICM (disables THIS pass).

--------------------------------------------------------------------------------
Algorithm
--------------------------------------------------------------------------------
Repeatedly (fixpoint): for each function, for each natural loop innermost-first,
find one invariant, safe-to-hoist instruction and move it to the end of the
loop's existing preheader; stop when none remain.

Innermost-first matters: hoisting a value out of an inner loop places it in the
inner loop's preheader, which lies in the ENCLOSING loop's body -- so on the next
round it can become invariant there and migrate further out. The outer while-loop
realizes that migration without special-casing it.

Invariant detection is a FIXED POINT: an instruction is invariant iff every temp
operand is defined outside the loop OR is itself already proven invariant.
Invariance is recursive (a+b is invariant only once a and b are), so one ordered
pass is insufficient -- we sweep until no new invariants appear.

--------------------------------------------------------------------------------
Legality (hoist only if ALL hold)
--------------------------------------------------------------------------------
  1. pure computation of a whitelisted kind (below) -- no side effects;
  2. defines exactly one Temp;
  3. that destination is NOT otherwise defined inside the loop (not loop-carried);
  4. every temp operand is loop-invariant (fixpoint);
  5. non-trapping -- integer arithmetic in this fault-free VM never traps
     (division by zero is defined as 0 by codegen), so it is safe to speculate;
     floats are excluded (NaN / trap uncertainty);
  6. after hoisting, the definition dominates every use. We guarantee this
     conservatively by requiring EVERY use of the destination to lie inside the
     loop: the preheader dominates the header, hence the whole loop body, so it
     dominates all in-loop uses. (Uses after the loop are rejected -- a simple,
     safe over-approximation for the first implementation.)
If anything is uncertain, the instruction is left in place.

Preheader: reused only. A valid preheader is the UNIQUE non-back-edge predecessor
of the header that dominates it. If none exists, the loop is skipped -- we do NOT
split edges to synthesize one (deliberate simplification; edge-splitting is the
classic source of preheader bugs).

Excluded categories and why: IRLoad/IRStore/IRGlobalLoad/IRGlobalStore (memory --
would need aliasing proof; deferred to a future memory-LICM), IRCall (arbitrary
side effects), branches/jumps/returns (control flow, not computation), floats
(trap/NaN semantics), volatile/atomics (ordering; none in this IR but stated).
"""

import os
from ir import Temp
from ir_utils import func_slices, src_names
from analysis import build_cfg, compute_dominators, build_loop_info, DefUse

# Pure, single-destination, non-memory computations eligible for hoisting.
_HOIST_KINDS = ('IRBinOp', 'IRUnaryOp', 'IRAssign', 'IRCast',
                'IRLoadAddr', 'IRGlobalAddrOf')
_TERMS = ('IRJump', 'IRCondJump', 'IRReturn', 'IRHalt')


class _Stats:
    def __init__(self):
        self.hoisted = 0


def _is_float(ins):
    if getattr(ins, 'ftype', None):
        return True
    if type(ins).__name__ == 'IRCast':
        return '$f' in getattr(ins, 'dest_type', '') or '$f' in getattr(ins, 'src_type', '')
    return False


def _hoistable_kind(ins):
    return type(ins).__name__ in _HOIST_KINDS and not _is_float(ins)


def _one_hoist(instrs, stats):
    """Find and apply a single legal hoist; return True if one was applied."""
    for lo, hi in func_slices(instrs):
        cfg = build_cfg(instrs, lo, hi)
        dom = compute_dominators(cfg)
        li = build_loop_info(cfg, dom)
        du = DefUse(instrs, lo, hi)
        # innermost-first: deepest nesting depth first
        loops = sorted(li.loops,
                       key=lambda L: -max((li.depth(b) for b in L.body), default=0))
        for loop in loops:
            header = loop.header
            non_back = [p for p in cfg.preds(header) if p not in loop.body]
            if len(non_back) != 1:
                continue                                   # no unique preheader
            pre = non_back[0]
            if not dom.dominates(pre, header):
                continue

            body = set()
            for b in loop.body:
                for i in range(cfg.blocks[b].lo, cfg.blocks[b].hi + 1):
                    body.add(i)

            inv = set()

            def op_inv(name):
                d = [x for x in du.def_sites(name) if x in body]
                return (not d) or all(x in inv for x in d)

            changed = True
            while changed:
                changed = False
                for i in sorted(body):
                    if i in inv or not _hoistable_kind(instrs[i]):
                        continue
                    if all(op_inv(nm) for nm in src_names(instrs[i])):
                        inv.add(i)
                        changed = True

            for i in sorted(inv):
                ins = instrs[i]
                dest = getattr(ins, 'dest', None)
                if not isinstance(dest, Temp):
                    continue
                dn = dest.name
                if [x for x in du.def_sites(dn) if x in body] != [i]:
                    continue                                # loop-carried destination
                if any(u not in body for u in du.use_sites(dn)):
                    continue                                # used after the loop
                pb = cfg.blocks[pre]
                pos = pb.hi if type(instrs[pb.hi]).__name__ in _TERMS else pb.hi + 1
                if pos >= i:
                    continue                                # safety: preheader precedes body
                moved = instrs[i]
                del instrs[i]
                instrs.insert(pos, moved)
                stats.hoisted += 1
                return True
    return False


def loop_invariant_code_motion(instrs):
    """Hoist loop-invariant pure computations to preheaders, to a fixpoint.
    Returns a NEW list; instructions are MOVED (never duplicated), original
    objects are reused unchanged."""
    # DEFAULT OFF (opt-in via APARA_LICM=1). Rationale: measurement shows this
    # pass is net-negative on the current corpus -- hoisting extends invariant
    # values' live ranges across the whole loop, raising register pressure so the
    # instruction count rises even though loop-body work drops (the hot loops are
    # recurrence/resource-bound, not invariant-bound, exactly as the design
    # review predicted). It is committed as a correct, unit-tested architectural
    # consolidation onto the shared analyses, kept off in production and A/B
    # enabled via APARA_LICM. (APARA_NO_LICM also force-disables it.)
    if not os.environ.get('APARA_LICM') or os.environ.get('APARA_NO_LICM'):
        return instrs
    instrs = list(instrs)
    stats = _Stats()
    for _ in range(100000):
        if not _one_hoist(instrs, stats):
            break
    if os.environ.get('APARA_LICM_DEBUG'):
        print(f"[licm] hoisted={stats.hoisted}")
    return instrs

"""
gvn.py -- Global Value Numbering (Milestone 6).

Eliminates redundant PURE computations by reusing a previously computed,
still-valid equivalent value. First pass to consume CFG + Dominators + DefUse
together.

        t1 = a + b            t1 = a + b
        ...              -->   ...
        t2 = a + b            t2 = t1        (redundant expr replaced by a copy)

GVN never deletes anything: it turns a redundant expression into a COPY and lets
the existing copy-propagation / coalescing / DCE pipeline clean it up.

--------------------------------------------------------------------------------
Value numbers & the expression table
--------------------------------------------------------------------------------
Each pure expression is given a canonical KEY:

    (kind, opcode, type-tag, canonical-operands)

Operands are canonicalized: a Const -> ('c', value); a Temp -> ('t', name).
For COMMUTATIVE operators (+ * & | ^ ~& ~| ~^) the operand list is SORTED so
that `a+b` and `b+a` hash identically. The expression table maps a key to the
"leader" temp that first computed it. A later instruction whose key is already
in the table is redundant and is rewritten to copy the leader.

--------------------------------------------------------------------------------
Dominance (why reuse is legal)
--------------------------------------------------------------------------------
We walk the DOMINATOR TREE in pre-order with a SCOPED table: entries a block adds
are visible only while processing that block and its dominator-tree descendants,
and are removed on the way back up. Therefore a leader is reused at an
instruction only when the leader's definition DOMINATES that instruction
(an ancestor block, or earlier in the same block) -- so the leader is computed on
every path reaching the use.

--------------------------------------------------------------------------------
Soundness on this NON-SSA IR
--------------------------------------------------------------------------------
An operand's value must be identical at both computations. We therefore only
value-number an expression when:
  * its result temp is SINGLE-DEF (a stable leader), and
  * every Temp operand is NOT multiply-defined (single-def or a param/entry
    value -- i.e. a stable value throughout the function).
Multiply-defined operands are unstable, so such expressions are skipped.

Excluded entirely (conservative): loads (each load is a fresh value -- no memory
CSE), stores, calls (each result unique), casts (representation change), and
floating-point expressions (uncertain NaN / signed-zero semantics). If in doubt,
do not value-number.
"""

import os
from ir import Const, Temp, IRAssign, IRBinOp, IRUnaryOp
from ir_utils import func_slices
from analysis import DefUse, build_cfg, compute_dominators

# Commutative integer operators (operands sorted so a op b == b op a).
_COMMUTATIVE = frozenset({'+', '*', '&', '|', '^', '~&', '~|', '~^'})


class _Stats:
    def __init__(self):
        self.numbered = 0        # expressions given a value number (new leaders)
        self.eliminated = 0      # redundant expressions replaced by a copy


def _operand_key(node, du):
    """Canonical operand token, or None if the operand is not stable/eligible."""
    if isinstance(node, Const):
        return ('c', node.value)
    if isinstance(node, Temp):
        if du.is_multi_def(node.name):
            return None                      # unstable value -> not eligible
        return ('t', node.name)
    return None


# R9.1: the foldable-immediate range codegen._gen_IRLoadAddr can encode directly.
# Sourced from `rematerialization` so ONE constant governs both passes.
from rematerialization import FP_IMM_LO as _FOLDABLE_LO, FP_IMM_HI as _FOLDABLE_HI

# Kill switch for Address Value Numbering alone (APARA_NO_GVN disables all of GVN).
_AVN_DISABLED = os.environ.get('APARA_NO_AVN', '') not in ('', '0')


def _expr_key(ins, du):
    """Canonical key for a value-numberable pure expression, or None if the
    instruction is not eligible."""
    c = type(ins).__name__

    if c == 'IRBinOp':
        if getattr(ins, 'ftype', None):
            return None                      # skip float expressions
        lo = _operand_key(ins.left, du)
        ro = _operand_key(ins.right, du)
        if lo is None or ro is None:
            return None
        ops = (lo, ro)
        if ins.op in _COMMUTATIVE:
            ops = tuple(sorted(ops))
        return ('bin', ins.op, bool(getattr(ins, 'unsigned', False)), ops)

    if c == 'IRUnaryOp':
        o = _operand_key(ins.operand, du)
        if o is None:
            return None
        return ('un', ins.op, o)

    if c == 'IRAssign':
        o = _operand_key(ins.src, du)
        if o is None:
            return None
        return ('copy', o)

    # R9.1 -- Address Value Numbering.
    #
    # `IRLoadAddr(dest, fp_offset)` computes `FP + fp_offset`. FP is written in
    # exactly two places in the compiler (startup, and the function prologue
    # before any body instruction), so within a function this is a PURE,
    # FUNCTION-INVARIANT value and `fp_offset` alone canonicalises it: the node
    # has no base operand, and no IR node names or writes a frame base, so there
    # is nothing else that can vary. The function need not appear in the key --
    # GVN's table is built per function slice, so a function id would be constant
    # in every comparison that can occur (design review sections 1 and 10).
    #
    # Restricted to offsets OUTSIDE the immediate field `_gen_IRLoadAddr` can
    # fold. A foldable offset lowers to ONE instruction (`+ rd, FP, imm`), so
    # collapsing it saves one instruction and costs an extended live range -- a
    # bad trade, measured as regressions on conv3 (+12.4%) and axpy vi16 (+9.2%).
    # Beyond the field, codegen must emit `$set` + `+(-1)` + `<<16` + `|` + add =
    # FIVE instructions, where removing the redundancy is decisive.
    if c == 'IRLoadAddr':
        if _AVN_DISABLED or _FOLDABLE_LO <= ins.fp_offset <= _FOLDABLE_HI:
            return None
        return ('addr', ins.fp_offset)

    return None                              # loads/stores/calls/casts/... excluded


def _gvn_function(instrs, cfg, dom, du, stats):
    """Dominator-tree pre-order value numbering over one function's CFG."""
    children = dom.tree_children()
    table = {}                               # key -> leader temp name

    # iterative dom-tree DFS with scoped add/undo (enter processes, exit undoes)
    entry = cfg.entry_id
    if entry is None:
        return
    added_at = {}
    stack = [(entry, 'enter')]
    while stack:
        bid, phase = stack.pop()
        if phase == 'exit':
            for k in added_at.get(bid, ()):
                table.pop(k, None)
            continue
        added = []
        b = cfg.blocks[bid]
        for i in range(b.lo, b.hi + 1):
            ins = instrs[i]
            dest = getattr(ins, 'dest', None)
            if not isinstance(dest, Temp) or not du.is_single_def(dest.name):
                continue                     # result must be a stable single-def leader
            key = _expr_key(ins, du)
            if key is None:
                continue
            leader = table.get(key)
            if leader is not None and leader != dest.name:
                instrs[i] = IRAssign(dest, Temp(leader))   # redundant -> copy leader
                stats.eliminated += 1
            elif leader is None:
                table[key] = dest.name
                added.append(key)
                stats.numbered += 1
        added_at[bid] = added
        stack.append((bid, 'exit'))
        for ch in reversed(children.get(bid, [])):
            stack.append((ch, 'enter'))


def global_value_numbering(instrs):
    """GVN over every function slice. Returns a NEW list; original instruction
    objects are never mutated (redundant defs are replaced by fresh IRAssign
    copies). Deletes nothing -- the copy/DCE pipeline cleans up."""
    if os.environ.get('APARA_NO_GVN'):
        return instrs
    instrs = list(instrs)
    stats = _Stats()
    for lo, hi in func_slices(instrs):
        cfg = build_cfg(instrs, lo, hi)
        dom = compute_dominators(cfg)
        du = DefUse(instrs, lo, hi)
        _gvn_function(instrs, cfg, dom, du, stats)
    if os.environ.get('APARA_GVN_DEBUG'):
        print(f"[gvn] numbered={stats.numbered} eliminated={stats.eliminated}")
    return instrs

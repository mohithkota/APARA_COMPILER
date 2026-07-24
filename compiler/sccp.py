"""
sccp.py -- Sparse Conditional Constant Propagation (Milestone 5).

The first optimization that consumes the Milestone-4 analysis infrastructure
(CFG + reachability). It propagates constants through Temp definitions, folds
constant expressions, simplifies branches whose condition becomes constant, and
removes basic blocks that become unreachable as a result.

--------------------------------------------------------------------------------
Lattice (classical three-point)
--------------------------------------------------------------------------------
Every Temp maps to one of:
    UNDEF          -- top: not yet known (no evidence of a value)
    CONST(v)       -- known to hold exactly the constant v
    OVER           -- bottom: not a known constant (over-defined)

Meet:
    UNDEF  ∧ x        = x
    CONST(c) ∧ CONST(c) = CONST(c)
    CONST(a) ∧ CONST(b) = OVER        (a != b)
    OVER   ∧ x        = OVER

--------------------------------------------------------------------------------
Soundness on this NON-SSA IR
--------------------------------------------------------------------------------
Only SINGLE-DEFINITION temps (per DefUse) can ever be CONST: a single-def temp
holds exactly the value its one definition computes, at every use, so replacing
that temp by the constant is always valid. Any multiply-defined temp, any
parameter / value defined outside the slice, and any temp defined by a
non-analyzable instruction (load, call, ...) is OVER. This is the conservative
substitute for SSA's per-definition reasoning -- if uncertain, we pick OVER.

Transfer functions evaluate only PURE integer operations (IRAssign / IRUnaryOp /
IRBinOp / IRCast) with signed semantics matching codegen's own constant folder.
Unsigned and float ops, loads, and calls are treated as OVER. Stores are never
propagated through (no memory constant propagation).

--------------------------------------------------------------------------------
Executable edges & unreachable code
--------------------------------------------------------------------------------
Reachability is computed over the CFG: from the entry block, a conditional
branch whose condition folds to a constant makes only its taken edge executable;
otherwise both edges are executable. A block with no executable path from entry
is unreachable -- nothing observes its effects and no reachable branch targets it
(if one did, it would be reachable), so removing it cannot change behaviour. The
function markers (IRFuncBegin/IRFuncEnd) are always retained.

A second DCE pass runs after SCCP (in the pipeline) to remove the now-dead
definitions SCCP's constant substitution leaves behind.
"""

import os
from ir import (Const, Temp, IRAssign, IRBinOp, IRUnaryOp, IRCast, IRJump,
                IRFuncBegin, IRFuncEnd)
from ir_utils import func_slices, src_names
from analysis import DefUse, build_cfg
from copyprop import _replace_uses           # reuse operand substitution (Temp->Const works)


# ── lattice ────────────────────────────────────────────────────────────────────

_UNDEF = ('undef',)
_OVER = ('over',)
def _const(v):        return ('const', v)
def _is_const(x):     return isinstance(x, tuple) and x and x[0] == 'const'


def _meet(a, b):
    if a is _UNDEF:
        return b
    if b is _UNDEF:
        return a
    if a is _OVER or b is _OVER:
        return _OVER
    return a if a[1] == b[1] else _OVER      # both CONST


# ── constant evaluation of pure integer ops ────────────────────────────────────

def _val_of(operand, val):
    """Lattice value of an operand node (Const literal or Temp)."""
    if isinstance(operand, Const):
        return _const(operand.value)
    if isinstance(operand, Temp):
        return val.get(operand.name, _UNDEF)
    return _OVER


def _fold_binop(op, a, b):
    """Signed integer fold, mirroring codegen's constant folder."""
    try:
        return {
            '+': a + b, '-': a - b, '*': a * b,
            '/': int(a / b) if b != 0 else 0,
            '%': (a - int(a / b) * b) if b != 0 else 0,
            '&': a & b, '|': a | b, '^': a ^ b,
            '<<': a << b, '>>': a >> b,
        }.get(op)
    except Exception:
        return None


def _fold_cmp(op, a, b):
    return {
        '<': a < b, '>': a > b, '<=': a <= b, '>=': a >= b,
        '==': a == b, '!=': a != b,
    }.get(op)


def _transfer(ins, val):
    """Lattice value produced into ins.dest, given current operand values.
    Only pure signed-integer producers are evaluated; everything else -> OVER."""
    c = type(ins).__name__
    ftype = getattr(ins, 'ftype', None)
    unsigned = getattr(ins, 'unsigned', False)
    if c == 'IRAssign':
        return _val_of(ins.src, val)
    # IRCast is intentionally NOT folded: a cast changes representation (e.g.
    # (int)3.5f reinterprets/converts the value's bits), so treating it as
    # value-preserving would miscompile. Conservatively OVER.
    if c == 'IRUnaryOp':
        v = _val_of(ins.operand, val)
        if _is_const(v):
            if ins.op == '-':
                return _const(-v[1])
            if ins.op == '~':
                return _const(~v[1])
        return _OVER if v is not _UNDEF else _UNDEF
    if c == 'IRBinOp':
        if ftype or unsigned:
            return _OVER
        l = _val_of(ins.left, val)
        r = _val_of(ins.right, val)
        if l is _UNDEF or r is _UNDEF:
            return _UNDEF
        if _is_const(l) and _is_const(r):
            f = _fold_binop(ins.op, l[1], r[1])
            return _const(f) if f is not None else _OVER
        return _OVER
    return _OVER


# ── value fixpoint ──────────────────────────────────────────────────────────────

_ANALYZABLE = ('IRAssign', 'IRUnaryOp', 'IRBinOp')   # casts excluded (see _transfer)


def _compute_values(instrs, lo, hi, du):
    """Fixpoint lattice value for every single-def, pure-producer temp in the
    slice. Non-single-def / non-pure temps stay OVER. Single-def value graph is
    acyclic here (loop-carried values are multi-def), so this converges."""
    # single-def temps produced by a pure analyzable op are tracked; everything
    # else (multi-def, params, loads, calls, ...) is OVER.
    producer = {}                                # temp name -> producing index
    val = {}
    for name, k in du.single_defs().items():
        if type(instrs[k]).__name__ in _ANALYZABLE:
            producer[name] = k
            val[name] = _UNDEF
        else:
            val[name] = _OVER
    for name in du.multi_names():
        val[name] = _OVER

    changed = True
    while changed:
        changed = False
        for name, k in producer.items():
            new = _transfer(instrs[k], val)      # single def => value is its transfer
            if new != val[name]:
                val[name] = new
                changed = True
    return val


# ── reachability with branch folding ────────────────────────────────────────────

def _eval_condition(term, val):
    """'true' / 'false' / None(unknown) for an IRCondJump terminator."""
    if type(term).__name__ != 'IRCondJump':
        return None
    if getattr(term, 'ftype', None):
        return None                              # don't fold float comparisons
    l = _val_of(term.left, val)
    r = _val_of(term.right, val)
    if _is_const(l) and _is_const(r):
        res = _fold_cmp(term.op, l[1], r[1])
        if res is not None:
            return 'true' if res else 'false'
    return None


def _reachable_blocks(cfg, val):
    """Blocks reachable from entry, honoring folded conditional branches."""
    if cfg.entry_id is None:
        return set()
    reach = {cfg.entry_id}
    work = [cfg.entry_id]
    while work:
        b = cfg.blocks[work.pop()]
        term = cfg.instrs[b.hi]
        cond = _eval_condition(term, val)
        if cond is None:
            succs = b.succs                       # unknown / non-branch: all edges
        else:
            # IRCondJump: succs[0] is the true target; the false edge is the rest
            t = cfg.label_to_block.get(term.true_label)
            if cond == 'true':
                succs = [t] if t is not None else []
            else:
                succs = [s for s in b.succs if s != t]
        for s in succs:
            if s not in reach:
                reach.add(s)
                work.append(s)
    return reach


# ── per-function transform ──────────────────────────────────────────────────────

class _Stats:
    def __init__(self):
        self.consts = 0
        self.folded = 0
        self.branches = 0
        self.blocks = 0


def _sccp_function(instrs, lo, hi, stats):
    cfg = build_cfg(instrs, lo, hi)
    du = DefUse(instrs, lo, hi)
    val = _compute_values(instrs, lo, hi, du)
    reach = _reachable_blocks(cfg, val)

    # count folded expressions: single-def CONST temps produced by a computed op
    for name, k in du.single_defs().items():
        if _is_const(val.get(name)) and type(instrs[k]).__name__ in ('IRBinOp', 'IRUnaryOp'):
            stats.folded += 1

    out = []
    for b in cfg.blocks:
        if b.id not in reach:
            # unreachable: drop everything except structural markers
            kept = [instrs[i] for i in range(b.lo, b.hi + 1)
                    if isinstance(instrs[i], (IRFuncBegin, IRFuncEnd))]
            if len(kept) < (b.hi - b.lo + 1):
                stats.blocks += 1
            out.extend(kept)
            continue
        for i in range(b.lo, b.hi + 1):
            ins = instrs[i]
            # 1. propagate constants into operand uses -- but NOT into casts or
            # float ops: a cast reinterprets the bits, and materializing a
            # constant into a float-typed op risks float-immediate miscodegen.
            # Conservative: leave those operands as temps.
            new = ins
            if type(ins).__name__ != 'IRCast' and getattr(ins, 'ftype', None) is None:
                for nm in set(src_names(ins)):
                    v = val.get(nm)
                    if _is_const(v):
                        new, ch = _replace_uses(new, nm, Const(v[1]))
                        if ch:
                            stats.consts += 1
            # 2. simplify a constant conditional branch
            if type(ins).__name__ == 'IRCondJump':
                cond = _eval_condition(ins, val)
                if cond == 'true':
                    out.append(IRJump(ins.true_label))
                    stats.branches += 1
                    continue
                if cond == 'false':
                    if ins.false_label is not None:
                        out.append(IRJump(ins.false_label))
                    # else: condition false with fall-through -> drop the branch
                    stats.branches += 1
                    continue
            out.append(new)
    return out


# ── public entry ────────────────────────────────────────────────────────────────

def sparse_conditional_constant_propagation(instrs):
    """SCCP over every function slice. Returns a NEW list (originals never
    mutated). Leaves now-dead definitions for the following DCE pass."""
    if os.environ.get('APARA_NO_SCCP'):
        return instrs
    stats = _Stats()
    result = []
    idx = 0
    for lo, hi in func_slices(instrs):
        result.extend(instrs[idx:lo])
        result.extend(_sccp_function(instrs, lo, hi, stats))
        idx = hi + 1
    result.extend(instrs[idx:])
    if os.environ.get('APARA_SCCP_DEBUG'):
        print(f"[sccp] consts={stats.consts} folded={stats.folded} "
              f"branches={stats.branches} unreachable_blocks={stats.blocks}")
    return result

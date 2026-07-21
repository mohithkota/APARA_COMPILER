"""
ir_utils.py -- pure structural helpers over the flat IR list.

These are IR *utilities*, deliberately kept separate from analysis classes
(compiler/analysis/): they answer purely local, per-instruction or per-slice
structural questions with no dataflow. Every analysis in compiler/analysis/ is
built on top of these primitives.

Canonical home going forward. (licm.py / loop_reg.py still carry their own
private copies of the first three; migrating them to import from here is a
trivial, low-risk follow-up left for the next infrastructure touch, so that
this milestone stays scoped to ir_utils + the analysis package + IVSR.)

The scoping rule these enable and enforce: temp names RESTART per function
(Temp.reset() runs at each IRFuncBegin), so any name-keyed analysis must be
built over ONE function slice only -- never whole-program. `func_slices` /
`enclosing_slice` exist precisely so callers can honor that invariant.
"""

from ir import Temp


def dest_names(ins):
    """Names of the temporaries this instruction DEFINES (writes)."""
    if type(ins).__name__ == 'IRLoadWide':
        return [d.name for d in ins.dests]
    d = getattr(ins, 'dest', None)
    return [d.name] if isinstance(d, Temp) else []


def src_names(ins):
    """Names of the temporaries this instruction USES (reads)."""
    c = type(ins).__name__
    ops = []
    if   c == 'IRBinOp':        ops = [ins.left, ins.right]
    elif c == 'IRUnaryOp':      ops = [ins.operand]
    elif c == 'IRAssign':       ops = [ins.src]
    elif c == 'IRLoad':         ops = [ins.base, ins.offset]
    elif c == 'IRLoadWide':     ops = [ins.base, ins.offset]
    elif c == 'IRStore':        ops = [ins.base, ins.offset, ins.src]
    elif c == 'IRStoreWide':    ops = [ins.base, ins.offset] + list(ins.srcs)
    elif c == 'IRGlobalLoad':   ops = [ins.offset]
    elif c == 'IRGlobalStore':  ops = [ins.offset, ins.src]
    elif c == 'IRGlobalAddrOf': ops = [ins.offset]
    elif c == 'IRCondJump':     ops = [ins.left, ins.right]
    elif c == 'IRReturn':       ops = [ins.value] if ins.value is not None else []
    elif c == 'IRCall':         ops = list(ins.args)
    elif c == 'IRIndirectCall': ops = ([ins.func_ptr] if isinstance(ins.func_ptr, Temp) else []) + list(ins.args)
    elif c == 'IRCast':         ops = [ins.src]
    elif c == 'IRFsqrt':        ops = [ins.src]
    elif c == 'IRCmov':         ops = [ins.check, ins.src_true, ins.src_false]
    elif c == 'IRSlice':        ops = [ins.src]
    elif c == 'IRPack':         ops = [ins.src1, ins.src2]
    elif c == 'IRVecArith':     ops = [ins.src1, ins.src2]
    elif c == 'IRVecDot':       ops = [ins.src1, ins.src2] + ([ins.accum] if getattr(ins, 'accumulate', False) and ins.accum else [])
    elif c == 'IRVecDot128':    ops = [ins.a_lo, ins.a_hi, ins.b_lo, ins.b_hi]
    elif c == 'IRVecReduce':    ops = [ins.src]
    return [o.name for o in ops if isinstance(o, Temp)]


def jump_targets(ins):
    """Labels this instruction may transfer control to (empty for fall-through)."""
    c = type(ins).__name__
    if c == 'IRJump':     return [ins.label]
    if c == 'IRCondJump': return [ins.true_label, ins.false_label]
    return []


def func_slices(instrs):
    """Inclusive (start, end) index pairs of each IRFuncBegin..IRFuncEnd slice.
    Callers scope all name-keyed analysis to one slice (see module docstring)."""
    out, start = [], None
    for k, ins in enumerate(instrs):
        c = type(ins).__name__
        if c == 'IRFuncBegin':
            start = k
        elif c == 'IRFuncEnd' and start is not None:
            out.append((start, k))
            start = None
    return out


def enclosing_slice(slices, s, e, n):
    """The (lo, hi) function slice that fully contains [s, e]; whole program
    (0, n-1) if none (e.g. startup code outside any function)."""
    for a, b in slices:
        if a <= s and e <= b:
            return a, b
    return 0, n - 1

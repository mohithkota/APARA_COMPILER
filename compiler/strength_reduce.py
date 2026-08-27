"""
strength_reduce.py -- power-of-two strength reduction for the APARA compiler.

Rewrites expensive integer IRBinOps whose constant operand is a power of two
into cheap shifts / masks, on the flat IR list before code generation:

    x *  2^n   ->  x << n          (always valid in two's complement)
    x /  2^n   ->  x >> n          (ONLY when unsigned / non-negative)
    x %  2^n   ->  x &  (2^n - 1)   (ONLY when unsigned / non-negative)

R17.1 adds the ADDITIVE IDENTITY, which this file's own `_pow2_exp` docstring
already assumed was "handled elsewhere" -- it was not, anywhere:

    x +  0     ->  x               (either operand; integer only)

Why this matters here: the compiler's own 2D-array address lowering emits an
`index * stride` multiply for every subscript and, for sub-word/byte accesses,
`/`(quotient) + `%`(remainder) by a power-of-two stride -- recomputed every
loop iteration. The profiler showed matmul inner loops are 8-wide bound by this
scalar address arithmetic (N ~= 29, only ~5 memory ops), and each `/` also
occupies the single divide/sqrt lane and sits on the critical path. Turning
these into shifts/masks removes the divides outright and shrinks N.

CORRECTNESS -- signedness is the whole subtlety:
  *  x * 2^n == x << n for ALL x (signed or unsigned): the low bits, and the
     two's-complement wraparound, are identical. Safe unconditionally.
  *  x / 2^n == x >> n ONLY when x >= 0. For negative x, C division truncates
     toward zero (-1/2 == 0) but an arithmetic right shift floors toward
     -infinity (-1 >> 1 == -1). So `/` is rewritten ONLY when the IRBinOp is
     flagged `unsigned` (the value is known non-negative); the resulting `>>`
     keeps unsigned=True so codegen emits the LOGICAL shift.
  *  x % 2^n == x & (2^n - 1) ONLY when x >= 0 (same reasoning). Rewritten
     ONLY when `unsigned`.
Float ops (ftype set) are never touched. A negative or non-power-of-two
constant divisor is left exactly as-is.
"""

import os

from ir import IRAssign, IRBinOp, Const


def _pow2_exp(v):
    """Return n if v == 2^n for some n >= 1, else None. (n>=1 so we never
    rewrite '*1' / '/1' / '%1' -- those are identities handled elsewhere and
    turning them into <<0 / >>0 / &0 would be pointless or wrong for %1.)"""
    if not isinstance(v, int) or v < 2:
        return None
    if v & (v - 1):
        return None
    return v.bit_length() - 1


def _reduce_one(ins):
    """Return a NEW reduced instruction if `ins` matches, else None -- an
    IRBinOp for the strength reductions, an IRAssign for the R17.1 additive
    identity. Never mutates `ins`: the caller keeps the original object so any
    other list holding it (e.g. the pristine IR used for verification) is
    unaffected."""
    if not isinstance(ins, IRBinOp):
        return None
    if ins.ftype is not None:            # never touch float arithmetic
        return None

    op = ins.op

    # ── R17.1: additive identity ────────────────────────────────────────────
    # `x + 0` is x for EVERY integer width and signedness this ISA has: an
    # integer IRBinOp always lowers to a full-width `($i64)`/`($u64)` ALU op
    # (codegen._gen_IRBinOp) and IRAssign lowers to `+ d ($i64) $r0 x`, so both
    # are 64-bit register copies and no truncation or re-extension is involved.
    # Folding to a copy lets the EXISTING copy-propagation / coalescing / DCE
    # erase it -- this pass deliberately does not try to delete anything itself.
    #
    # Float is excluded by the `ftype` guard above and must stay excluded:
    # `x + 0.0` is NOT x for x = -0.0, and it silences a signalling NaN.
    #
    # Why it mattered: the compiler had NO algebraic identity simplification at
    # all. `sccp.py` folds only `const OP const` (`_is_const(l) and
    # _is_const(r)`), so an `x + 0` with a variable x survived every pass into
    # codegen as a real instruction. `vector_lowering`'s row-base cloning
    # (`_clone_offset(..., Const(0))`) re-emits the loop's own address
    # expression with the IV substituted by 0 and leaves exactly this residue,
    # twice, in the middle of a SERIAL address chain -- so each one cost a
    # whole bundle of latency, not just a slot (R17.0 Phase 11).
    if op == '+' and not os.environ.get('APARA_NO_IDENTITY_FOLD'):
        for zero_side, val_side in ((ins.right, ins.left), (ins.left, ins.right)):
            if isinstance(zero_side, Const) and zero_side.value == 0:
                return IRAssign(ins.dest, val_side)

    def _new(new_op, left, right):
        return IRBinOp(ins.dest, new_op, left, right,
                       unsigned=ins.unsigned, ftype=ins.ftype)

    if op == '*':
        # commutative: the power-of-two const may be on either side.
        for const_side, val_side in ((ins.right, ins.left), (ins.left, ins.right)):
            if isinstance(const_side, Const):
                n = _pow2_exp(const_side.value)
                if n is not None:
                    return _new('<<', val_side, Const(n))
        return None

    if op in ('/', '%'):
        # non-commutative: divisor must be the RIGHT operand, and the value
        # must be known non-negative (unsigned) for the rewrite to be exact.
        if not ins.unsigned:
            return None
        if not isinstance(ins.right, Const):
            return None
        n = _pow2_exp(ins.right.value)
        if n is None:
            return None
        if op == '/':
            return _new('>>', ins.left, Const(n))     # unsigned -> logical shift
        return _new('&', ins.left, Const((1 << n) - 1))  # low-n-bits mask

    return None


def strength_reduce(instrs):
    """Apply power-of-two strength reduction to a flat IR list. Returns
    (new_instrs, n_changed); changed instructions are fresh objects, so the
    input list's objects are never mutated."""
    out = []
    n_changed = 0
    for ins in instrs:
        new = _reduce_one(ins)
        if new is not None:
            out.append(new)
            n_changed += 1
        else:
            out.append(ins)
    return out, n_changed


if __name__ == '__main__':
    # tiny self-test
    def mk(op, l, r, uns=False, ft=None):
        return IRBinOp('d', op, l, r, unsigned=uns, ftype=ft)
    from ir import Temp
    t = Temp('x')
    cases = [
        (mk('*', t, Const(16)),            '<<', 4),
        (mk('*', Const(8), t),             '<<', 3),
        (mk('/', t, Const(256), uns=True), '>>', 8),
        (mk('%', t, Const(256), uns=True), '&', 255),
        (mk('/', t, Const(256)),           '/', 256),   # signed -> unchanged
        (mk('%', t, Const(64)),            '%', 64),    # signed -> unchanged
        (mk('*', t, Const(3)),             '*', 3),     # not pow2 -> unchanged
        (mk('*', t, Const(16), ft='$f64'), '*', None),  # float -> unchanged
    ]
    for ins, exp_op, exp_r in cases:
        new = _reduce_one(ins)
        res = new if new is not None else ins
        got_r = res.right.value if isinstance(res.right, Const) else None
        status = 'ok' if (res.op == exp_op and (exp_r is None or got_r == exp_r)) else 'FAIL'
        print(f"  [{status}] -> op={res.op} right={got_r}")

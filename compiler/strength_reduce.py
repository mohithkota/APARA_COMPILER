"""
strength_reduce.py -- power-of-two strength reduction for the APARA compiler.

Rewrites expensive integer IRBinOps whose constant operand is a power of two
into cheap shifts / masks, on the flat IR list before code generation:

    x *  2^n   ->  x << n          (always valid in two's complement)
    x /  2^n   ->  x >> n          (ONLY when unsigned / non-negative)
    x %  2^n   ->  x &  (2^n - 1)   (ONLY when unsigned / non-negative)

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

from ir import IRBinOp, Const


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
    """Return a NEW reduced IRBinOp if `ins` matches, else None. Never mutates
    `ins` -- the caller keeps the original object so any other list holding it
    (e.g. the pristine IR used for verification) is unaffected."""
    if not isinstance(ins, IRBinOp):
        return None
    if ins.ftype is not None:            # never touch float arithmetic
        return None

    op = ins.op

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

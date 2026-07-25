"""
ir_interp.py -- a small, self-contained IR interpreter used ONLY as a semantic
oracle for R1.2 differential validation. It is NOT part of the compiler and is
never imported by any production path; it exists so a test / corpus harness can
EXECUTE a function's IR before and after loop unrolling and prove the observable
behaviour is identical.

Model (matches the memory-backed IR the loop analyses assume):
  * a value is a 64-bit two's-complement integer;
  * `IRLoadAddr(dest, off)` yields the integer address `off` (FP taken as 0), so
    a stack slot is addressed by its FP offset; globals are addressed by their
    absolute DMEM address; both live in one flat `mem` dict keyed by int address
    (stack offsets are negative, DMEM addresses positive -- they never collide);
  * a store writes the low `elem_bytes` bytes; a load reads them and sign- or
    zero-extends per the load's `unsigned` flag.

Endianness / sub-word placement is deliberately simplified: the ONLY property
this oracle needs is DETERMINISM and identical treatment of the pre- and
post-transform code, so a difference in output can only come from a real
behavioural divergence introduced by the transform.

Anything outside the supported subset (calls, floating point, vectors, wide
load/store) raises `Unsupported`; the harness then skips the differential for
that function (it is never scored as a pass or a fail).
"""

from ir import Const, Temp


class Unsupported(Exception):
    """The function uses IR outside this oracle's subset; skip the differential."""


class StepLimit(Exception):
    """The interpreter exceeded its instruction budget (treated like Unsupported)."""


_MASK = (1 << 64) - 1


def _wrap(x):
    return x & _MASK


def _to_signed(x):
    x &= _MASK
    return x - (1 << 64) if (x >> 63) else x


def _trunc(value, elem_bytes, unsigned):
    """Reduce `value` to an `elem_bytes`-wide field, then extend to 64 bits with
    the given signedness (models a sub-word store followed by a load)."""
    bits = 8 * elem_bytes
    if bits >= 64:
        return _to_signed(value)
    field = value & ((1 << bits) - 1)
    if not unsigned and (field >> (bits - 1)):
        field -= (1 << bits)
    return field


# ── DMEM preload from global declarations ─────────────────────────────────────

def _preload_globals(instrs):
    """Return a flat address->value dict seeded from every IRGlobalDecl's init."""
    mem = {}
    for ins in instrs:
        if type(ins).__name__ == 'IRGlobalDecl':
            stride = ins.stride
            for i, v in enumerate(ins.init or []):
                try:
                    mem[ins.dmem_addr + i * stride] = _to_signed(int(v))
                except (TypeError, ValueError):
                    pass
    return mem


# ── the interpreter ───────────────────────────────────────────────────────────

class Interp:
    """Executes ONE function slice instrs[lo..hi] over a shared `mem` dict."""

    def __init__(self, instrs, step_limit=2_000_000):
        self.instrs = instrs
        self.step_limit = step_limit

    def _val(self, mem, regs, operand):
        if isinstance(operand, Const):
            return operand.value
        if isinstance(operand, Temp):
            if operand.name not in regs:
                return 0                       # uninitialised temp reads as 0
            return regs[operand.name]
        if isinstance(operand, int):
            return operand
        raise Unsupported(f'operand {operand!r}')

    def run_function(self, lo, hi, mem):
        """Execute the slice [lo, hi]; return the integer passed to IRReturn (or
        0 if the function falls off the end). Mutates `mem` in place."""
        instrs = self.instrs
        # label -> instruction index (within the slice)
        labels = {}
        for i in range(lo, hi + 1):
            ins = instrs[i]
            if type(ins).__name__ == 'IRLabel':
                labels[ins.name] = i

        regs = {}
        pc = lo
        steps = 0
        while pc <= hi:
            steps += 1
            if steps > self.step_limit:
                raise StepLimit()
            ins = instrs[pc]
            c = type(ins).__name__

            if c in ('IRFuncBegin', 'IRLabel', 'IRGlobalDecl', 'IRNop'):
                pc += 1
                continue
            if c == 'IRFuncEnd':
                return 0

            if c == 'IRJump':
                pc = labels[ins.label]
                continue
            if c == 'IRCondJump':
                l = self._val(mem, regs, ins.left)
                r = self._val(mem, regs, ins.right)
                if ins.ftype is not None:
                    raise Unsupported('float compare')
                taken = self._compare(ins.op, l, r)
                nxt = ins.true_label if taken else ins.false_label
                if nxt is None:
                    pc += 1
                else:
                    pc = labels[nxt]
                continue
            if c == 'IRReturn':
                return _to_signed(self._val(mem, regs, ins.value)) if ins.value is not None else 0
            if c == 'IRHalt':
                return 0

            # straight-line data ops
            self._exec_data(ins, c, mem, regs)
            pc += 1

        return 0

    def _compare(self, op, l, r):
        l = _to_signed(l); r = _to_signed(r)
        if op == '<':  return l < r
        if op == '<=': return l <= r
        if op == '>':  return l > r
        if op == '>=': return l >= r
        if op == '==': return l == r
        if op == '!=': return l != r
        raise Unsupported(f'compare op {op}')

    def _exec_data(self, ins, c, mem, regs):
        v = lambda o: self._val(mem, regs, o)

        if c == 'IRLoadAddr':
            regs[ins.dest.name] = ins.fp_offset            # FP == 0
        elif c == 'IRGlobalAddrOf':
            regs[ins.dest.name] = ins.dmem_addr + v(ins.offset)
        elif c == 'IRAssign':
            regs[ins.dest.name] = _to_signed(v(ins.src))
        elif c == 'IRLoad':
            addr = v(ins.base) + v(ins.offset)
            regs[ins.dest.name] = _trunc(mem.get(addr, 0), ins.elem_bytes, ins.unsigned)
        elif c == 'IRStore':
            addr = v(ins.base) + v(ins.offset)
            mem[addr] = _trunc(v(ins.src), ins.elem_bytes, unsigned=False)
        elif c == 'IRGlobalLoad':
            addr = ins.dmem_addr + v(ins.offset)
            regs[ins.dest.name] = _trunc(mem.get(addr, 0), ins.elem_bytes, ins.unsigned)
        elif c == 'IRGlobalStore':
            addr = ins.dmem_addr + v(ins.offset)
            mem[addr] = _trunc(v(ins.src), ins.elem_bytes, unsigned=False)
        elif c == 'IRBinOp':
            regs[ins.dest.name] = self._binop(ins.op, v(ins.left), v(ins.right),
                                              ins.unsigned, ins.ftype)
        elif c == 'IRUnaryOp':
            regs[ins.dest.name] = self._unop(ins.op, v(ins.operand))
        elif c == 'IRCast':
            regs[ins.dest.name] = self._cast(ins.dest_type, v(ins.src))
        else:
            raise Unsupported(f'instruction {c}')

    def _binop(self, op, a, b, unsigned, ftype):
        if ftype is not None:
            raise Unsupported('float arithmetic')
        if unsigned:
            ua, ub = _wrap(a), _wrap(b)
        sa, sb = _to_signed(a), _to_signed(b)
        if op == '+':  return _to_signed(sa + sb)
        if op == '-':  return _to_signed(sa - sb)
        if op == '*':  return _to_signed(sa * sb)
        if op == '/':
            if unsigned:
                return _to_signed(0 if ub == 0 else ua // ub)
            if sb == 0:
                return 0
            q = abs(sa) // abs(sb)
            return _to_signed(-q if (sa < 0) ^ (sb < 0) else q)
        if op == '%':
            if unsigned:
                return _to_signed(0 if ub == 0 else ua % ub)
            if sb == 0:
                return 0
            rrem = abs(sa) % abs(sb)
            return _to_signed(-rrem if sa < 0 else rrem)
        if op == '<<': return _to_signed(_wrap(sa << (sb & 63)))
        if op == '>>':
            if unsigned:
                return _to_signed(_wrap(a) >> (sb & 63))
            return _to_signed(sa >> (sb & 63))
        if op == '&':  return _to_signed(_wrap(a) & _wrap(b))
        if op == '|':  return _to_signed(_wrap(a) | _wrap(b))
        if op == '^':  return _to_signed(_wrap(a) ^ _wrap(b))
        # comparisons occasionally lowered as a binop yielding 0/1
        if op in ('<', '<=', '>', '>=', '==', '!='):
            return 1 if self._compare(op, a, b) else 0
        raise Unsupported(f'binop {op}')

    def _unop(self, op, a):
        if op == '-':  return _to_signed(-_to_signed(a))
        if op == '~':  return _to_signed(~_wrap(a))
        if op == '!':  return 1 if _to_signed(a) == 0 else 0
        raise Unsupported(f'unaryop {op}')

    def _cast(self, dest_type, a):
        t = dest_type.replace('$', '')
        unsigned = t.startswith('u')
        width = {'i8': 1, 'u8': 1, 'i16': 2, 'u16': 2, 'i32': 4, 'u32': 4,
                 'i64': 8, 'u64': 8}.get(t)
        if width is None:
            raise Unsupported(f'cast {dest_type}')
        return _trunc(a, width, unsigned)


# ── differential driver ───────────────────────────────────────────────────────

def run_slice(instrs, lo, hi, init_mem=None, step_limit=2_000_000):
    """Execute one function slice; return (return_value, final_mem_dict)."""
    mem = dict(init_mem) if init_mem else {}
    ret = Interp(instrs, step_limit).run_function(lo, hi, mem)
    return ret, mem


def differential(orig_instrs, unr_instrs, lo, hi, init_mem=None):
    """Run the SAME function slice (same [lo, hi]) in both programs from an
    identical initial memory and compare observable behaviour.

    Returns ('match'|'mismatch'|'unsupported', detail). 'unsupported' means the
    oracle could not execute the function (calls/floats/etc.) -- it is neither a
    pass nor a fail. Requires the two programs share the slice's [lo, hi] range,
    which holds when only the *tail* of the slice grows (unrolling appends the
    remainder loop at the slice end and splices inside the body -- the caller
    passes each program its own hi)."""
    seed = _preload_globals(orig_instrs)
    if init_mem:
        seed.update(init_mem)
    try:
        r0, m0 = run_slice(orig_instrs, *lo_hi(orig_instrs, lo), init_mem=seed)
    except (Unsupported, StepLimit) as e:
        return 'unsupported', f'orig: {e}'
    try:
        seed2 = _preload_globals(unr_instrs)
        if init_mem:
            seed2.update(init_mem)
        r1, m1 = run_slice(unr_instrs, *lo_hi(unr_instrs, lo, by_name=True,
                                              fname=_fname(orig_instrs, lo)),
                           init_mem=seed2)
    except (Unsupported, StepLimit) as e:
        return 'unsupported', f'unrolled: {e}'
    if r0 != r1:
        return 'mismatch', f'return {r0} != {r1}'
    if m0 != m1:
        diff = {k: (m0.get(k), m1.get(k)) for k in set(m0) | set(m1)
                if m0.get(k) != m1.get(k)}
        return 'mismatch', f'memory differs at {sorted(diff)[:6]} -> {list(diff.items())[:6]}'
    return 'match', f'return={r0}'


def _fname(instrs, lo):
    return instrs[lo].name if type(instrs[lo]).__name__ == 'IRFuncBegin' else None


def lo_hi(instrs, lo, by_name=False, fname=None):
    """Resolve the (lo, hi) of the function slice. When `by_name`, locate the
    slice whose IRFuncBegin name matches `fname` (its `lo` may have shifted in the
    transformed program if earlier functions grew -- they do not here, but this
    keeps the harness robust)."""
    from ir_utils import func_slices
    if by_name and fname is not None:
        for a, b in func_slices(instrs):
            if instrs[a].name == fname:
                return a, b
    for a, b in func_slices(instrs):
        if a == lo:
            return a, b
    return lo, len(instrs) - 1

"""
memory_objects.py -- symbolic memory addresses and the independence decision
procedure (Milestone R6.2).

--------------------------------------------------------------------------------
THE PROBLEM R6.1 MEASURED
--------------------------------------------------------------------------------
`bundler._mem_may_alias` proves two accesses independent in exactly one
situation: the SAME base register with DIFFERENT CONSTANT offsets. Anything else
-- two different base registers, a register offset, an unrolled copy with its own
index -- is treated as a possible alias, deliberately, because two registers can
hold the same address.

R6.1 measured what that costs: unrolling a vector loop bought 1.24x, and the same
unroll with distinct-object information bought 2.57x, because every store
serialised against the next copy's loads.

--------------------------------------------------------------------------------
THE MODEL
--------------------------------------------------------------------------------
An address is a symbolic affine expression

        sum(coeff_k * sym_k)  +  constant

where each `sym` is an opaque VALUE (a register's content at a definition point,
the frame pointer, an unknown loaded word). Two accesses are compared by
SUBTRACTING their expressions:

  * every symbol cancels  ->  the difference is a known integer `d`, and
                              independence is decided by comparing `d` against
                              the access widths;
  * any symbol survives   ->  the difference is unknown  ->  MAY ALIAS.

This subsumes base+constant reasoning rather than replacing it, and it needs no
knowledge of object extents:

    A[i] vs B[i]        FP-64+v  vs  FP-128+v      -> d = 64      independent
    A[i] vs A[i+8]      FP-64+v  vs  FP-64+v+8     -> d = 8       independent
    A[i] vs A[i+VL]     unrolled copies of one loop-> d = k*VL    independent
    base+i*s vs base+(i+k)*s                       -> d = k*s     independent
    A[i] vs B[j]        FP-64+v  vs  FP-128+w      -> w survives  MAY ALIAS

--------------------------------------------------------------------------------
CORRECTNESS
--------------------------------------------------------------------------------
Independence is proved or it is not claimed. Every path that cannot establish a
constant difference returns MAY_ALIAS. Specifically:

  * an unrecognised instruction defining a register yields a FRESH opaque symbol,
    never an assumption about its value;
  * only 64-bit (`$i64`/`$u64`) address arithmetic is interpreted. Narrower adds
    wrap at their own width, and modelling them as unbounded integers would be
    unsound, so they produce opaque symbols;
  * `$set` is treated as opaque: it writes a 16-bit FIELD, and a register can be
    assembled from several `$set`s;
  * the model uses unbounded integers, so it assumes address arithmetic does not
    wrap around 2^64. Every address here lives in a 64 KB DMEM; the existing
    textual rule makes the same assumption when it calls `[r+0]` and `[r+8]`
    distinct.

SUB-WORD STORES. DMEM is 64-bit wide and a narrower store is a read-modify-write
of its containing word. Two sub-word stores that land in the same word therefore
race even though their byte ranges are disjoint. Byte-range disjointness alone is
enough for the existing textual rule (same base, different constant offsets, both
naturally aligned by construction), but this module can now relate accesses
through DIFFERENT base registers, where that guarantee does not exist. So when a
pair involves a store narrower than a full word, independence additionally
requires a full 8-byte separation. This can only make a proof rarer; it never
weakens one.
"""

# decision outcomes
INDEPENDENT = 'independent'      # proved disjoint -- safe to reorder / co-issue
MAY_ALIAS = 'may-alias'          # not proved -- the caller must be conservative
MUST_ALIAS = 'must-alias'        # proved to be the identical location

WORD_BYTES = 8                   # DMEM word width (sub-word stores are RMW)


class SymAddr:
    """An affine symbolic address: `sum(coeff * sym) + const`, or UNKNOWN.

    `terms` maps an opaque symbol to a non-zero integer coefficient. An address
    with `ok=False` is unknown and never proves anything."""

    __slots__ = ('terms', 'const', 'ok')

    def __init__(self, terms=None, const=0, ok=True):
        self.terms = {k: v for k, v in (terms or {}).items() if v}
        self.const = const
        self.ok = ok

    # -- constructors ----------------------------------------------------------
    @staticmethod
    def unknown():
        return SymAddr(ok=False)

    @staticmethod
    def constant(c):
        return SymAddr({}, c)

    @staticmethod
    def symbol(sym, coeff=1, const=0):
        return SymAddr({sym: coeff}, const)

    # -- algebra ---------------------------------------------------------------
    def add(self, other):
        if not (self.ok and other.ok):
            return SymAddr.unknown()
        t = dict(self.terms)
        for k, v in other.terms.items():
            t[k] = t.get(k, 0) + v
        return SymAddr(t, self.const + other.const)

    def sub(self, other):
        if not (self.ok and other.ok):
            return SymAddr.unknown()
        t = dict(self.terms)
        for k, v in other.terms.items():
            t[k] = t.get(k, 0) - v
        return SymAddr(t, self.const - other.const)

    def scale(self, k):
        if not self.ok:
            return SymAddr.unknown()
        return SymAddr({s: c * k for s, c in self.terms.items()}, self.const * k)

    def offset(self, c):
        if not self.ok:
            return SymAddr.unknown()
        return SymAddr(dict(self.terms), self.const + c)

    def is_constant(self):
        return self.ok and not self.terms

    def __repr__(self):
        if not self.ok:
            return 'UNKNOWN'
        parts = [f"{c:+d}*{s}" for s, c in sorted(self.terms.items(),
                                                  key=lambda kv: str(kv[0]))]
        if self.const or not parts:
            parts.append(f"{self.const:+d}")
        return ''.join(parts).lstrip('+') or '0'


def difference(a, b):
    """The integer `addr(a) - addr(b)`, or None when it is not a known constant.

    None is the honest answer for "the two addresses are not related by a
    compile-time constant" -- which is the only case that can prove anything."""
    d = a.sub(b)
    return d.const if d.is_constant() else None


class MemRef:
    """One memory access: where it lands, how wide it is, and whether it writes.

    `origin` is free-form provenance carried purely for reporting -- the
    decision procedure never reads it."""

    __slots__ = ('addr', 'width', 'is_write', 'origin')

    def __init__(self, addr, width, is_write, origin=None):
        self.addr = addr
        self.width = max(1, int(width))
        self.is_write = bool(is_write)
        self.origin = origin

    def __repr__(self):
        return (f"{'ST' if self.is_write else 'LD'}{self.width}[{self.addr!r}]")


def classify(a, b):
    """Independence of two memory accesses executed at the SAME iteration point.

    Returns INDEPENDENT / MUST_ALIAS / MAY_ALIAS. Unknown addresses, unrelated
    symbols and every unhandled shape fall through to MAY_ALIAS."""
    if a is None or b is None:
        return MAY_ALIAS
    d = difference(a.addr, b.addr)
    if d is None:
        return MAY_ALIAS
    if d == 0 and a.width == b.width:
        return MUST_ALIAS
    return _ranges_disjoint(d, a, b)


def _ranges_disjoint(d, a, b):
    """`a` sits `d` bytes above `b`. Disjoint iff the byte ranges do not meet --
    with the sub-word-store guard from the module docstring applied."""
    sep = abs(d)
    if (a.is_write or b.is_write) and min(a.width, b.width) < WORD_BYTES:
        # a narrow store is a read-modify-write of its whole 64-bit word
        return INDEPENDENT if sep >= WORD_BYTES else MAY_ALIAS
    if d >= b.width or -d >= a.width:
        return INDEPENDENT
    return MAY_ALIAS


def classify_carried(a, b, iv_sym, step):
    """Independence of two accesses executed in DIFFERENT iterations of a loop
    whose induction value `iv_sym` advances by `step` each time.

    For iterations separated by delta != 0 the address difference is

        d(delta) = (addr_a - addr_b) + k * step * delta

    where `k` is the IV coefficient, which must be the SAME in both accesses for
    the symbol to cancel. If any other symbol survives, or the coefficients
    differ, nothing is proved. Otherwise the finitely many `delta` that could
    bring the ranges together are enumerated and checked -- this generalises the
    R2.2 same-base SIV rule to arbitrary affine addresses and to real widths."""
    if a is None or b is None or not (a.addr.ok and b.addr.ok):
        return MAY_ALIAS
    ka = a.addr.terms.get(iv_sym, 0)
    kb = b.addr.terms.get(iv_sym, 0)
    if ka != kb or ka == 0 or not step:
        return MAY_ALIAS
    rest = a.addr.sub(b.addr)
    if rest.terms.get(iv_sym, 0):
        return MAY_ALIAS
    rest = SymAddr({s: c for s, c in rest.terms.items() if s != iv_sym},
                   rest.const)
    if not rest.is_constant():
        return MAY_ALIAS
    c, adv = rest.const, ka * step
    span = max(a.width, b.width, WORD_BYTES)
    lo = -(span + abs(c)) // abs(adv) - 2
    for delta in range(lo, -lo + 1):
        if delta == 0:
            continue
        if _ranges_disjoint(c + adv * delta, a, b) != INDEPENDENT:
            return MAY_ALIAS
    return INDEPENDENT

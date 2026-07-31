"""
mem_dependence.py -- front-ends that turn real code into symbolic memory
references, and the interfaces its two consumers use (Milestone R6.2).

ONE analysis, TWO front-ends, TWO consumers -- no parallel alias framework:

    mcode front-end  --\\                            /-->  bundler.py
                        >-- memory_objects.classify -<
    IR front-end     --/                            \\-->  DependenceGraph
                                                          (via the disambiguator
                                                           hook it already has)

The decision procedure lives in `memory_objects.py` and is shared. These
front-ends only answer "what address does this access compute?".

--------------------------------------------------------------------------------
THE mcode FRONT-END, AND WHY IT IS BLOCK-LOCAL
--------------------------------------------------------------------------------
The bundler works on register-allocated instruction text, where the only thing
distinguishing two addresses is the arithmetic that produced them. So the front
end symbolically evaluates that arithmetic:

    + $r7 ($i64) $r26 -128     ->   r7 = FP - 128
    + $r3 ($i64) $r3 8         ->   r3 = <r3 on entry> + 8
    $ld ($i64) $r9 [$r6 + $r3] ->   address = <r6 on entry> + <r3 on entry>

Evaluation is scoped to ONE BASIC BLOCK, with every register live into the block
treated as an opaque symbol. That is not a limitation: the bundler packs and the
scheduler reorders within a basic block and nowhere else, so block scope is
exactly the scope in which the answer is used. It also removes any need to reason
about loop back-edges -- a loop-carried register is simply opaque.

SOUNDNESS UNDER REORDERING. The refs are computed on the pre-scheduling order,
and the scheduler then moves instructions. This stays valid because
`bundler._must_precede` unconditionally keeps every register RAW, WAR and WAW
dependence, so no definition can cross a use or another definition of the same
register. The symbolic value of a register at a given instruction is therefore
invariant under any schedule the bundler is allowed to produce.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .memory_objects import (SymAddr, MemRef, classify, classify_carried,   # noqa: E402
                             INDEPENDENT, MAY_ALIAS, MUST_ALIAS)

# ── mcode instruction shapes ──────────────────────────────────────────────────
_LDST = re.compile(r'^\$(ld|st)\s+\(\$[iuf](\d+)\)\s+'
                   r'(?:(\$r\d+)\s+)?\[(\$r\d+)\s*\+\s*(-?\d+|\$r\d+)\]')
# 64-bit address arithmetic only: a narrower add wraps at its own width, and
# modelling it with unbounded integers would be unsound.
_ALU = re.compile(r'^([+\-]|<<|\*)\s+(\$r\d+)\s+\(\$[iu]64\)\s+(\$r\d+)\s+'
                  r'(-?\d+|\$r\d+)$')

ZERO_REG = '$r0'          # hardware constant zero


def _width_bytes(nbits):
    return max(1, int(nbits) // 8)


class BlockSymbols:
    """Symbolic register values through one basic block."""

    def __init__(self, block_id):
        self.block_id = block_id
        self.vals = {}
        self.serial = 0

    def live_in(self, reg):
        """The opaque value a register holds on entry to this block."""
        return SymAddr.symbol(('in', self.block_id, reg))

    def get(self, reg):
        if reg == ZERO_REG:
            return SymAddr.constant(0)
        if reg not in self.vals:
            self.vals[reg] = self.live_in(reg)
        return self.vals[reg]

    def opaque(self, reg):
        """Definition by an instruction this analysis does not interpret. A
        FRESH symbol -- never an assumption about the value."""
        self.serial += 1
        self.vals[reg] = SymAddr.symbol(('def', self.block_id, reg, self.serial))

    def define(self, reg, val):
        self.vals[reg] = val

    def operand(self, tok):
        """A `$rN` register or a decimal literal, as a symbolic value."""
        if tok.startswith('$r'):
            return self.get(tok)
        return SymAddr.constant(int(tok))


def _eval_alu(sym, text):
    """The symbolic value an interpretable 64-bit ALU instruction defines, or
    None when the instruction is not one this analysis understands."""
    m = _ALU.match(text.strip())
    if not m:
        return None, None
    op, rd, ra, rb = m.group(1), m.group(2), m.group(3), m.group(4)
    a = sym.get(ra)
    if op == '+':
        return rd, a.add(sym.operand(rb))
    if op == '-':
        return rd, a.sub(sym.operand(rb))
    if op == '<<' and not rb.startswith('$r'):
        sh = int(rb)
        return (rd, a.scale(1 << sh)) if 0 <= sh < 32 else (rd, None)
    if op == '*' and not rb.startswith('$r'):
        return rd, a.scale(int(rb))
    return rd, None


def annotate_block(entries, block_id, carried=None, single=None):
    """Attach a `mem_ref` to every memory access in one basic block.

    `entries` are the bundler's parsed instruction dicts, in program order.
    Mutates each dict in place (adding `mem_ref`), which is how the information
    travels with the instruction through scheduling. `carried` supplies the
    values of single-definition registers established in earlier blocks (see
    `annotate`). Returns the block's symbol state."""
    sym = BlockSymbols(block_id)
    if carried:
        sym.vals.update(carried)
    for e in entries:
        text = e['text'].strip()
        m = _LDST.match(text)  # noqa: E501
        if m:
            kind, nbits, rd, base, off = m.groups()
            addr = sym.get(base).add(sym.operand(off))
            e['mem_ref'] = MemRef(addr, _width_bytes(nbits),
                                  is_write=(kind == 'st'), origin=text)
        else:
            e['mem_ref'] = None
        # then update the register state with whatever this instruction defines
        rd, val = _eval_alu(sym, text)
        if rd is not None and val is not None and val.ok:
            sym.define(rd, val)
            continue
        for w in e['writes']:
            sym.opaque(w)
    return sym


def annotate(flat):
    """Attach `mem_ref` to every instruction of a whole program.

    Basic blocks are cut exactly where the bundler cuts them: a labelled
    instruction opens one, a control transfer closes one.

    CARRYING VALUES ACROSS BLOCKS. Block-local evaluation alone cannot relate two
    array base registers, because LICM hoists `FP + k` into the loop PREHEADER --
    so inside the loop body every base is an unrelated live-in and nothing is
    provable. That is not a theoretical gap: it is the difference between a
    compact vector loop gaining nothing and gaining everything.

    A register may keep its symbolic value across block boundaries only when it
    is written exactly once AND its value is expressed purely in terms of
    FUNCTION-ENTRY live-ins -- the frame/stack pointer as they are on entry.

    The entry-live-in condition is not optional, and the original R6.2 rule
    (single static write alone) was UNSOUND without it. "Written once" is a
    STATIC count: a definition inside a loop body executes on every iteration,
    and its expression is written in terms of that block's live-in symbols,
    which denote a DIFFERENT concrete value each time round. Carrying such a
    value into another block lets two addresses cancel symbols that were never
    equal at the same moment, so the model can report "independent" for accesses
    that genuinely alias. Measured: it let a later scalar load of `out[11]` be
    reordered above the vector stores that write it, and the kernel read 0.

    A value built only from entry live-ins is loop-invariant by construction --
    it has the same concrete value on every iteration and after every loop -- so
    carrying it is safe. That is also exactly the case the carry exists for:
    LICM hoists array bases into the preheader as `FP + constant`.

    Multiply-defined registers -- induction variables, accumulators, anything a
    loop updates -- still get a FRESH opaque symbol per block."""
    counts = {}
    for e in flat:
        for w in e['writes']:
            counts[w] = counts.get(w, 0) + 1
    single = {r for r, n in counts.items() if n == 1}

    block, blocks, prev_ctrl = [], [], False
    for e in flat:
        if block and (e['labels'] or prev_ctrl):
            blocks.append(block)
            block = []
        block.append(e)
        prev_ctrl = e['is_ctrl']
    if block:
        blocks.append(block)

    def _entry_only(addr):
        """True when `addr` depends on nothing but function-entry live-ins, so
        it has the same value on every iteration and after every loop."""
        return addr.ok and all(s[0] == 'in' and s[1] == 0 for s in addr.terms)

    carried = {}
    for i, blk in enumerate(blocks):
        sym = annotate_block(blk, i, carried=carried, single=single)
        for reg in single:
            v = sym.vals.get(reg)
            if v is not None and _entry_only(v):
                carried[reg] = v
    return flat


# ── consumer 1: the bundler ───────────────────────────────────────────────────

def independent(a, b):
    """True only when the two instructions provably touch disjoint memory.

    This is the single entry point `bundler.py` uses. Everything unproven --
    a missing ref, an unknown address, an unrelated symbol -- is False, so the
    bundler falls back to its existing textual rule."""
    if a is None or b is None:
        return False
    ra, rb = a.get('mem_ref'), b.get('mem_ref')
    if ra is None or rb is None:
        return False
    return classify(ra, rb) == INDEPENDENT


def explain(a, b):
    """(verdict, difference) for reporting. Never consulted for a decision."""
    ra, rb = (a or {}).get('mem_ref'), (b or {}).get('mem_ref')
    if ra is None or rb is None:
        return MAY_ALIAS, None
    from .memory_objects import difference
    return classify(ra, rb), difference(ra.addr, rb.addr)


# ── consumer 2: the IR dependence graph ───────────────────────────────────────

def _ir_symbol(name):
    return ('t', name)


class IRAddressModel:
    """Symbolic addresses for the memory accesses of one IR function slice.

    IR temporaries are effectively single-assignment within a slice, so a temp
    can be expanded transitively; anything not expandable becomes an opaque
    symbol named after the temp. Reuses no numbering of its own -- the algebra
    and the decision procedure are `memory_objects`'."""

    def __init__(self, instrs, lo, hi):
        self.instrs = instrs
        self.lo, self.hi = lo, hi
        self._def = {}
        self._cache = {}
        for k in range(lo, hi + 1):
            ins = instrs[k]
            d = getattr(ins, 'dest', None)
            if d is not None and hasattr(d, 'name'):
                self._def.setdefault(d.name, k)

    def value(self, operand, depth=0):
        from ir import Const, Temp
        if isinstance(operand, Const):
            return SymAddr.constant(int(operand.value))
        if not isinstance(operand, Temp):
            return SymAddr.unknown()
        name = operand.name
        if name in self._cache:
            return self._cache[name]
        if depth > 12 or name not in self._def:
            v = SymAddr.symbol(_ir_symbol(name))
        else:
            v = self._expand(self._def[name], name, depth)
        self._cache[name] = v
        return v

    def _expand(self, k, name, depth):
        ins = self.instrs[k]
        c = type(ins).__name__
        if c == 'IRLoadAddr':
            # the address of a frame slot: FP + offset, one distinct object per
            # slot but expressed in the SAME symbol so slots are comparable
            return SymAddr.symbol(('fp',), 1, int(ins.offset))
        if c == 'IRGlobalAddrOf':
            return SymAddr.constant(int(ins.dmem_addr))
        if c == 'IRAssign':
            return self.value(ins.src, depth + 1)
        if c == 'IRBinOp':
            L = self.value(ins.left, depth + 1)
            R = self.value(ins.right, depth + 1)
            if ins.op == '+':
                return L.add(R)
            if ins.op == '-':
                return L.sub(R)
            if ins.op == '*' and R.is_constant():
                return L.scale(R.const)
            if ins.op == '*' and L.is_constant():
                return R.scale(L.const)
            if ins.op == '<<' and R.is_constant() and 0 <= R.const < 32:
                return L.scale(1 << R.const)
        return SymAddr.symbol(_ir_symbol(name))

    def ref(self, k):
        """The MemRef of IR instruction k, or None if it is not a memory access."""
        ins = self.instrs[k]
        c = type(ins).__name__
        eb = getattr(ins, 'elem_bytes', 8) or 8
        if c in ('IRLoad', 'IRStore'):
            addr = self.value(ins.base).add(self.value(ins.offset))
            return MemRef(addr, eb, is_write=(c == 'IRStore'), origin=repr(ins))
        if c in ('IRLoadWide', 'IRStoreWide'):
            n = len(getattr(ins, 'dests', None) or getattr(ins, 'srcs', []))
            addr = self.value(ins.base).add(self.value(ins.offset))
            return MemRef(addr, 8 * max(1, n), is_write=(c == 'IRStoreWide'),
                          origin=repr(ins))
        if c in ('IRGlobalLoad', 'IRGlobalStore'):
            addr = SymAddr.constant(int(ins.dmem_addr)).add(self.value(ins.offset))
            return MemRef(addr, eb, is_write=(c == 'IRGlobalStore'),
                          origin=repr(ins))
        return None


class StrongDisambiguator:
    """The R2.2 `MemoryDisambiguator` refined by the symbolic model.

    Integration is through the hook `DependenceGraph` ALREADY exposes
    (`disambiguator=`), so nothing in the frozen R2.1/R2.2 code changes and any
    pass that builds a graph can opt in by passing this instead.

    Order of consultation:
      1. R2.2 first. If it proves disjoint or must-alias, that verdict stands --
         its clean-slot and distinct-object rules use escape information this
         model does not have.
      2. Only when R2.2 says "maybe" does the symbolic model get a turn, and only
         a proof of independence is taken from it.
    A refinement can therefore only ever REMOVE a conservative edge, never add
    one and never contradict R2.2."""

    def __init__(self, base, instrs, lo, hi, iv_sym=None, iv_step=None):
        self.base = base
        self.model = IRAddressModel(instrs, lo, hi)
        self.iv_sym = iv_sym
        self.iv_step = iv_step
        self.stats = {'r22': 0, 'symbolic': 0, 'maybe': 0}

    def classify(self, i, j, carried):
        v = self.base.classify(i, j, carried) if self.base is not None else None
        if v is not None and (v.disjoint or v.proven):
            self.stats['r22'] += 1
            return v
        a, b = self.model.ref(i), self.model.ref(j)
        verdict = MAY_ALIAS
        if not carried:
            verdict = classify(a, b)
        elif self.iv_sym is not None:
            verdict = classify_carried(a, b, self.iv_sym, self.iv_step)
        if verdict == INDEPENDENT:
            self.stats['symbolic'] += 1
            return _Verdict(True, False, 'symbolic-affine-disjoint')
        self.stats['maybe'] += 1
        return v if v is not None else _Verdict(False, False, 'unknown')


class _Verdict:
    """Mirrors `loopopt.depgraph_disambig.Verdict`'s shape (duck-typed by the
    graph: `.disjoint`, `.proven`, `.reason`)."""
    __slots__ = ('disjoint', 'proven', 'reason')

    def __init__(self, disjoint, proven, reason):
        self.disjoint = disjoint
        self.proven = proven
        self.reason = reason

    def __repr__(self):
        return f"Verdict({self.disjoint},{self.proven},{self.reason})"

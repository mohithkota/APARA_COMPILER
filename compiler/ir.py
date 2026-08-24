"""
APARA Compiler — Three-Address IR  (v2: fully general)
"""

# ─── Operands ─────────────────────────────────────────────────────────────────

class Const:
    def __init__(self, value): self.value = int(value)
    def __str__(self): return str(self.value)

class Temp:
    _n = 0
    def __init__(self, name=None):
        if name: self.name = name
        else: Temp._n += 1; self.name = f"_t{Temp._n}"
    def __str__(self): return self.name
    @staticmethod
    def reset(): Temp._n = 0

# ─── Instructions ─────────────────────────────────────────────────────────────

class IRGlobalDecl:
    """A global variable in DMEM."""
    def __init__(self, name, dmem_addr, total_bytes, elem_bytes, init=None, stride=None):
        self.name       = name
        self.dmem_addr  = dmem_addr   # absolute byte addr in DMEM
        self.total_bytes= total_bytes
        self.elem_bytes = elem_bytes  # C type size (drives instruction type: $i32 vs $i64)
        # APARA DMEM: $ld ($i32) always reads bits[63:32] of the 8-byte word.
        # Every element must sit at byte_off=0 of its own 8-byte DMEM word.
        self.stride     = stride if stride is not None else max(elem_bytes, 8)
        self.init       = init or []  # flat list of init values
    def __repr__(self): return f"GLOBAL {self.name} @0x{self.dmem_addr:x} ({self.total_bytes}B stride={self.stride})"

class IRFuncBegin:
    def __init__(self, name, params, var_offsets, frame_size):
        self.name        = name
        self.params      = params        # [(name, fp_offset), ...]
        self.var_offsets = var_offsets   # {name: fp_offset}
        self.frame_size  = frame_size
    def __repr__(self): return f"FUNC_BEGIN {self.name} frame={self.frame_size}"

class IRFuncEnd:
    def __init__(self, name): self.name = name
    def __repr__(self): return f"FUNC_END {self.name}"

class IRLabel:
    def __init__(self, name): self.name = name
    def __repr__(self): return f"{self.name}:"

class IRAssign:
    """dest = src"""
    def __init__(self, dest, src): self.dest = dest; self.src = src
    def __repr__(self): return f"{self.dest} = {self.src}"

class IRBinOp:
    """dest = left op right.  `unsigned` selects the $u type tag where signedness
    changes the operation (e.g. '>>' logical vs arithmetic)."""
    def __init__(self, dest, op, left, right, unsigned=False, ftype=None):
        self.dest = dest; self.op = op; self.left = left; self.right = right
        self.unsigned = unsigned
        self.ftype = ftype        # '$f32'/'$f64' for float arithmetic, else None
    def __repr__(self): return f"{self.dest} = {self.left} {self.op} {self.right}"

class IRUnaryOp:
    """dest = op operand"""
    def __init__(self, dest, op, operand):
        self.dest = dest; self.op = op; self.operand = operand
    def __repr__(self): return f"{self.dest} = {self.op}{self.operand}"

class IRLoadAddr:
    """dest = FP + fp_offset  (address of a local/param stack slot)"""
    def __init__(self, dest, fp_offset): self.dest = dest; self.fp_offset = fp_offset
    def __repr__(self): return f"{self.dest} = &stack[FP{self.fp_offset:+d}]"

class IRLoad:
    """dest = mem[base + offset]. unsigned picks $u (zero-extend) over the
    default $i (sign-extend) for the < 8-byte-width type tag -- store doesn't
    need this (the grammar ignores $u on $st, truncation is the same either
    way), only load has a sign/zero-extension boundary to get right."""
    def __init__(self, dest, base, offset, elem_bytes, unsigned=False):
        self.dest = dest; self.base = base; self.offset = offset
        self.elem_bytes = elem_bytes
        self.unsigned = unsigned
    def __repr__(self): return f"{self.dest} = *({self.base}+{self.offset})"

class IRStore:
    """mem[base + offset] = src"""
    def __init__(self, base, offset, src, elem_bytes):
        self.base = base; self.offset = offset; self.src = src
        self.elem_bytes = elem_bytes
    def __repr__(self): return f"*({self.base}+{self.offset}) = {self.src}"

class IRLoadWide:
    """dests = load len(dests)*64 bits from mem[base + offset] as a register
    group (len(dests)==2 for $ld ($u128), ==4 for $ld ($u256)). A single wide
    $ld fills all of dests in one instruction -- the registers must be
    consecutive and start-index-aligned per the ISA's hardware requirement
    (even for a pair, multiple of 4 for a quad; see codegen.py RegAlloc)."""
    def __init__(self, dests, base, offset):
        self.dests = dests  # list of Temp, length 2 or 4
        self.base = base; self.offset = offset
    def __repr__(self):
        return f"{':'.join(str(d) for d in self.dests)} = *wide({self.base}+{self.offset})"

class IRStoreWide:
    """*wide(base+offset) = srcs: store len(srcs)*64 bits to mem[base + offset]
    from a register group (len(srcs)==2 for $st ($u128), ==4 for $st ($u256)).
    Mirrors IRLoadWide in reverse -- same consecutive/aligned register-group
    requirement, confirmed from the assembler grammar (isa.g
    mcode_store_instruction): $st takes a single rd token just like $ld, so
    the hardware reads rd..rd+n-1 as the source group for the write."""
    def __init__(self, srcs, base, offset):
        self.srcs = srcs  # list of Temp/Const/value, length 2 or 4
        self.base = base; self.offset = offset
    def __repr__(self):
        return f"*wide({self.base}+{self.offset}) = {':'.join(str(s) for s in self.srcs)}"

class IRGlobalLoad:
    """dest = DMEM[dmem_addr + offset]  (global variable access). See IRLoad
    for why `unsigned` only matters here, not on IRGlobalStore."""
    def __init__(self, dest, dmem_addr, offset=None, *, elem_bytes, unsigned=False):
        self.dest = dest; self.dmem_addr = dmem_addr
        self.offset = offset or Const(0); self.elem_bytes = elem_bytes
        self.unsigned = unsigned
    def __repr__(self): return f"{self.dest} = DMEM[0x{self.dmem_addr:x}+{self.offset}]"

class IRGlobalStore:
    """DMEM[dmem_addr + offset] = src"""
    def __init__(self, dmem_addr, offset, src, elem_bytes):
        self.dmem_addr = dmem_addr; self.offset = offset
        self.src = src; self.elem_bytes = elem_bytes
    def __repr__(self): return f"DMEM[0x{self.dmem_addr:x}+{self.offset}] = {self.src}"

class ArrayBase:
    """Where a vectorizable array lives (R16.2).

    The vector lowering used to identify an array by its stack slot alone -- an
    `fp_offset` int that every use site turned back into an address with
    `IRLoadAddr`. That hard-codes ONE storage class, which is why a matrix in
    fixed DMEM produced `pattern:array-bases-not-extracted` and fell back to
    scalar.

    This carries the same information polymorphically: `kind` is 'stack' or
    'global', and `emit(temp)` materialises the base address into `temp` with
    whichever node that storage class needs. Everything else in the lowering --
    contiguity, constant deltas, immediates, store grouping -- is unchanged,
    because they all operate on the OFFSET, not on where the base came from.

    Compares and hashes by (kind, value) so it can be used as a dict key and
    compared for identity exactly as the bare int was.
    """

    __slots__ = ('kind', 'value')

    def __init__(self, kind, value):
        assert kind in ('stack', 'global')
        self.kind = kind
        self.value = value

    @staticmethod
    def stack(fp_offset):
        return ArrayBase('stack', fp_offset)

    @staticmethod
    def glob(dmem_addr):
        return ArrayBase('global', dmem_addr)

    def emit(self, temp):
        """The IR instruction that puts this base address into `temp`."""
        if self.kind == 'stack':
            return IRLoadAddr(temp, self.value)
        return IRGlobalAddrOf(temp, self.value, Const(0))

    def __eq__(self, other):
        if isinstance(other, ArrayBase):
            return (self.kind, self.value) == (other.kind, other.value)
        # a bare int still means a stack slot, so pre-R16.2 comparisons hold
        return self.kind == 'stack' and self.value == other

    def __hash__(self):
        return hash((self.kind, self.value))

    def __repr__(self):
        return (f"stack[{self.value}]" if self.kind == 'stack'
                else f"DMEM[0x{self.value:x}]")


def emit_array_base(temp, base):
    """Materialise an array base into `temp`.

    Accepts an `ArrayBase` or a bare int (a stack `fp_offset`), so every
    pre-R16.2 caller keeps working unchanged while global-backed arrays become
    expressible.
    """
    if isinstance(base, ArrayBase):
        return base.emit(temp)
    return IRLoadAddr(temp, base)


class IRGlobalAddrOf:
    """dest = address of global (dmem_addr + optional offset)"""
    def __init__(self, dest, dmem_addr, offset=None):
        self.dest = dest; self.dmem_addr = dmem_addr
        self.offset = offset or Const(0)
    def __repr__(self): return f"{self.dest} = &DMEM[0x{self.dmem_addr:x}]"

class IRCondJump:
    """if left op right goto true_label [else false_label]"""
    def __init__(self, left, op, right, true_label, false_label=None, ftype=None):
        self.left = left; self.op = op; self.right = right
        self.true_label = true_label; self.false_label = false_label
        self.ftype = ftype        # '$f32'/'$f64' for a float comparison, else None
    def __repr__(self): return f"if {self.left} {self.op} {self.right} goto {self.true_label}"

class IRJump:
    """goto label"""
    def __init__(self, label): self.label = label
    def __repr__(self): return f"goto {self.label}"

class IRCall:
    """dest = func(args)

    n_reg: for a call to a VARIADIC function, how many leading args go in
    registers (the named parameters); the rest are stored to the stack just
    below the caller's SP so the callee finds them at [FP + 8 + 8*i].
    None (default) = non-variadic call, all args in registers.
    All args stay in .args regardless, so operand/liveness scans that walk
    list(ir.args) (codegen, licm) see the stack-passed ones too."""
    def __init__(self, dest, func_name, args, n_reg=None):
        self.dest = dest; self.func_name = func_name; self.args = args
        self.n_reg = n_reg
    def __repr__(self):
        return f"{self.dest} = {self.func_name}({', '.join(str(a) for a in self.args)})"

class IRVaStart:
    """dest = address of the first stack-passed variadic argument (FP + offset;
    offset is 8 plus 8 per stack-passed NAMED param when the variadic function
    itself has more than 4 named params)."""
    def __init__(self, dest, offset=8): self.dest = dest; self.offset = offset
    def __repr__(self): return f"{self.dest} = va_start()  /* FP+{self.offset} */"

class IRReturn:
    """return [value]"""
    def __init__(self, value=None): self.value = value
    def __repr__(self): return f"return {self.value}"

class IRHalt:
    def __repr__(self): return "HALT"

# ─── New ISA Instructions ──────────────────────────────────────────────────────

class IRCast:
    """dest = $cast(dest_type) src  — type conversion"""
    def __init__(self, dest, src, dest_type, src_type='$i64'):
        self.dest = dest; self.src = src
        self.dest_type = dest_type; self.src_type = src_type
    def __repr__(self): return f"{self.dest} = cast({self.dest_type}) {self.src}"

class IRFsqrt:
    """dest = $fsqrt(type) src  — floating-point square root"""
    def __init__(self, dest, src, type_str='$f64'):
        self.dest = dest; self.src = src; self.type_str = type_str
    def __repr__(self): return f"{self.dest} = fsqrt({self.type_str}) {self.src}"

class IRCmov:
    """if check cond 0: dest = src_true  else: dest = src_false"""
    def __init__(self, dest, check, cond, src_true, src_false, type_str='$i64'):
        self.dest = dest; self.check = check; self.cond = cond
        self.src_true = src_true; self.src_false = src_false
        self.type_str = type_str
    def __repr__(self):
        return f"{self.dest} = cmov({self.check} {self.cond} 0 ? {self.src_true} : {self.src_false})"

class IRSlice:
    """dest = src[hindex:lindex]  — bit-field extract"""
    def __init__(self, dest, src, hindex, lindex):
        self.dest = dest; self.src = src
        self.hindex = int(hindex); self.lindex = int(lindex)
    def __repr__(self): return f"{self.dest} = slice({self.src}, {self.hindex}, {self.lindex})"

class IRPack:
    """dest = pack(src1, src2, result_nbits, src_nbits)  — pack two regs into one"""
    def __init__(self, dest, src1, src2, result_nbits, src_nbits):
        self.dest = dest; self.src1 = src1; self.src2 = src2
        self.result_nbits = int(result_nbits); self.src_nbits = int(src_nbits)
    def __repr__(self):
        return f"{self.dest} = pack({self.src1}, {self.src2}, {self.result_nbits}, {self.src_nbits})"

class IRVecArith:
    """dest = $v op (type_str) src1 src2 [$replicate]  — vector element-wise arithmetic"""
    def __init__(self, dest, op, src1, src2, type_str, replicate=False):
        self.dest = dest; self.op = op; self.src1 = src1; self.src2 = src2
        self.type_str = type_str; self.replicate = replicate
    def __repr__(self):
        return f"{self.dest} = $v {self.op} ({self.type_str}) {self.src1} {self.src2}"

class IRVecDot:
    """dest = $dot (type_str) src1 src2 [+ dest]  — vector dot product"""
    def __init__(self, dest, src1, src2, type_str, accumulate=False, accum=None):
        self.dest = dest; self.src1 = src1; self.src2 = src2
        self.type_str = type_str; self.accumulate = accumulate; self.accum = accum
    def __repr__(self): return f"{self.dest} = dot({self.type_str}) {self.src1} . {self.src2}"

class IRVecDot128:
    """
    dest = 16-element dot product across a u128-wide pair, split into the
    exact two-instruction pattern confirmed from the 16x16 reference
    (log.txt): a plain $dot on the lo halves, then $dot $accumulate on the
    hi halves into the same dest. a_lo/a_hi hold elements 0-7/8-15 of vector
    A; b_lo/b_hi the same for vector B.
    """
    def __init__(self, dest, a_lo, a_hi, b_lo, b_hi, type_str):
        self.dest = dest
        self.a_lo = a_lo; self.a_hi = a_hi; self.b_lo = b_lo; self.b_hi = b_hi
        self.type_str = type_str
    def __repr__(self):
        return f"{self.dest} = dot128({self.type_str}) ({self.a_lo}:{self.a_hi}) . ({self.b_lo}:{self.b_hi})"

class IRVecReduce:
    """dest = $vreduce <op> (type_str) src  — reduce all vector elements.
    op is the sub-opcode token: '+' (sum) or '$max' (horizontal max).
    Only '+' and '$max' are emitted -- MIN/MUL/AND/OR/XOR/XNOR are
    simulator-broken (return 0), verified on the fixed toolchain."""
    def __init__(self, dest, src, type_str, op='+'):
        self.dest = dest; self.src = src; self.type_str = type_str; self.op = op
    def __repr__(self): return f"{self.dest} = vreduce({self.op},{self.type_str}) {self.src}"

class IRNop:
    """$nop  — no operation"""
    def __repr__(self): return "NOP"

class IRFuncAddr:
    """dest = address of a named function (for function pointers)"""
    def __init__(self, dest, func_name):
        self.dest = dest; self.func_name = func_name
    def __repr__(self): return f"{self.dest} = &func({self.func_name})"

class IRIndirectCall:
    """dest = (*func_ptr)(args)  — indirect call through a register"""
    def __init__(self, dest, func_ptr, args):
        self.dest = dest; self.func_ptr = func_ptr; self.args = args
    def __repr__(self):
        return f"{self.dest} = (*{self.func_ptr})({', '.join(str(a) for a in self.args)})"

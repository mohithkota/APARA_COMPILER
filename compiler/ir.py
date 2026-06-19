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
    """dest = left op right"""
    def __init__(self, dest, op, left, right):
        self.dest = dest; self.op = op; self.left = left; self.right = right
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
    """dest = mem[base + offset]"""
    def __init__(self, dest, base, offset, elem_bytes):
        self.dest = dest; self.base = base; self.offset = offset
        self.elem_bytes = elem_bytes
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

class IRGlobalLoad:
    """dest = DMEM[dmem_addr + offset]  (global variable access)"""
    def __init__(self, dest, dmem_addr, offset=None, *, elem_bytes):
        self.dest = dest; self.dmem_addr = dmem_addr
        self.offset = offset or Const(0); self.elem_bytes = elem_bytes
    def __repr__(self): return f"{self.dest} = DMEM[0x{self.dmem_addr:x}+{self.offset}]"

class IRGlobalStore:
    """DMEM[dmem_addr + offset] = src"""
    def __init__(self, dmem_addr, offset, src, elem_bytes):
        self.dmem_addr = dmem_addr; self.offset = offset
        self.src = src; self.elem_bytes = elem_bytes
    def __repr__(self): return f"DMEM[0x{self.dmem_addr:x}+{self.offset}] = {self.src}"

class IRGlobalAddrOf:
    """dest = address of global (dmem_addr + optional offset)"""
    def __init__(self, dest, dmem_addr, offset=None):
        self.dest = dest; self.dmem_addr = dmem_addr
        self.offset = offset or Const(0)
    def __repr__(self): return f"{self.dest} = &DMEM[0x{self.dmem_addr:x}]"

class IRCondJump:
    """if left op right goto true_label [else false_label]"""
    def __init__(self, left, op, right, true_label, false_label=None):
        self.left = left; self.op = op; self.right = right
        self.true_label = true_label; self.false_label = false_label
    def __repr__(self): return f"if {self.left} {self.op} {self.right} goto {self.true_label}"

class IRJump:
    """goto label"""
    def __init__(self, label): self.label = label
    def __repr__(self): return f"goto {self.label}"

class IRCall:
    """dest = func(args)"""
    def __init__(self, dest, func_name, args):
        self.dest = dest; self.func_name = func_name; self.args = args
    def __repr__(self):
        return f"{self.dest} = {self.func_name}({', '.join(str(a) for a in self.args)})"

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
    """dest = $vreduce (type_str) src  — sum all vector elements"""
    def __init__(self, dest, src, type_str):
        self.dest = dest; self.src = src; self.type_str = type_str
    def __repr__(self): return f"{self.dest} = vreduce({self.type_str}) {self.src}"

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

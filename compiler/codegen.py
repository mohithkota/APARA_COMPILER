"""
APARA Compiler — IR → APARA mcode  (v5: 28-register dynamic allocator)

Fixed registers (2 only):
  $r0  = ZERO   (hardware read-only, always 0)
  $r28 = GBASE  (global data base = 0x400, set once in startup, never modified)

Reserved per-function (not in allocatable pool):
  $r26 = FP  (frame pointer — live throughout each function call)
  $r27 = SP  (stack pointer — live throughout each function call)

Dynamically allocated pool (28 registers):
  $r1        — pre-coloured for return value; recycled between calls
  $r2–$r5    — pre-coloured for call arguments; recycled between call sites
  $r6–$r25   — general purpose
  $r29–$r31  — general purpose (no longer permanently reserved)

Unconditional jump: "? ($i64) $r0 == $goto label"  (0==0 always true; no ONE reg needed)
Scratch registers: borrowed on demand from the free pool, returned immediately after use.
$pack: borrow_pair() finds any two physically consecutive free registers at emit time.
"""

from ir import *

# ── ABI-fixed names (never change) ────────────────────────────────────────────
ZERO  = '$r0'
RET   = '$r1'
ARG   = ['$r2', '$r3', '$r4', '$r5']
FP    = '$r26'
SP    = '$r27'
GBASE = '$r28'

# ── Allocatable pool: everything except r0, r26, r27, r28 ─────────────────────
# = r1..r25, r29, r30, r31  →  28 registers
POOL_REGS = [f'$r{i}' for i in range(1, 32) if i not in (0, 26, 27, 28)]

# Worst-case caller-save area: all 28 pool regs × 8 bytes = 224 bytes
CALLER_SAVE_BYTES = len(POOL_REGS) * 8   # 224

# Physical scratch regs used ONLY in startup/global-init code (before any function runs)
_INIT_SCR  = '$r30'
_INIT_SCR2 = '$r31'


# ── Register allocator ─────────────────────────────────────────────────────────

class RegAlloc:
    """
    Linear-scan allocator with on-demand scratch borrowing.

    reg(temp)          — assign a permanent register to a named temporary
    free(name)         — return temp's register to the pool when it dies
    borrow()           — pop a scratch register for one instruction
    unborrow(r)        — return a scratch register immediately after use
    borrow_pair()      — find two physically consecutive free regs (for $pack)
    """

    def __init__(self):
        self._map  = {}              # temp_name  → physical register
        self._pool = list(POOL_REGS) # free register list (ordered)

    def reg(self, temp):
        name = temp.name if isinstance(temp, Temp) else str(temp)
        if name not in self._map:
            if not self._pool:
                raise RuntimeError(
                    f"Register exhaustion allocating '{name}': "
                    f"all {len(POOL_REGS)} registers live simultaneously.")
            self._map[name] = self._pool.pop(0)
        return self._map[name]

    def free(self, name):
        r = self._map.pop(name, None)
        if r is not None:
            self._pool.append(r)

    def borrow(self):
        """Temporarily borrow a free register for one or two instructions."""
        if not self._pool:
            raise RuntimeError(
                "No free register to borrow for scratch use.")
        return self._pool.pop(0)

    def unborrow(self, r):
        """Return a borrowed scratch register back to the head of the pool."""
        self._pool.insert(0, r)

    def borrow_pair(self):
        """
        Find and borrow two physically consecutive registers (required by $pack).
        Returns (r_low, r_high) where int(r_high[2:]) == int(r_low[2:]) + 1.
        """
        by_num = sorted(self._pool, key=lambda r: int(r[2:]))
        for i in range(len(by_num) - 1):
            n1 = int(by_num[i][2:])
            n2 = int(by_num[i + 1][2:])
            if n2 == n1 + 1:
                r_lo, r_hi = by_num[i], by_num[i + 1]
                self._pool.remove(r_lo)
                self._pool.remove(r_hi)
                return r_lo, r_hi
        raise RuntimeError(
            "No consecutive register pair available for $pack. "
            f"Free pool: {sorted(self._pool)}")

    def items(self):
        """Return list of (name, reg) for all currently allocated temps."""
        return list(self._map.items())


# ── Code generator ─────────────────────────────────────────────────────────────

class CodeGen:

    def __init__(self, global_base=0x400):
        self._lines            = []
        self._ra               = RegAlloc()
        self._global_base      = global_base
        self._decl_frame       = 0
        self._init_code        = []
        self._pending_labels   = []
        self._last_use         = {}
        self._func_ir_base     = 0
        self._current_epilogue = ''

    # ── public interface ───────────────────────────────────────────────────────

    def generate(self, ir_instructions, global_base=None):
        if global_base is not None:
            self._global_base = global_base
        self._lines          = []
        self._init_code      = []
        self._ra             = RegAlloc()
        self._pending_labels = []
        self._last_use       = {}
        self._func_ir_base   = 0

        for idx, ir in enumerate(ir_instructions):
            if isinstance(ir, IRFuncBegin):
                end = next(
                    (j for j in range(idx, len(ir_instructions))
                     if isinstance(ir_instructions[j], IRFuncEnd)),
                    len(ir_instructions) - 1)
                self._last_use     = self._compute_last_uses(ir_instructions[idx:end + 1])
                self._func_ir_base = idx

            local_idx = idx - self._func_ir_base
            self._gen(ir)
            self._free_dead_at(local_idx)

        if self._pending_labels:
            for lbl in self._pending_labels:
                self._lines.append(f"{lbl}:")
            self._lines.extend(["||", "    $null", ";"])

        return '\n'.join(self._lines) + '\n'

    # ── liveness helpers ───────────────────────────────────────────────────────

    def _get_src_temps(self, ir):
        cls  = type(ir).__name__
        ops  = []
        if   cls == 'IRAssign':     ops = [ir.src]
        elif cls == 'IRBinOp':      ops = [ir.left, ir.right]
        elif cls == 'IRUnaryOp':    ops = [ir.operand]
        elif cls == 'IRLoad':       ops = [ir.base, ir.offset]
        elif cls == 'IRStore':      ops = [ir.base, ir.offset, ir.src]
        elif cls == 'IRGlobalLoad': ops = [ir.offset]
        elif cls == 'IRGlobalStore':ops = [ir.offset, ir.src]
        elif cls == 'IRGlobalAddrOf':ops= [ir.offset] if hasattr(ir, 'offset') else []
        elif cls == 'IRCondJump':   ops = [ir.left, ir.right]
        elif cls == 'IRReturn':     ops = [ir.value] if ir.value else []
        elif cls == 'IRCall':       ops = list(ir.args)
        elif cls == 'IRCast':       ops = [ir.src]
        elif cls == 'IRFsqrt':      ops = [ir.src]
        elif cls == 'IRCmov':       ops = [ir.check, ir.src_true, ir.src_false]
        elif cls == 'IRSlice':      ops = [ir.src]
        elif cls == 'IRPack':       ops = [ir.src1, ir.src2]
        elif cls == 'IRVecArith':   ops = [ir.src1, ir.src2]
        elif cls == 'IRVecDot':     ops = [ir.src1, ir.src2] + (
                                        [ir.accum] if ir.accumulate and ir.accum else [])
        elif cls == 'IRVecReduce':  ops = [ir.src]
        return [o for o in ops if isinstance(o, Temp)]

    def _compute_last_uses(self, ir_slice):
        last_use = {}
        for i, ir in enumerate(ir_slice):
            for t in self._get_src_temps(ir):
                last_use[t.name] = i
        return last_use

    def _free_dead_at(self, local_idx):
        for name, last in self._last_use.items():
            if last == local_idx:
                self._ra.free(name)

    # ── startup / init ─────────────────────────────────────────────────────────

    def startup_code(self, global_base=None, stack_top=0x7FF8):
        gb = global_base if global_base is not None else self._global_base
        out = [
            "// ── APARA startup (APARA C Compiler v5: 28-reg dynamic allocator) ────────────",
            f"// Fixed: $r0=ZERO $r28=GBASE=0x{gb:x}  Reserved: $r26=FP $r27=SP",
            f"// Pool:  $r1-$r25 $r29-$r31  (28 registers, all recycled dynamically)",
            "apara_start:",
        ]

        def b(instr):
            out.extend(["||", f"    {instr}", ";"])

        self._emit_set_const_into(gb,        GBASE, b)
        self._emit_set_const_into(stack_top, SP,    b)
        b(f"+ {FP} ($i64) {ZERO} {SP}")
        # No ONE register — unconditional jump uses "? $r0 == label" (0==0)

        for line in self._init_code:
            out.append(line)

        b(f"$call main")
        b(f"$halt")
        out.append("")
        return '\n'.join(out)

    # ── emit helpers ───────────────────────────────────────────────────────────

    def _emit(self, instr, label=None):
        if label:
            self._pending_labels.append(label)
        for lbl in self._pending_labels:
            self._lines.append(f"{lbl}:")
        self._pending_labels = []
        self._lines.append("||")
        self._lines.append(f"    {instr}")
        self._lines.append(";")

    # ── constant loading ───────────────────────────────────────────────────────

    def _load_const(self, reg, value):
        value = int(value)
        if -512 <= value <= 511:
            self._emit(f"+ {reg} ($i64) {ZERO} {value}")
        elif 0 <= value <= 65535:
            self._emit(f"$set {reg} 0 {value}")
        else:
            lo = value & 0xFFFF
            hi = (value >> 16) & 0xFFFF
            self._emit(f"$set {reg} 0 {lo}")
            if hi:
                self._emit(f"$set {reg} 2 {hi}")

    def _emit_set_const_into(self, value, reg, emit_fn):
        value = int(value)
        if -512 <= value <= 511:
            emit_fn(f"+ {reg} ($i64) {ZERO} {value}")
        elif 0 <= value <= 65535:
            emit_fn(f"$set {reg} 0 {value}")
        else:
            lo = value & 0xFFFF
            hi = (value >> 16) & 0xFFFF
            emit_fn(f"$set {reg} 0 {lo}")
            if hi:
                emit_fn(f"$set {reg} 2 {hi}")

    def _operand_reg(self, op):
        """
        Return (register, borrowed) for any IR operand.
        If op is a Const, borrows a scratch register and loads the constant.
        Caller must call _ra.unborrow(r) when borrowed=True.
        """
        if isinstance(op, Const):
            r = self._ra.borrow()
            self._load_const(r, op.value)
            return r, True
        return self._ra.reg(op), False

    def _atype(self, elem_bytes):
        return '($i64)'

    # ── unconditional / conditional branches ───────────────────────────────────

    def _emit_jump(self, label):
        # 0 == 0 is always true → unconditional jump; no dedicated ONE register needed
        self._emit(f"? ($i64) {ZERO} == $goto {label}")

    def _emit_cond_branch(self, left, op, right, true_lbl, false_lbl):
        # Constant fold when both operands are known at compile time
        if isinstance(left, Const) and isinstance(right, Const):
            lv, rv = left.value, right.value
            taken = {'==': lv == rv, '!=': lv != rv, '>': lv > rv,
                     '<': lv < rv,   '>=': lv >= rv, '<=': lv <= rv}.get(op, False)
            if taken:
                self._emit_jump(true_lbl)
            elif false_lbl:
                self._emit_jump(false_lbl)
            return

        l_reg, l_bor = self._operand_reg(left)

        r_is_zero = isinstance(right, Const) and right.value == 0
        if r_is_zero:
            if op in ('<', '<='):
                scr = self._ra.borrow()
                self._emit(f"- {scr} ($i64) {ZERO} {l_reg}")
                apara_op = '>' if op == '<' else '>='
                self._emit(f"? ($i64) {scr} {apara_op} $goto {true_lbl}")
                self._ra.unborrow(scr)
            else:
                self._emit(f"? ($i64) {l_reg} {op} $goto {true_lbl}")
        else:
            r_reg, r_bor = self._operand_reg(right)
            scr = self._ra.borrow()
            if op in ('>', '>=', '==', '!='):
                self._emit(f"- {scr} ($i64) {l_reg} {r_reg}")
                self._emit(f"? ($i64) {scr} {op} $goto {true_lbl}")
            else:   # '<', '<='
                self._emit(f"- {scr} ($i64) {r_reg} {l_reg}")
                apara_op = '>' if op == '<' else '>='
                self._emit(f"? ($i64) {scr} {apara_op} $goto {true_lbl}")
            self._ra.unborrow(scr)
            if r_bor: self._ra.unborrow(r_reg)

        if l_bor: self._ra.unborrow(l_reg)

        if false_lbl:
            self._emit_jump(false_lbl)

    # ── IR dispatch ────────────────────────────────────────────────────────────

    def _gen(self, ir):
        method = getattr(self, f'_gen_{type(ir).__name__}', None)
        if method:
            method(ir)

    # ── global variable initialiser ────────────────────────────────────────────

    def _gen_IRGlobalDecl(self, ir):
        if not ir.init:
            return
        at     = self._atype(ir.elem_bytes)
        offset = ir.dmem_addr - self._global_base
        stride = getattr(ir, 'stride', ir.elem_bytes)

        for i, val in enumerate(ir.init):
            byte_off = offset + i * stride
            lines = []
            val = int(val)

            if -512 <= val <= 511:
                lines += ["||", f"    + {_INIT_SCR} ($i64) {ZERO} {val}", ";"]
            elif 0 <= val <= 65535:
                lines += ["||", f"    $set {_INIT_SCR} 0 {val}", ";"]
            else:
                lo = val & 0xFFFF; hi = (val >> 16) & 0xFFFF
                lines += ["||", f"    $set {_INIT_SCR} 0 {lo}", ";"]
                if hi:
                    lines += ["||", f"    $set {_INIT_SCR} 2 {hi}", ";"]

            if 0 <= byte_off <= 511:
                lines += ["||", f"    $st {at} [{GBASE} + {byte_off}] {_INIT_SCR}", ";"]
            else:
                lo2 = byte_off & 0xFFFF; hi2 = (byte_off >> 16) & 0xFFFF
                lines += ["||", f"    $set {_INIT_SCR2} 0 {lo2}", ";"]
                if hi2:
                    lines += ["||", f"    $set {_INIT_SCR2} 2 {hi2}", ";"]
                lines += ["||", f"    $st {at} [{GBASE} + {_INIT_SCR2}] {_INIT_SCR}", ";"]

            self._init_code.extend(lines)

    # ── function prologue / epilogue ───────────────────────────────────────────

    def _gen_IRFuncBegin(self, ir):
        self._ra           = RegAlloc()   # fresh 28-reg pool for this function
        self._decl_frame   = ir.frame_size
        self._current_epilogue = f"{ir.name}_epilogue"

        self._pending_labels.append(ir.name)

        self._emit(f"$st ($i64) [{SP} + 0] {FP}")
        self._emit(f"+ {FP} ($i64) {ZERO} {SP}")

        fs = ir.frame_size + CALLER_SAVE_BYTES
        if fs <= 511:
            self._emit(f"- {SP} ($i64) {SP} {fs}")
        else:
            scr = self._ra.borrow()
            self._load_const(scr, fs)
            self._emit(f"- {SP} ($i64) {SP} {scr}")
            self._ra.unborrow(scr)

        # No ONE register — unconditional branch uses $r0 == $r0

        for i, (pname, fp_off) in enumerate(ir.params):
            if i >= len(ARG):
                break
            self._emit(f"$st ($i64) [{FP} + {fp_off}] {ARG[i]}")

    def _gen_IRFuncEnd(self, ir):
        self._pending_labels.append(self._current_epilogue)
        self._emit(f"+ {SP} ($i64) {ZERO} {FP}")
        self._emit(f"$ld ($i64) {FP} [{SP} + 0]")
        self._emit("$return")

    # ── labels / jumps ─────────────────────────────────────────────────────────

    def _gen_IRLabel(self, ir):
        self._pending_labels.append(ir.name)

    def _gen_IRJump(self, ir):
        self._emit_jump(ir.label)

    def _gen_IRCondJump(self, ir):
        self._emit_cond_branch(ir.left, ir.op, ir.right, ir.true_label, ir.false_label)

    # ── data movement ──────────────────────────────────────────────────────────

    def _gen_IRAssign(self, ir):
        dest = self._ra.reg(ir.dest)
        if isinstance(ir.src, Const):
            self._load_const(dest, ir.src.value)
        else:
            src = self._ra.reg(ir.src)
            self._emit(f"+ {dest} ($i64) {ZERO} {src}")

    def _gen_IRLoadAddr(self, ir):
        dest   = self._ra.reg(ir.dest)
        fp_off = ir.fp_offset
        if -512 <= fp_off <= 511:
            self._emit(f"+ {dest} ($i64) {FP} {fp_off}")
        else:
            scr = self._ra.borrow()
            self._load_const(scr, fp_off)
            self._emit(f"+ {dest} ($i64) {FP} {scr}")
            self._ra.unborrow(scr)

    def _gen_IRLoad(self, ir):
        dest     = self._ra.reg(ir.dest)
        at       = self._atype(ir.elem_bytes)
        base, b_bor = self._operand_reg(ir.base)

        if isinstance(ir.offset, Const):
            off = ir.offset.value
            if -512 <= off <= 511:
                self._emit(f"$ld {at} {dest} [{base} + {off}]")
            else:
                scr = self._ra.borrow()
                self._load_const(scr, off)
                self._emit(f"$ld {at} {dest} [{base} + {scr}]")
                self._ra.unborrow(scr)
        else:
            off_reg = self._ra.reg(ir.offset)
            self._emit(f"$ld {at} {dest} [{base} + {off_reg}]")

        if b_bor: self._ra.unborrow(base)

    def _gen_IRStore(self, ir):
        at      = self._atype(ir.elem_bytes)
        src, s_bor  = self._operand_reg(ir.src)
        base, b_bor = self._operand_reg(ir.base)

        if isinstance(ir.offset, Const):
            off = ir.offset.value
            if -512 <= off <= 511:
                self._emit(f"$st {at} [{base} + {off}] {src}")
            else:
                scr = self._ra.borrow()
                self._load_const(scr, off)
                self._emit(f"$st {at} [{base} + {scr}] {src}")
                self._ra.unborrow(scr)
        else:
            off_reg = self._ra.reg(ir.offset)
            self._emit(f"$st {at} [{base} + {off_reg}] {src}")

        if b_bor: self._ra.unborrow(base)
        if s_bor: self._ra.unborrow(src)

    # ── global variable access ─────────────────────────────────────────────────

    def _gen_IRGlobalLoad(self, ir):
        dest = self._ra.reg(ir.dest)
        at   = self._atype(ir.elem_bytes)
        goff = ir.dmem_addr - self._global_base

        if isinstance(ir.offset, Const) and ir.offset.value == 0:
            if 0 <= goff <= 511:
                self._emit(f"$ld {at} {dest} [{GBASE} + {goff}]")
            else:
                scr = self._ra.borrow()
                self._load_const(scr, goff)
                self._emit(f"$ld {at} {dest} [{GBASE} + {scr}]")
                self._ra.unborrow(scr)
        else:
            off_reg, o_bor = self._operand_reg(ir.offset)
            addr = self._ra.borrow()
            if 0 <= goff <= 511:
                self._emit(f"+ {addr} ($i64) {off_reg} {goff}")
            else:
                self._load_const(addr, goff)
                self._emit(f"+ {addr} ($i64) {addr} {off_reg}")
            self._emit(f"+ {addr} ($i64) {GBASE} {addr}")
            self._emit(f"$ld {at} {dest} [{addr} + 0]")
            self._ra.unborrow(addr)
            if o_bor: self._ra.unborrow(off_reg)

    def _gen_IRGlobalStore(self, ir):
        at   = self._atype(ir.elem_bytes)
        goff = ir.dmem_addr - self._global_base
        src, s_bor = self._operand_reg(ir.src)

        if isinstance(ir.offset, Const) and ir.offset.value == 0:
            if 0 <= goff <= 511:
                self._emit(f"$st {at} [{GBASE} + {goff}] {src}")
            else:
                scr = self._ra.borrow()
                self._load_const(scr, goff)
                self._emit(f"$st {at} [{GBASE} + {scr}] {src}")
                self._ra.unborrow(scr)
        else:
            off_reg, o_bor = self._operand_reg(ir.offset)
            addr = self._ra.borrow()
            if 0 <= goff <= 511:
                self._emit(f"+ {addr} ($i64) {off_reg} {goff}")
            else:
                self._load_const(addr, goff)
                self._emit(f"+ {addr} ($i64) {addr} {off_reg}")
            self._emit(f"+ {addr} ($i64) {GBASE} {addr}")
            self._emit(f"$st {at} [{addr} + 0] {src}")
            self._ra.unborrow(addr)
            if o_bor: self._ra.unborrow(off_reg)

        if s_bor: self._ra.unborrow(src)

    def _gen_IRGlobalAddrOf(self, ir):
        dest = self._ra.reg(ir.dest)
        goff = ir.dmem_addr - self._global_base

        if isinstance(ir.offset, Const) and ir.offset.value == 0:
            if 0 <= goff <= 511:
                self._emit(f"+ {dest} ($i64) {GBASE} {goff}")
            else:
                scr = self._ra.borrow()
                self._load_const(scr, goff)
                self._emit(f"+ {dest} ($i64) {GBASE} {scr}")
                self._ra.unborrow(scr)
        else:
            off_reg, o_bor = self._operand_reg(ir.offset)
            scr = self._ra.borrow()
            self._load_const(scr, goff)
            self._emit(f"+ {scr} ($i64) {scr} {off_reg}")
            self._emit(f"+ {dest} ($i64) {GBASE} {scr}")
            self._ra.unborrow(scr)
            if o_bor: self._ra.unborrow(off_reg)

    # ── arithmetic ─────────────────────────────────────────────────────────────

    _APARA_OP = {
        '+':  '+',  '-':  '-',  '*':  '*',  '/':  '/',
        '<<': '<<', '>>': '>>',
        '|':  '|',  '&':  '&',  '^':  '^',
        '~|': '~|', '~&': '~&', '~^': '~~',
    }

    def _gen_IRBinOp(self, ir):
        dest = self._ra.reg(ir.dest)
        op   = ir.op

        # Constant-fold both-const case
        if isinstance(ir.left, Const) and isinstance(ir.right, Const):
            lv, rv = ir.left.value, ir.right.value
            try:
                result = {
                    '+': lv + rv, '-': lv - rv, '*': lv * rv,
                    '/': int(lv / rv) if rv != 0 else 0,
                    '%': lv % rv if rv != 0 else 0,
                    '&': lv & rv, '|': lv | rv, '^': lv ^ rv,
                    '<<': lv << rv, '>>': lv >> rv,
                }.get(op)
                if result is not None:
                    self._load_const(dest, result)
                    return
            except Exception:
                pass

        # Modulo: synthesise as  a - (a/b)*b
        if op == '%':
            l_reg, l_bor = self._operand_reg(ir.left)
            r_reg, r_bor = self._operand_reg(ir.right)
            tmp = self._ra.borrow()
            self._emit(f"/ {tmp} ($i64) {l_reg} {r_reg}")
            self._emit(f"* {tmp} ($i64) {tmp} {r_reg}")
            self._emit(f"- {dest} ($i64) {l_reg} {tmp}")
            self._ra.unborrow(tmp)
            if r_bor: self._ra.unborrow(r_reg)
            if l_bor: self._ra.unborrow(l_reg)
            return

        apara = self._APARA_OP.get(op, op)
        l_reg, l_bor = self._operand_reg(ir.left)

        if isinstance(ir.right, Const):
            rv = ir.right.value
            if -512 <= rv <= 511:
                self._emit(f"{apara} {dest} ($i64) {l_reg} {rv}")
            else:
                scr = self._ra.borrow()
                self._load_const(scr, rv)
                self._emit(f"{apara} {dest} ($i64) {l_reg} {scr}")
                self._ra.unborrow(scr)
        else:
            r_reg = self._ra.reg(ir.right)
            self._emit(f"{apara} {dest} ($i64) {l_reg} {r_reg}")

        if l_bor: self._ra.unborrow(l_reg)

    def _gen_IRUnaryOp(self, ir):
        dest     = self._ra.reg(ir.dest)
        src, bor = self._operand_reg(ir.operand)
        op       = ir.op
        if op == '-':
            self._emit(f"- {dest} ($i64) {ZERO} {src}")
        elif op == '~':
            self._emit(f"^ {dest} ($i64) {src} -1")
        else:
            self._emit(f"+ {dest} ($i64) {ZERO} {src}")
        if bor: self._ra.unborrow(src)

    # ── function calls ─────────────────────────────────────────────────────────

    def _gen_IRCall(self, ir):
        fname = ir.func_name
        if fname == 'halt':
            self._emit("$halt")
            return

        # 1. Save all currently live allocated temps to the caller-save area.
        #    Record which stack slot each register was saved to (needed for arg setup).
        saved = list(self._ra.items())
        saved_slot = {}          # reg  → frame slot (signed offset from FP)
        saved_name_slot = {}     # name → frame slot
        for idx, (name, reg) in enumerate(saved):
            slot = -(self._decl_frame + 8 + idx * 8)
            self._emit(f"$st ($i64) [{FP} + {slot}] {reg}")
            saved_slot[reg]  = slot
            saved_name_slot[name] = slot

        # 2. Set up arguments in r2–r5.
        #    Read from the saved stack slots to avoid register-aliasing bugs
        #    (e.g. arg0 source is r2, which we'd clobber when writing arg1 to r3).
        for i, arg in enumerate(ir.args[:4]):
            if isinstance(arg, Const):
                self._load_const(ARG[i], arg.value)
            elif isinstance(arg, Temp):
                name = arg.name
                if name in saved_name_slot:
                    # Safe path: read from stack (avoids r2-r5 aliasing)
                    self._emit(f"$ld ($i64) {ARG[i]} [{FP} + {saved_name_slot[name]}]")
                else:
                    # Arg temp not live yet (edge case); read from register
                    arg_reg = self._ra.reg(arg)
                    self._emit(f"+ {ARG[i]} ($i64) {ZERO} {arg_reg}")

        # 3. Make the call
        self._emit(f"$call {fname}")

        # 4. Capture return value into dest BEFORE restoring saved regs.
        #    Restoring may overwrite $r1 if it was allocated to a live temp before the call.
        if ir.dest is not None:
            dest = self._ra.reg(ir.dest)
            self._emit(f"+ {dest} ($i64) {ZERO} {RET}")

        # 5. Restore all saved registers
        for idx, (_, reg) in enumerate(saved):
            slot = -(self._decl_frame + 8 + idx * 8)
            self._emit(f"$ld ($i64) {reg} [{FP} + {slot}]")

    # ── return ─────────────────────────────────────────────────────────────────

    def _gen_IRReturn(self, ir):
        if ir.value is not None:
            if isinstance(ir.value, Const):
                self._load_const(RET, ir.value.value)
            else:
                src = self._ra.reg(ir.value)
                self._emit(f"+ {RET} ($i64) {ZERO} {src}")
        self._emit_jump(self._current_epilogue)

    # ── APARA-specific instructions ────────────────────────────────────────────

    def _gen_IRNop(self, ir):
        self._emit("$nop")

    def _gen_IRCast(self, ir):
        dest     = self._ra.reg(ir.dest)
        src, bor = self._operand_reg(ir.src)
        self._emit(f"$cast ({ir.dest_type}) {dest} ({ir.src_type}) {src}")
        if bor: self._ra.unborrow(src)

    def _gen_IRFsqrt(self, ir):
        dest     = self._ra.reg(ir.dest)
        src, bor = self._operand_reg(ir.src)
        self._emit(f"$fsqrt {dest} ({ir.type_str}) {src}")
        if bor: self._ra.unborrow(src)

    def _gen_IRCmov(self, ir):
        """
        $cmov: dest = src_false initially; if check cond 0 then dest = src_true.
        """
        dest  = self._ra.reg(ir.dest)
        chk, c_bor  = self._operand_reg(ir.check)
        fls, f_bor  = self._operand_reg(ir.src_false)
        tru, t_bor  = self._operand_reg(ir.src_true)

        self._emit(f"+ {dest} ($i64) {ZERO} {fls}")
        self._emit(f"$cmov ({ir.type_str}) {chk} {ir.cond} {dest} {tru}")

        if t_bor: self._ra.unborrow(tru)
        if f_bor: self._ra.unborrow(fls)
        if c_bor: self._ra.unborrow(chk)

    def _gen_IRSlice(self, ir):
        dest     = self._ra.reg(ir.dest)
        src, bor = self._operand_reg(ir.src)
        self._emit(f"$slice {dest} {ir.hindex} {ir.lindex} {src}")
        if bor: self._ra.unborrow(src)

    def _gen_IRPack(self, ir):
        """
        $pack rd result_nbits src_nbits rs2
        ISA requires rs2 and rs2+1 to be a consecutive register pair.
        borrow_pair() finds any two consecutive free registers at emit time.
        """
        dest     = self._ra.reg(ir.dest)
        a, a_bor = self._operand_reg(ir.src1)
        b, b_bor = self._operand_reg(ir.src2)

        p1, p2 = self._ra.borrow_pair()
        self._emit(f"+ {p1} ($i64) {ZERO} {a}")
        self._emit(f"+ {p2} ($i64) {ZERO} {b}")
        self._emit(f"$pack {dest} {ir.result_nbits} {ir.src_nbits} {p1}")
        self._ra.unborrow(p2)
        self._ra.unborrow(p1)

        if b_bor: self._ra.unborrow(b)
        if a_bor: self._ra.unborrow(a)

    def _gen_IRVecArith(self, ir):
        dest     = self._ra.reg(ir.dest)
        rs1, b1  = self._operand_reg(ir.src1)
        rs2, b2  = self._operand_reg(ir.src2)
        rep      = " $replicate" if ir.replicate else ""
        self._emit(f"$v {ir.op} {dest} ({ir.type_str}) {rs1} {rs2}{rep}")
        if b2: self._ra.unborrow(rs2)
        if b1: self._ra.unborrow(rs1)

    def _gen_IRVecDot(self, ir):
        dest    = self._ra.reg(ir.dest)
        rs1, b1 = self._operand_reg(ir.src1)
        rs2, b2 = self._operand_reg(ir.src2)
        if ir.accumulate and ir.accum is not None:
            acc, ba = self._operand_reg(ir.accum)
            self._emit(f"+ {dest} ($i64) {ZERO} {acc}")
            self._emit(f"$dot $accumulate {dest} ({ir.type_str}) {rs1} {rs2}")
            if ba: self._ra.unborrow(acc)
        else:
            self._emit(f"$dot {dest} ({ir.type_str}) {rs1} {rs2}")
        if b2: self._ra.unborrow(rs2)
        if b1: self._ra.unborrow(rs1)

    def _gen_IRVecReduce(self, ir):
        dest    = self._ra.reg(ir.dest)
        src, bor = self._operand_reg(ir.src)
        self._emit(f"$vreduce {dest} ({ir.type_str}) {src}")
        if bor: self._ra.unborrow(src)

    def _gen_IRHalt(self, ir):
        self._emit("$halt")

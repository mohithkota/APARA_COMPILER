"""
_r9_2_test.py -- unit tests for R9.2 branch-immediate folding.

R9.2 folds a compile-time-constant RIGHT operand of a conditional branch into
the subtract's IMMEDIATE field, replacing

    + rC  ($i64) $r0   K          <- loop-INVARIANT, recomputed every iteration
    - scr ($i64) l_reg rC
    ? ($i64) scr OP $goto L

with

    - scr ($i64) l_reg K
    ? ($i64) scr OP $goto L

Three instructions become two. This suite pins:

  * the fold window is exactly [-512, 511] -- both boundaries fold, and -513 /
    512 do not. That window is not arbitrary: it is the SAME window
    `_load_const` uses to decide a constant fits an ALU immediate field
    (codegen.py:498), so the two cannot drift apart;
  * K == 0 keeps the pre-existing ONE-instruction path (it must not regress
    into the two-instruction fold);
  * the folded form is emitted for every comparison operator, including the
    `<` / `<=` forms the compiler never emitted before R9.2;
  * against the real pre-R9.2 baseline (`wip_r9_2/codegen_PRE_r9_2_baseline.py`)
    the fold removes exactly one instruction per branch and never adds one;
  * the fold is SEMANTICS-PRESERVING: for every operator, a spread of constants
    (in and out of the window) and a spread of left values, the emitted code is
    interpreted instruction-by-instruction and must branch exactly when C does.

Run:  python3 compiler/_r9_2_test.py
"""

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from ir import (Temp, Const, IRFuncBegin, IRFuncEnd, IRLabel, IRCondJump,   # noqa: E402
                IRAssign, IRReturn)
from codegen import CodeGen                                                 # noqa: E402

FOLD_LO, FOLD_HI = -512, 511
OPS = ['<', '<=', '>', '>=', '==', '!=']

_fail = []
_pass = 0


def check(cond, what):
    global _pass
    if cond:
        _pass += 1
    else:
        _fail.append(what)
        print(f"  FAIL: {what}")


# ── the pre-R9.2 codegen, loaded side by side for a true A/B ────────────────
def _load_baseline():
    path = os.path.join(_HERE, 'wip_r9_2', 'codegen_PRE_r9_2_baseline.py')
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location('_codegen_pre_r9_2', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['_codegen_pre_r9_2'] = mod
    spec.loader.exec_module(mod)
    return mod.CodeGen


BaselineCodeGen = _load_baseline()


def build_ir(op, k, l_value):
    t = Temp('t_l')
    return [
        IRFuncBegin('main', [], {}, 16),
        IRAssign(t, Const(l_value)),
        IRCondJump(t, op, Const(k), 'L_true', 'L_false'),
        IRLabel('L_true'),
        IRReturn(Const(1)),
        IRLabel('L_false'),
        IRReturn(Const(0)),
        IRFuncEnd('main'),
    ]


def _lines(cg_cls, op, k, l_value):
    text = cg_cls().generate(build_ir(op, k, l_value))
    if isinstance(text, (list, tuple)):
        text = '\n'.join(text)
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def emit_branch(op, k, l_value=7):
    return _lines(CodeGen, op, k, l_value)


def emit_baseline(op, k, l_value=7):
    return _lines(BaselineCodeGen, op, k, l_value)


# ── locating the compare/branch pair ────────────────────────────────────────
def branch_index(lines):
    for i, ln in enumerate(lines):
        if ln.startswith('?') and '$goto L_true' in ln:
            return i
    return None


def branch_line(lines):
    i = branch_index(lines)
    return lines[i] if i is not None else None


def sub_feeding_branch(lines):
    """The instruction that defines the register the branch tests."""
    bi = branch_index(lines)
    if bi is None:
        return None
    reg = lines[bi].split()[2]                 # ? ($i64) REG op $goto L
    define = None
    for ln in lines[:bi]:
        p = ln.split()
        if p and p[0] in ('-', '+', '$set') and len(p) > 1 and p[1] == reg:
            define = ln
    return define


def is_folded(lines, k):
    """True iff K appears as a literal operand of the subtract feeding the
    branch (i.e. it went into the immediate field, not into a register)."""
    define = sub_feeding_branch(lines)
    if define is None or not define.startswith('-'):
        return False
    return define.split()[-1] == str(k)


# ── a concrete interpreter for the shapes _emit_cond_branch can produce ─────
def simulate(lines):
    """Execute the emitted instructions far enough to decide the branch.
    Returns True if the branch to L_true is taken. Every register value is
    concrete -- the left operand is materialised from a constant."""
    regs = {'$r0': 0, '$r27': 0x7FF8, '$r26': 0}   # $r0 zero, SP, FP

    def val(tok):
        if tok in regs:
            return regs[tok]
        if tok.lstrip('-').isdigit():
            return int(tok)
        raise AssertionError(f"unknown operand {tok!r}")

    for ln in lines:
        p = ln.split()
        if p[0] == '$set' and len(p) == 4:                  # $set rD field imm
            field, imm = int(p[2]), int(p[3])
            regs[p[1]] = imm << (8 * field)
        elif p[0] in ('-', '+', '<<', '|') and len(p) == 5:  # OP rD (t) A B
            a, b = val(p[3]), val(p[4])
            # evaluate lazily -- `a << b` is illegal for the many b < 0 cases
            if p[0] == '-':
                regs[p[1]] = a - b
            elif p[0] == '+':
                regs[p[1]] = a + b
            elif p[0] == '<<':
                regs[p[1]] = a << b
            else:
                regs[p[1]] = a | b
        elif p[0] == '?' and '$goto L_true' in ln:
            reg, op = p[2], p[3]
            v = val(reg)
            return {'<': v < 0, '<=': v <= 0, '>': v > 0,
                    '>=': v >= 0, '==': v == 0, '!=': v != 0}[op]
        elif p[0] == '?':
            continue                                        # $goto L_false etc.
    return None


def c_taken(l, op, k):
    return {'<': l < k, '<=': l <= k, '>': l > k,
            '>=': l >= k, '==': l == k, '!=': l != k}[op]


print("=" * 72)
print("R9.2 branch-immediate folding -- unit tests")
print("=" * 72)

# ── 1. immediate window boundaries ──────────────────────────────────────────
print("\n[1] fold window is exactly [-512, 511]")
for k in (FOLD_LO, FOLD_LO + 1, -1, 1, 16, FOLD_HI - 1, FOLD_HI):
    check(is_folded(emit_branch('<', k), k),
          f"K={k} must FOLD into the subtract immediate")
for k in (FOLD_LO - 1, FOLD_HI + 1, -1000, 4096, 100000):
    check(not is_folded(emit_branch('<', k), k),
          f"K={k} is out of range and must NOT fold")
print(f"    {FOLD_LO}..{FOLD_HI} fold; {FOLD_LO - 1}, {FOLD_HI + 1} and wider do not")

# the window must agree with _load_const's own immediate test, or the two drift
import inspect                                                              # noqa: E402
_src = inspect.getsource(CodeGen._load_const)
check(f"{FOLD_LO} <= value <= {FOLD_HI}" in _src,
      "_load_const must use the same [-512, 511] immediate window")

# ── 2. K == 0 keeps the shorter pre-existing path ───────────────────────────
print("\n[2] K == 0 keeps the pre-R9.2 one-instruction path")
for op in ('>', '>=', '==', '!='):
    lines = emit_branch(op, 0)
    define = sub_feeding_branch(lines)
    check(define is None or not define.startswith('-'),
          f"K=0 op {op} must branch on the value directly, with no subtract")
check(not is_folded(emit_branch('<', 0), 0), "K=0 must not take the R9.2 fold")

# ── 3. every operator folds; < and <= are emitted natively ──────────────────
print("\n[3] all six operators fold; < and <= are emitted, not flipped")
for op in OPS:
    lines = emit_branch(op, 16)
    check(is_folded(lines, 16), f"op {op} with K=16 must fold")
    br = branch_line(lines)
    check(br is not None and br.split()[3] == op,
          f"op {op} must be emitted as `{op}`, not flipped (got {br})")
_native = [op for op in ('<', '<=')
           if branch_line(emit_branch(op, 16)).split()[3] == op]
check(_native == ['<', '<='], "both `<` and `<=` must be emitted natively")

# ── 4. A/B against the real pre-R9.2 baseline: exactly one instruction saved ─
print("\n[4] A/B vs wip_r9_2/codegen_PRE_r9_2_baseline.py")
if BaselineCodeGen is None:
    print("    SKIP -- baseline not present")
else:
    def real_instrs(lines):
        return [ln for ln in lines
                if not ln.endswith(':') and ln not in ('||', ';')]
    worse = 0
    for op in OPS:
        for k in (-512, -33, 1, 17, 511):
            n_new = len(real_instrs(emit_branch(op, k)))
            n_old = len(real_instrs(emit_baseline(op, k)))
            check(n_new == n_old - 1,
                  f"op {op} K={k}: expected exactly 1 instruction saved "
                  f"(baseline {n_old}, R9.2 {n_new})")
            if n_new > n_old:
                worse += 1
        for k in (-513, 512, 4096):        # out of window: must be identical
            check(emit_branch(op, k) == emit_baseline(op, k),
                  f"op {op} K={k}: out-of-window code must be UNCHANGED")
    check(worse == 0, "the fold must never add an instruction")
    print("    in-window: -1 instruction for all 6 operators; "
          "out-of-window: byte-identical")

# ── 5. semantics preserved, folded AND unfolded ─────────────────────────────
print("\n[5] semantics: emitted branch matches C, per operator and per value")
LEFTS = [-600, -513, -512, -7, -1, 0, 1, 7, 16, 510, 511, 512, 513, 5000]
KS = [-512, -511, -33, -1, 0, 1, 7, 16, 510, 511, -513, 512, 4096]
bad = 0
n = 0
for op in OPS:
    for k in KS:
        for l in LEFTS:
            n += 1
            got = simulate(emit_branch(op, k, l))
            want = c_taken(l, op, k)
            if got != want:
                bad += 1
                if bad <= 5:
                    print(f"  FAIL: {l} {op} {k}: emitted={got} C={want}")
check(bad == 0, f"{bad} semantic mismatches across {n} cases")
print(f"    {n} (left, op, K) combinations executed and checked against C")

# ── 6. no constant materialisation survives for in-window K ─────────────────
print("\n[6] no `+ rX $r0 K` materialisation remains for in-window K")
for op in OPS:
    for k in (-512, -33, 17, 511):
        lines = emit_branch(op, k, l_value=1000)   # left needs $set, not `+ K`
        bi = branch_index(lines)
        mat = [ln for ln in lines[:bi]
               if ln.startswith('+') and '$r0' in ln and ln.split()[-1] == str(k)]
        check(not mat, f"op {op} K={k}: constant must not be materialised: {mat}")

print("\n" + "=" * 72)
if _fail:
    print(f"R9.2: {_pass} checks passed, {len(_fail)} FAILED")
    for f in _fail[:20]:
        print(f"  - {f}")
    sys.exit(1)
print(f"R9.2: ALL {_pass} CHECKS PASS")
print("=" * 72)

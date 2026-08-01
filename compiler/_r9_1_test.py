"""_r9_1_test.py -- unit tests for R9.1 Address Value Numbering.

Five groups, mirroring the design review:
  1. KEY        -- `IRLoadAddr` is value-numbered iff its offset is outside the
     foldable-immediate range, and the key carries nothing but the offset;
  2. TRIPWIRE   -- `IRLoadAddr`'s shape is pinned, so a future base operand or
     frame id fails HERE and names `gvn._expr_key` as the thing to revisit;
  3. CORRECTNESS-- equal offsets collapse, different offsets do not, and GVN
     preserves single-def by replacing the DEF rather than the uses;
  4. MEM2REG    -- the pipeline reorder holds: promotions must not fall
     (guards the interaction found in design review section 11d);
  5. EFFECT     -- the redundancy really is removed, and kernels with nothing to
     collapse are byte-identical with the switch on and off.
"""
import os
import sys
import copy
import inspect

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'verification'))

import gvn                                          # noqa: E402
from ir import IRLoadAddr, IRAssign, Temp, IRBinOp, Const   # noqa: E402
from ir_utils import func_slices                    # noqa: E402
from vector_backend import ilp_analysis as ia       # noqa: E402
import suite                                        # noqa: E402

GB = ia.GB
_fails = []


def check(n, c):
    print(f"  [{'ok' if c else 'FAIL'}] {n}")
    if not c:
        _fails.append(n)


def build(src, no_avn=False):
    """Shipped IR for one kernel, with Address Value Numbering on or off."""
    if no_avn:
        os.environ['APARA_NO_AVN'] = '1'
    else:
        os.environ.pop('APARA_NO_AVN', None)
    try:
        import importlib
        importlib.reload(gvn)                # the kill switch is read at import
        vec, _st, _r = ia.vectorize_all_module(copy.deepcopy(ia.build_ir(src)))
        sel, mtext, _t = ia.production_codegen(copy.deepcopy(vec))
        return sel, mtext
    finally:
        os.environ.pop('APARA_NO_AVN', None)
        import importlib
        importlib.reload(gvn)


def n_loadaddr(ir):
    return sum(1 for i in ir if type(i).__name__ == 'IRLoadAddr')


# ── 1. the key ────────────────────────────────────────────────────────────────

def test_key():
    print("IRLoadAddr is numbered iff its offset is beyond the foldable range")
    k = gvn._expr_key
    check("a large negative offset is numbered",
          k(IRLoadAddr(Temp('t'), -1544), None) == ('addr', -1544))
    check("a large positive offset is numbered",
          k(IRLoadAddr(Temp('t'), 4096), None) == ('addr', 4096))
    check("a small offset is NOT numbered (one instruction to rebuild)",
          k(IRLoadAddr(Temp('t'), -8), None) is None)

    # the bound must be codegen's own, not a second copy of the number
    lo, hi = gvn._FOLDABLE_LO, gvn._FOLDABLE_HI
    check(f"boundary {lo} is foldable, so not numbered",
          k(IRLoadAddr(Temp('t'), lo), None) is None)
    check(f"boundary {hi} is foldable, so not numbered",
          k(IRLoadAddr(Temp('t'), hi), None) is None)
    check("just outside the low bound IS numbered",
          k(IRLoadAddr(Temp('t'), lo - 1), None) == ('addr', lo - 1))
    check("just outside the high bound IS numbered",
          k(IRLoadAddr(Temp('t'), hi + 1), None) == ('addr', hi + 1))

    src = inspect.getsource(__import__('codegen')._gen_IRLoadAddr) \
        if hasattr(__import__('codegen'), '_gen_IRLoadAddr') else ''
    check("the bound matches rematerialization's, one constant for both passes",
          (lo, hi) == (__import__('rematerialization').FP_IMM_LO,
                       __import__('rematerialization').FP_IMM_HI))

    key = k(IRLoadAddr(Temp('t'), -1544), None)
    check("the key carries ONLY the offset -- no function or frame id",
          key == ('addr', -1544) and len(key) == 2)


def test_kill_switch():
    print("the kill switch disables address numbering without disabling GVN")
    os.environ['APARA_NO_AVN'] = '1'
    try:
        import importlib
        importlib.reload(gvn)
        check("IRLoadAddr is not numbered",
              gvn._expr_key(IRLoadAddr(Temp('t'), -1544), None) is None)
        check("ordinary expressions are STILL numbered",
              gvn._expr_key(IRBinOp(Temp('d'), '+', Temp('a'), Temp('b')),
                            _DU()) is not None)
    finally:
        os.environ.pop('APARA_NO_AVN', None)
        import importlib
        importlib.reload(gvn)


class _DU:
    """Minimal DefUse stand-in: every operand is a stable single def."""
    def is_single_def(self, n):
        return True

    def is_multi_def(self, n):
        return False


# ── 2. tripwire ───────────────────────────────────────────────────────────────

def test_tripwire_irloadaddr_shape():
    """R9.1 relies on `IRLoadAddr` having NO base operand: that is what makes
    `fp_offset` alone a canonical key. If someone adds a base, a frame id, or a
    second base register, this test fails and `gvn._expr_key` must be revisited
    (design review section 10)."""
    print("tripwire: IRLoadAddr's shape is pinned")
    params = list(inspect.signature(IRLoadAddr.__init__).parameters)
    check(f"IRLoadAddr.__init__ takes exactly (self, dest, fp_offset) -- got {params}",
          params == ['self', 'dest', 'fp_offset'])
    a = IRLoadAddr(Temp('t'), -8)
    check("an instance carries exactly {dest, fp_offset}",
          set(vars(a)) == {'dest', 'fp_offset'})


# ── 3. correctness of the transformation ──────────────────────────────────────

def _run_gvn(instrs):
    return gvn.global_value_numbering(instrs)


def test_collapse_semantics():
    print("equal offsets collapse; different offsets do not; defs stay single")
    from ir import IRFuncBegin, IRFuncEnd, IRLoad, IRStore, IRReturn
    OFF = gvn._FOLDABLE_LO - 100          # safely outside the foldable range
    def prog(off2):
        return [IRFuncBegin('f', [], {}, 4096),
                IRLoadAddr(Temp('a1'), OFF),
                IRLoad(Temp('v1'), Temp('a1'), Const(0), 8),
                IRLoadAddr(Temp('a2'), off2),
                IRLoad(Temp('v2'), Temp('a2'), Const(0), 8),
                IRReturn(Temp('v2')),
                IRFuncEnd('f')]

    out = _run_gvn(prog(OFF))
    a2 = next(i for i in out if getattr(getattr(i, 'dest', None), 'name', '') == 'a2')
    check("equal offsets: the second def becomes a copy of the leader",
          type(a2).__name__ == 'IRAssign' and a2.src.name == 'a1')
    check("the leader itself is untouched",
          any(type(i).__name__ == 'IRLoadAddr' and i.dest.name == 'a1' for i in out))
    check("uses are NOT rewritten by GVN (single-def preserved)",
          any(type(i).__name__ == 'IRLoad' and i.base.name == 'a2' for i in out))
    check("every temp still has exactly one definition",
          len([i for i in out if getattr(getattr(i, 'dest', None), 'name', '') == 'a2']) == 1)

    out2 = _run_gvn(prog(OFF - 8))
    check("different offsets are NOT collapsed",
          sum(1 for i in out2 if type(i).__name__ == 'IRLoadAddr') == 2)

    out3 = _run_gvn(prog(-8))             # second one foldable -> ineligible
    check("a foldable offset is never collapsed",
          sum(1 for i in out3 if type(i).__name__ == 'IRLoadAddr') == 2)


# ── 4. mem2reg interaction (design review 11d) ────────────────────────────────

def test_mem2reg_promotions_do_not_fall():
    """GVN emits `IRAssign(dest, leader)`, which is a USE of the leader that is
    not a load/store base -- mem2reg's escape analysis would taint the leader and
    refuse to promote the slot. `compiler._cp` cleans GVN's copies before
    mem2reg to prevent that. This test guards the reorder."""
    print("mem2reg promotions must not fall (guards the pipeline reorder)")
    import io, re, contextlib
    os.environ['APARA_MEM2REG_DEBUG'] = '1'

    def promoted(src, no_avn):
        if no_avn:
            os.environ['APARA_NO_AVN'] = '1'
        else:
            os.environ.pop('APARA_NO_AVN', None)
        import importlib
        importlib.reload(gvn)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                vec, _s, _r = ia.vectorize_all_module(copy.deepcopy(ia.build_ir(src)))
                ia.production_codegen(copy.deepcopy(vec))
        finally:
            os.environ.pop('APARA_NO_AVN', None)
            importlib.reload(gvn)
        return sum(int(m.group(1)) for m in
                   re.finditer(r'\[mem2reg\] vars=(\d+)', buf.getvalue()))

    try:
        for name, src in (('axpy vi32', suite.axpy('vi32_t')),
                          ('elementwise vi32', suite.elementwise('vi32_t'))):
            off = promoted(src, True)
            on = promoted(src, False)
            check(f"{name}: promotions {off} -> {on} (must not fall)", on >= off)
    finally:
        os.environ.pop('APARA_MEM2REG_DEBUG', None)


# ── 5. measured effect ────────────────────────────────────────────────────────

def test_redundancy_removed():
    print("the measured redundancy is actually removed")
    src = suite.gemm('vi16_t')
    off, m_off = build(src, no_avn=True)
    on, m_on = build(src, no_avn=False)
    check(f"gemm vi16: IRLoadAddr {n_loadaddr(off)} -> {n_loadaddr(on)}",
          n_loadaddr(on) < n_loadaddr(off))
    check(f"gemm vi16: mcode shrinks ({len(m_off.splitlines())} -> "
          f"{len(m_on.splitlines())} lines)",
          len(m_on.splitlines()) < len(m_off.splitlines()))


def test_no_effect_where_nothing_to_collapse():
    print("kernels with no large-offset duplicates are byte-identical")
    for name, src in (('dot vi8', suite.dot('vi8_t')),
                      ('reduction vi8', suite.reduction('vi8_t'))):
        _o, m_off = build(src, no_avn=True)
        _n, m_on = build(src, no_avn=False)
        check(f"{name}: emitted mcode identical with AVN on and off",
              m_off == m_on)


def main():
    for t in (test_key, test_kill_switch, test_tripwire_irloadaddr_shape,
              test_collapse_semantics, test_mem2reg_promotions_do_not_fall,
              test_redundancy_removed, test_no_effect_where_nothing_to_collapse):
        t()
    print()
    if _fails:
        print(f"{len(_fails)} FAILURES:")
        for f in _fails:
            print(f"  - {f}")
        return 1
    print("ALL R9.1 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

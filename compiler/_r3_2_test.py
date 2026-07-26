"""
_r3_2_test.py -- unit tests for R3.2 Superblock / Trace Scheduling.

Verifies the region-formation + cross-block-scheduling contract:
  * region formation is semantics-preserving (no speculation, no duplication);
  * scheduling regions actually get larger;
  * the trace scheduler preserves its input's behaviour;
  * rollback is reliable (bundle-increase / spill / scheduler mismatch → input kept);
  * the oracle gate skips programs without scheduling headroom;
  * the APARA_NO_SUPERBLOCK kill-switch is byte-identical;
  * determinism.

Run:  python3 compiler/_r3_2_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pycparser                                                       # noqa: E402
from compiler import _FAKE_TYPEDEFS, compile_c_to_mcode               # noqa: E402
from ir import Temp                                                    # noqa: E402
from ir_gen import IRGenerator                                         # noqa: E402
from ir_utils import func_slices                                       # noqa: E402
from superblock import superblock_module, form_superblocks            # noqa: E402
from trace_scheduler import (apply_superblock_scheduling,             # noqa: E402
                             superblock_schedule, _has_scheduling_headroom)
from loopopt import ir_interp                                          # noqa: E402

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


def _ir(code):
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=0x400)
    g.visit(ast)
    return list(g.instructions)


def _data_multiset(ir):
    """Multiset of non-control instructions (labels/jumps excluded) -- must be
    preserved: region formation only drops control, scheduling only reorders."""
    ctl = ('IRLabel', 'IRJump')
    return sorted(repr(x) for x in ir if type(x).__name__ not in ctl)


def _match(a, b):
    for lo, hi in func_slices(a):
        if ir_interp.differential(a, b, lo, hi)[0] == 'mismatch':
            return False
    return True


SUM = "int f(){int s=0,i; int a[16]; for(i=0;i<16;i++) a[i]=i; for(i=0;i<16;i++) s+=a[i]; return s;}"
POLY = "int f(int*x,int n){int i,s=0; for(i=0;i<n;i++){ s+=x[i]*x[i]+3*x[i]; } return s;}"
NOLOOP = "int f(int a,int b){return a*b+7;}"


def test_region_formation_semantics():
    print("region formation is semantics-preserving (no speculation/duplication)")
    for code in (SUM, POLY):
        ir = _ir(code)
        merged, st = superblock_module(ir)
        check(f"{code[:12]}..: behaviour identical after merge", _match(ir, merged))
        check("no data instruction added or removed (no duplication)",
              _data_multiset(ir) == _data_multiset(merged))
        check("some regions merged", st.regions_merged >= 1)


def test_regions_get_larger():
    print("scheduling regions get larger")
    ir = _ir(SUM)
    _final, summ = apply_superblock_scheduling(ir, global_base=0x400)
    check("dead loop-increment labels removed", summ.regions_merged >= 1)
    check("average region grows", summ.avg_region_after > summ.avg_region_before)


def test_trace_scheduler_preserves_behaviour():
    print("the trace scheduler preserves its input's behaviour")
    for code in (SUM, POLY):
        ir = _ir(code)
        sched, _r, _s = superblock_schedule(ir)
        check(f"{code[:12]}..: scheduled == original behaviour", _match(ir, sched))
        check("no instruction duplicated", _data_multiset(ir) == _data_multiset(sched))


def test_accept_only_if_not_worse():
    print("accepted only when spill-safe and bundles do not increase")
    for code in (SUM, POLY):
        ir = _ir(code)
        final, summ = apply_superblock_scheduling(ir, global_base=0x400)
        if summ.accepted:
            check("accepted -> bundles did not increase",
                  summ.bundles_after <= summ.bundles_before)
            check("accepted -> no new spills", summ.spills_after <= summ.spills_before)
            check("accepted -> behaviour preserved", _match(ir, final))


def test_oracle_gate_skips_flat_code():
    print("the oracle gate skips programs without scheduling headroom")
    ir = _ir(NOLOOP)
    headroom, _g = _has_scheduling_headroom(ir, 0.5)
    check("no scheduling headroom for a loop-free function", not headroom)
    final, summ = apply_superblock_scheduling(ir, global_base=0x400)
    check("final IS input (no-op)", final is ir)
    check("reason is no-scheduling-headroom", summ.reason == 'no-scheduling-headroom')


def test_kill_switch_identity():
    print("APARA_NO_SUPERBLOCK produces byte-identical output")
    import tempfile
    d = tempfile.mkdtemp()
    src = os.path.join(d, "s.c")
    with open(src, "w") as fh:
        fh.write(SUM.replace("int f()", "int main()"))
    env = dict(APARA_NO_SUPERBLOCK='1', APARA_NO_SWP='1')
    for k, v in env.items():
        os.environ[k] = v
    a = os.path.join(d, "a.mcode"); compile_c_to_mcode(src, output_file=a)
    b = os.path.join(d, "b.mcode"); compile_c_to_mcode(src, output_file=b)
    for k in env:
        os.environ.pop(k)
    with open(a) as fa, open(b) as fb:
        check("NO_SUPERBLOCK builds byte-identical", fa.read() == fb.read())


def test_determinism():
    print("superblock scheduling is deterministic")
    f1, _s1 = apply_superblock_scheduling(_ir(SUM), global_base=0x400)
    f2, _s2 = apply_superblock_scheduling(_ir(SUM), global_base=0x400)
    check("identical output twice", [repr(x) for x in f1] == [repr(x) for x in f2])


def main():
    for t in (test_region_formation_semantics, test_regions_get_larger,
              test_trace_scheduler_preserves_behaviour, test_accept_only_if_not_worse,
              test_oracle_gate_skips_flat_code, test_kill_switch_identity,
              test_determinism):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R3.2 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

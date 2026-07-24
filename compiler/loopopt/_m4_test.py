"""
_m4_test.py -- unit tests for the M4 LoopCanonicalizer.

Every test builds a hand-written loop, canonicalizes it, and checks four things
the milestone demands:

  * STRUCTURAL CHANGE   -- observed through the CFG-diff utility (cfgdiff.py).
  * VERIFIER SUCCESS    -- LoopVerifier passes on the regenerated descriptors.
  * DESCRIPTOR REGEN    -- discovery re-runs cleanly on the mutated IR (loop
                          identity / nesting preserved).
  * SEMANTIC EQUIVALENCE -- a small branch-executing IR interpreter (below)
                          produces identical (return value, memory) before and
                          after. (The compiler's own eval_ir gives up on any
                          branch, so it cannot execute a loop; this test-only
                          interpreter is the "eval_ir where applicable" stand-in
                          for looping code. It is a developer tool, never part
                          of the pipeline.)

Coverage: already-canonical, missing preheader (shared guard), multiple
preheader edges, multiple latches, nested loops, do-while, short-circuit
condition, multiple exits, irreducible (unchanged), no-change loop.

Run:  python3 compiler/loopopt/_m4_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ir import (IRFuncBegin, IRFuncEnd, IRLabel, IRBinOp, IRCondJump, IRJump,
                IRReturn, IRStore, IRLoad, IRLoadAddr, Temp, Const)
from analysis import build_cfg
from loopopt import discover
from loopopt.verify import LoopVerifier
from loopopt.canonicalize import LoopCanonicalizer, _irreducible_headers
from loopopt.cfgdiff import diff_loop, diff_cfg
from analysis import compute_dominators

_fails = []


def check(name, cond):
    print(f"  [{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        _fails.append(name)


# ── tiny branch-executing IR interpreter (test-only semantic oracle) ──────────

_CMP = {'<': lambda a, b: a < b, '<=': lambda a, b: a <= b,
        '>': lambda a, b: a > b, '>=': lambda a, b: a >= b,
        '==': lambda a, b: a == b, '!=': lambda a, b: a != b}
_BIN = {'+': lambda a, b: a + b, '-': lambda a, b: a - b,
        '*': lambda a, b: a * b}


def run(instrs, fname='f', budget=100000):
    """Execute function `fname` and return (return_value, sorted_memory_items).
    Supports the instruction subset the M4 loops use: labels, jumps, cond jumps,
    binops, stack address/load/store, returns. Deterministic; step-budgeted."""
    # isolate the function body
    lo = hi = None
    for i, ins in enumerate(instrs):
        c = type(ins).__name__
        if c == 'IRFuncBegin' and ins.name == fname:
            lo = i
        elif c == 'IRFuncEnd' and lo is not None and hi is None:
            hi = i
            break
    body = instrs[lo:hi + 1]
    labels = {ins.name: k for k, ins in enumerate(body)
              if type(ins).__name__ == 'IRLabel'}
    regs, mem = {}, {}

    def val(o):
        return o.value if type(o).__name__ == 'Const' else regs[o.name]

    pc, steps = 1, 0                      # skip IRFuncBegin at index 0
    while pc < len(body):
        steps += 1
        if steps > budget:
            raise RuntimeError("interpreter budget exceeded (non-terminating?)")
        ins = body[pc]
        c = type(ins).__name__
        if c in ('IRLabel', 'IRFuncBegin'):
            pc += 1
        elif c == 'IRFuncEnd':
            break
        elif c == 'IRBinOp':
            regs[ins.dest.name] = _BIN[ins.op](val(ins.left), val(ins.right))
            pc += 1
        elif c == 'IRLoadAddr':
            regs[ins.dest.name] = ins.fp_offset
            pc += 1
        elif c == 'IRLoad':
            regs[ins.dest.name] = mem.get(val(ins.base) + val(ins.offset), 0)
            pc += 1
        elif c == 'IRStore':
            mem[val(ins.base) + val(ins.offset)] = val(ins.src)
            pc += 1
        elif c == 'IRJump':
            pc = labels[ins.label]
        elif c == 'IRCondJump':
            taken = _CMP[ins.op](val(ins.left), val(ins.right))
            if taken:
                pc = labels[ins.true_label]
            elif ins.false_label is not None:
                pc = labels[ins.false_label]
            else:
                pc += 1
        elif c == 'IRReturn':
            return (val(ins.value) if ins.value is not None else None,
                    tuple(sorted(mem.items())))
        else:
            raise RuntimeError(f"interpreter: unsupported {c}")
    return (None, tuple(sorted(mem.items())))


# ── helpers ───────────────────────────────────────────────────────────────────

def _find(descs, header_label):
    return next((d for d in descs
                 if d.cfg.blocks[d.header].label == header_label), None)


def canonicalize_and_check(factory, header_label, expect_change,
                           expect=None, fname='f', extra=None):
    """Full harness: build twice (before/after), canonicalize the second, and
    assert CFG-diff / verifier / descriptor-regen / semantic equivalence."""
    Temp.reset(); before_ir = factory()
    Temp.reset(); after_ir = factory()

    before_descs = discover(before_ir)
    rep = LoopCanonicalizer().canonicalize(after_ir)
    after_descs = discover(after_ir)

    bd = _find(before_descs, header_label)
    ad = _find(after_descs, header_label)
    check("descriptor regenerated (loop identity preserved)", ad is not None)
    if ad is None:
        return rep

    # STRUCTURAL CHANGE via CFG diff
    ld = diff_loop(bd, ad)
    check("header identity preserved (never moved)", ld.identity_preserved)
    if expect_change:
        check("CFG-diff shows a structural change", ld.structurally_changed)
    else:
        check("CFG-diff shows NO change (no-op)", not ld.structurally_changed)

    # VERIFIER SUCCESS on regenerated descriptors
    v = LoopVerifier().verify_all(after_descs)
    check("LoopVerifier passes after canonicalization", v.ok)
    check("no verifier failures recorded", rep.verifier_failures == 0)

    # SEMANTIC EQUIVALENCE
    try:
        rb = run(before_ir, fname)
        ra = run(after_ir, fname)
        check(f"semantics preserved (before={rb[0]} after={ra[0]})", rb == ra)
    except RuntimeError as e:
        check(f"semantics not executable here ({e}) -- skipped", expect is None)

    if extra:
        extra(rep, ad, ld)
    return rep


# ── executable loop factories (sum 0..n-1 into slot -16) ──────────────────────

def _sum_body(nconst):
    # slot -8 = i, slot -16 = acc; body: acc += i; i += 1
    ai, av, av2 = Temp('ai'), Temp('av'), Temp('av2')
    bi, bv, bi2 = Temp('bi'), Temp('bv'), Temp('bi2')
    return [
        IRLabel('body'),
        IRLoadAddr(Temp('pa'), -16), IRLoad(av, Temp('pa'), Const(0), 8),
        IRLoadAddr(Temp('pi'), -8), IRLoad(bv, Temp('pi'), Const(0), 8),
        IRBinOp(av2, '+', av, bv),
        IRLoadAddr(Temp('pa2'), -16), IRStore(Temp('pa2'), Const(0), av2, 8),
        IRBinOp(bi2, '+', bv, Const(1)),
        IRLoadAddr(Temp('pi2'), -8), IRStore(Temp('pi2'), Const(0), bi2, 8),
        IRJump('head'),
    ]


def _init():
    return [IRLoadAddr(Temp('z1'), -8), IRStore(Temp('z1'), Const(0), Const(0), 8),
            IRLoadAddr(Temp('z2'), -16), IRStore(Temp('z2'), Const(0), Const(0), 8)]


def _head_test(n):
    iv = Temp('civ')
    return [IRLabel('head'),
            IRLoadAddr(Temp('pc'), -8), IRLoad(iv, Temp('pc'), Const(0), 8),
            IRCondJump(iv, '<', Const(n), 'body', 'exit')]


def _exit_ret():
    rv = Temp('rv')
    return [IRLabel('exit'),
            IRLoadAddr(Temp('pr'), -16), IRLoad(rv, Temp('pr'), Const(0), 8),
            IRReturn(rv)]


def canonical_loop():
    """Already canonical: dedicated preheader 'pre', single latch, dedicated exit."""
    return ([IRFuncBegin('f', [], {}, 0)] + _init()
            + [IRLabel('pre'), IRJump('head')]
            + _head_test(5) + _sum_body(5) + _exit_ret()
            + [IRFuncEnd('f')])


# ── tests ─────────────────────────────────────────────────────────────────────

def test_already_canonical_is_noop():
    print("already-canonical loop -> no-op:")
    rep = canonicalize_and_check(canonical_loop, 'head', expect_change=False,
                                 expect='run')
    check("no preheader created", rep.preheaders_created == 0)
    check("no latch normalized", rep.latches_normalized == 0)
    check("loop counted unchanged", rep.loops_unchanged == 1 and rep.loops_modified == 0)


def test_missing_preheader_shared_guard():
    print("missing preheader (guard block has two successors):")
    def f():
        # guard: if 0 < n goto head else skip ; skip merges away from the loop
        return ([IRFuncBegin('f', [], {}, 0)] + _init()
                + [IRCondJump(Const(0), '<', Const(5), 'head', 'skip'),
                   IRLabel('skip'), IRJump('exit')]
                + _head_test(5) + _sum_body(5)
                + [IRLabel('exit'),
                   IRLoadAddr(Temp('pr'), -16), IRLoad(Temp('rv'), Temp('pr'), Const(0), 8),
                   IRReturn(Temp('rv'))]
                + [IRFuncEnd('f')])
    def extra(rep, ad, ld):
        check("a preheader was created", rep.preheaders_created >= 1)
        check("header now has a dedicated preheader", ad.preheader is not None
              and len(ad.cfg.succs(ad.preheader)) == 1)
    canonicalize_and_check(f, 'head', expect_change=True, expect='run', extra=extra)


def test_multiple_preheader_edges():
    print("multiple incoming edges to the header (>=2 external preds):")
    def f():
        return ([IRFuncBegin('f', [], {}, 0)] + _init()
                + [IRCondJump(Const(1), '==', Const(1), 'head', 'alt'),
                   IRLabel('alt'), IRJump('head')]
                + _head_test(5) + _sum_body(5) + _exit_ret()
                + [IRFuncEnd('f')])
    def extra(rep, ad, ld):
        check("preheader created", rep.preheaders_created >= 1)
        check("exactly one external predecessor now",
              len([p for p in ad.cfg.preds(ad.header) if p not in ad.body_blocks]) == 1)
        check("preheader-change reported by CFG diff", ld.preheader_change is not None)
    canonicalize_and_check(f, 'head', expect_change=True, expect='run', extra=extra)


def test_multiple_latches():
    print("multiple latches -> single latch:")
    def f():
        # two back edges into head: one from body, one from an extra tail 'more'
        return ([IRFuncBegin('f', [], {}, 0)] + _init()
                + [IRLabel('pre'), IRJump('head')]
                + [IRLabel('head'),
                   IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('civ'), Temp('pc'), Const(0), 8),
                   IRCondJump(Temp('civ'), '<', Const(5), 'body', 'exit'),
                   IRLabel('body'),
                   IRLoadAddr(Temp('pa'), -16), IRLoad(Temp('av'), Temp('pa'), Const(0), 8),
                   IRBinOp(Temp('av2'), '+', Temp('av'), Temp('civ')),
                   IRLoadAddr(Temp('pa2'), -16), IRStore(Temp('pa2'), Const(0), Temp('av2'), 8),
                   IRBinOp(Temp('bi2'), '+', Temp('civ'), Const(1)),
                   IRLoadAddr(Temp('pi2'), -8), IRStore(Temp('pi2'), Const(0), Temp('bi2'), 8),
                   # split: even/odd both loop back, forming two latches
                   IRCondJump(Temp('bi2'), '<', Const(3), 'more', 'head'),
                   IRLabel('more'), IRJump('head')]
                + _exit_ret()
                + [IRFuncEnd('f')])
    def extra(rep, ad, ld):
        check("a latch was normalized", rep.latches_normalized >= 1)
        check("exactly one latch now", len(ad.latches) == 1)
        check("latch-change reported by CFG diff", ld.latch_change is not None)
    canonicalize_and_check(f, 'head', expect_change=True, expect='run', extra=extra)


def test_nested_loops():
    print("nested loops (both non-canonical: guarded headers) -> both canonical:")
    def f():
        # outer over slot -8 (i<2), inner over slot -24 (j<2). BOTH headers are
        # reached through a guard block with two successors, so each needs a
        # fresh preheader; nesting must survive.
        return [IRFuncBegin('f', [], {}, 0),
                IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
                IRLoadAddr(Temp('za'), -16), IRStore(Temp('za'), Const(0), Const(0), 8),
                IRCondJump(Const(0), '<', Const(2), 'oh', 'ox'),     # outer guard
                IRLabel('oh'),
                IRLoadAddr(Temp('pi'), -8), IRLoad(Temp('iv'), Temp('pi'), Const(0), 8),
                IRCondJump(Temp('iv'), '<', Const(2), 'ob', 'ox'),
                IRLabel('ob'),
                IRLoadAddr(Temp('zj'), -24), IRStore(Temp('zj'), Const(0), Const(0), 8),
                IRCondJump(Const(0), '<', Const(2), 'ih', 'ix'),     # inner guard
                IRLabel('ih'),
                IRLoadAddr(Temp('pj'), -24), IRLoad(Temp('jv'), Temp('pj'), Const(0), 8),
                IRCondJump(Temp('jv'), '<', Const(2), 'ib', 'ix'),
                IRLabel('ib'),
                IRLoadAddr(Temp('pa'), -16), IRLoad(Temp('av'), Temp('pa'), Const(0), 8),
                IRBinOp(Temp('av2'), '+', Temp('av'), Const(1)),
                IRLoadAddr(Temp('pa2'), -16), IRStore(Temp('pa2'), Const(0), Temp('av2'), 8),
                IRBinOp(Temp('jv2'), '+', Temp('jv'), Const(1)),
                IRLoadAddr(Temp('pj2'), -24), IRStore(Temp('pj2'), Const(0), Temp('jv2'), 8),
                IRJump('ih'),
                IRLabel('ix'),
                IRBinOp(Temp('iv2'), '+', Temp('iv'), Const(1)),
                IRLoadAddr(Temp('pi2'), -8), IRStore(Temp('pi2'), Const(0), Temp('iv2'), 8),
                IRJump('oh'),
                IRLabel('ox'),
                IRLoadAddr(Temp('pr'), -16), IRLoad(Temp('rv'), Temp('pr'), Const(0), 8),
                IRReturn(Temp('rv')),
                IRFuncEnd('f')]
    def extra(rep, ad, ld):
        after = discover(_relist(f))
        oh = _find(after, 'oh'); ih = _find(after, 'ih')
        check("both loops still discovered", oh is not None and ih is not None)
        check("inner nested in outer (body subset)", ih.body_blocks <= oh.body_blocks)
        check("inner depth > outer depth", ih.depth > oh.depth)
        check("both nested headers now have dedicated preheaders",
              oh.preheader is not None and len(oh.cfg.succs(oh.preheader)) == 1
              and ih.preheader is not None and len(ih.cfg.succs(ih.preheader)) == 1)
        check("preheaders created for BOTH nested loops", rep.preheaders_created >= 2)
    canonicalize_and_check(f, 'oh', expect_change=True, expect='run', extra=extra)


def _relist(factory):
    Temp.reset(); ir = factory()
    LoopCanonicalizer().canonicalize(ir)
    return ir


def test_do_while():
    print("do-while (bottom-tested) loop:")
    def f():
        # body first, test at the latch
        return [IRFuncBegin('f', [], {}, 0),
                IRLoadAddr(Temp('zi'), -8), IRStore(Temp('zi'), Const(0), Const(0), 8),
                IRLoadAddr(Temp('za'), -16), IRStore(Temp('za'), Const(0), Const(0), 8),
                IRLabel('body'),
                IRLoadAddr(Temp('pa'), -16), IRLoad(Temp('av'), Temp('pa'), Const(0), 8),
                IRLoadAddr(Temp('pi'), -8), IRLoad(Temp('iv'), Temp('pi'), Const(0), 8),
                IRBinOp(Temp('av2'), '+', Temp('av'), Temp('iv')),
                IRLoadAddr(Temp('pa2'), -16), IRStore(Temp('pa2'), Const(0), Temp('av2'), 8),
                IRBinOp(Temp('iv2'), '+', Temp('iv'), Const(1)),
                IRLoadAddr(Temp('pi2'), -8), IRStore(Temp('pi2'), Const(0), Temp('iv2'), 8),
                IRCondJump(Temp('iv2'), '<', Const(5), 'body', 'exit'),
                IRLabel('exit'),
                IRLoadAddr(Temp('pr'), -16), IRLoad(Temp('rv'), Temp('pr'), Const(0), 8),
                IRReturn(Temp('rv')),
                IRFuncEnd('f')]
    def extra(rep, ad, ld):
        check("do-while has single latch", len(ad.latches) == 1)
    # do-while entry falls straight into the header from init code (single
    # dedicated pred), so this may already be canonical -> allow either.
    Temp.reset(); before = discover(f())
    Temp.reset(); air = f(); rep = LoopCanonicalizer().canonicalize(air)
    ad = _find(discover(air), 'body')
    check("do-while descriptor regenerated", ad is not None)
    check("verifier passes", LoopVerifier().verify_all(discover(air)).ok)
    Temp.reset(); rb = run(f()); ra = run(air)
    check(f"do-while semantics preserved (before={rb[0]} after={ra[0]})", rb == ra)
    check("do-while single latch", ad is not None and len(ad.latches) == 1)


def test_short_circuit_condition():
    print("short-circuit loop condition (&& split across two header blocks):")
    def f():
        # while (i < 5 && i < 3) ... ; guard split into head/head2
        return ([IRFuncBegin('f', [], {}, 0)] + _init()
                + [IRLabel('pre'), IRJump('head'),
                   IRLabel('head'),
                   IRLoadAddr(Temp('pc'), -8), IRLoad(Temp('civ'), Temp('pc'), Const(0), 8),
                   IRCondJump(Temp('civ'), '<', Const(5), 'head2', 'exit'),
                   IRLabel('head2'),
                   IRCondJump(Temp('civ'), '<', Const(3), 'body', 'exit')]
                + _sum_body(3) + _exit_ret()
                + [IRFuncEnd('f')])
    def extra(rep, ad, ld):
        check("verifier passes on short-circuit loop",
              LoopVerifier().verify_all(discover(_relist(f))).ok)
    canonicalize_and_check(f, 'head', expect_change=False, expect='run', extra=extra)


def test_multiple_exits():
    print("multiple exits -> exits preserved:")
    def f():
        # extra early exit from the body when acc reaches a threshold
        return ([IRFuncBegin('f', [], {}, 0)] + _init()
                + [IRLabel('pre'), IRJump('head')]
                + _head_test(9)
                + [IRLabel('body'),
                   IRLoadAddr(Temp('pa'), -16), IRLoad(Temp('av'), Temp('pa'), Const(0), 8),
                   IRLoadAddr(Temp('pi'), -8), IRLoad(Temp('bv'), Temp('pi'), Const(0), 8),
                   IRBinOp(Temp('av2'), '+', Temp('av'), Temp('bv')),
                   IRLoadAddr(Temp('pa2'), -16), IRStore(Temp('pa2'), Const(0), Temp('av2'), 8),
                   IRCondJump(Temp('av2'), '>=', Const(10), 'exit2', 'cont'),
                   IRLabel('cont'),
                   IRBinOp(Temp('bi2'), '+', Temp('bv'), Const(1)),
                   IRLoadAddr(Temp('pi2'), -8), IRStore(Temp('pi2'), Const(0), Temp('bi2'), 8),
                   IRJump('head'),
                   IRLabel('exit2'),
                   IRLoadAddr(Temp('pr2'), -16), IRLoad(Temp('rv2'), Temp('pr2'), Const(0), 8),
                   IRReturn(Temp('rv2'))]
                + _exit_ret()
                + [IRFuncEnd('f')])
    def extra(rep, ad, ld):
        check("loop still has multiple exit targets", len(ad.exit_blocks) >= 2)
        check("single latch preserved", len(ad.latches) == 1)
    canonicalize_and_check(f, 'head', expect_change=False, expect='run', extra=extra)


def test_dedicated_exit_optin():
    print("dedicated-exit normalization (opt-in): shared exit block gets a pad:")
    def f():
        # while(i<9){ acc+=i; if(acc>=10) break; i++; }  -- the loop-condition
        # exit and the break BOTH land on 'done', which also has the break's
        # (out-of-loop) predecessor, so 'done' is a SHARED exit block.
        return ([IRFuncBegin('f', [], {}, 0)] + _init()
                + [IRLabel('pre'), IRJump('head')]
                + _head_test(9)
                + [IRLabel('body'),
                   IRLoadAddr(Temp('pa'), -16), IRLoad(Temp('av'), Temp('pa'), Const(0), 8),
                   IRLoadAddr(Temp('pi'), -8), IRLoad(Temp('bv'), Temp('pi'), Const(0), 8),
                   IRBinOp(Temp('av2'), '+', Temp('av'), Temp('bv')),
                   IRLoadAddr(Temp('pa2'), -16), IRStore(Temp('pa2'), Const(0), Temp('av2'), 8),
                   IRCondJump(Temp('av2'), '>=', Const(10), 'done', 'cont'),  # break -> done
                   IRLabel('cont'),
                   IRBinOp(Temp('bi2'), '+', Temp('bv'), Const(1)),
                   IRLoadAddr(Temp('pi2'), -8), IRStore(Temp('pi2'), Const(0), Temp('bi2'), 8),
                   IRJump('head')]
                + [IRLabel('exit'), IRJump('done'),          # loop-condition exit -> done
                   IRLabel('done'),
                   IRLoadAddr(Temp('pr'), -16), IRLoad(Temp('rv'), Temp('pr'), Const(0), 8),
                   IRReturn(Temp('rv'))]
                + [IRFuncEnd('f')])

    # DEFAULT canonicalizer leaves the shared exit alone (near-no-op guarantee).
    Temp.reset(); ir_def = f()
    before_types = [type(x).__name__ for x in ir_def]
    rep_def = LoopCanonicalizer().canonicalize(ir_def)
    check("default (exits off) makes NO exit change",
          rep_def.exits_normalized == 0
          and [type(x).__name__ for x in ir_def] == before_types)

    # OPT-IN canonicalizer inserts a dedicated landing pad and verifies clean.
    Temp.reset(); ir_before = f()
    Temp.reset(); ir_after = f()
    rep = LoopCanonicalizer(normalize_exits=True).canonicalize(ir_after)
    ad = _find(discover(ir_after), 'head')
    check("opt-in created an exit landing pad", rep.exits_normalized >= 1)
    check("no rollbacks (postcondition progresses)", rep.rollbacks == 0)
    check("verifier passes with exits normalized",
          LoopVerifier().verify_all(discover(ir_after)).ok)
    # every remaining loop-exit target is now entered only from inside the loop
    lc = LoopCanonicalizer(normalize_exits=True)
    check("no shared exits remain", len(lc._shared_exit_edges(ad)) == 0)
    rb = run(ir_before); ra = run(ir_after)
    check(f"semantics preserved (before={rb[0]} after={ra[0]})", rb == ra)


def test_irreducible_unchanged():
    print("irreducible loop -> left byte-for-byte unchanged:")
    def f():
        # two mutually reachable blocks A<->B with TWO entries (from entry and via
        # the branch) => no single header dominates => irreducible.
        return [IRFuncBegin('f', [], {}, 0),
                IRCondJump(Const(1), '==', Const(1), 'A', 'B'),
                IRLabel('A'),
                IRCondJump(Const(0), '<', Const(0), 'B', 'done'),
                IRLabel('B'),
                IRCondJump(Const(0), '<', Const(0), 'A', 'done'),
                IRLabel('done'),
                IRReturn(Const(0)),
                IRFuncEnd('f')]
    Temp.reset(); ir = f()
    cfg = build_cfg(ir, 0, len(ir) - 1)
    irr = _irreducible_headers(cfg, compute_dominators(cfg))
    check("test CFG is genuinely irreducible", len(irr) >= 1)
    before = [type(x).__name__ for x in ir]
    before_descs = discover(ir)
    check("no natural loop discovered (irreducible)", len(before_descs) == 0)
    rep = LoopCanonicalizer().canonicalize(ir)
    after = [type(x).__name__ for x in ir]
    check("IR left completely unchanged", before == after)
    check("nothing modified; nothing rolled back",
          rep.loops_modified == 0 and rep.rollbacks == 0)
    check("irreducible reported as skipped", rep.irreducible_skipped >= 1)


def test_no_change_loop():
    print("minimal loop that needs no changes:")
    def f():
        return ([IRFuncBegin('f', [], {}, 0)] + _init()
                + [IRLabel('pre'), IRJump('head')]
                + _head_test(1) + _sum_body(1) + _exit_ret()
                + [IRFuncEnd('f')])
    Temp.reset(); ir = f()
    before = [type(x).__name__ for x in ir]
    rep = LoopCanonicalizer().canonicalize(ir)
    after = [type(x).__name__ for x in ir]
    check("no-change loop: IR identical", before == after)
    check("no-change loop: 0 mutations", rep.loops_modified == 0)


def main():
    tests = [test_already_canonical_is_noop,
             test_missing_preheader_shared_guard,
             test_multiple_preheader_edges,
             test_multiple_latches,
             test_nested_loops,
             test_do_while,
             test_short_circuit_condition,
             test_multiple_exits,
             test_dedicated_exit_optin,
             test_irreducible_unchanged,
             test_no_change_loop]
    for t in tests:
        Temp.reset()
        t()
    print()
    if _fails:
        print(f"M4 TESTS FAILED ({len(_fails)}): {_fails}")
        return 1
    print("M4 TESTS PASSED")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

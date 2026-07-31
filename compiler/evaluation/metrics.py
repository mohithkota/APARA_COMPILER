"""metrics.py -- per-program measurement primitives (R4.6.5).

Measurement only: nothing here changes compilation. Every number is taken from
the REAL production path -- tier-1 scalar optimizer + R3.2 superblock + codegen +
bundler -- so the figures are what actually ships, not a pre-optimizer proxy.
"""
import os, sys, copy, time, re
_HERE = os.path.dirname(os.path.abspath(__file__))
_C = os.path.dirname(_HERE)
sys.path.insert(0, _C)

import pycparser
from compiler import _FAKE_TYPEDEFS
from ir import Temp
from ir_gen import IRGenerator
from codegen import CodeGen
from bundler import bundle_mcode
from vector_size_probe import _optimize_like_production, _superblock
from dot_vectorizer import vectorize_all_module
import vector_compact_loop as _vcl

GB = 0x400
ISSUE_WIDTH = 8          # R4.0 capability database
MEM_LANES = 4
DIV_LANES = 1

_MEM = re.compile(r'^\s*\$(ld|st)')
_VEC = re.compile(r'^\s*\$(v |dot|vreduce)')
_BR = re.compile(r'^\s*\$(j|call|ret|b)')


def build_ir(code):
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=GB)
    g.visit(ast)
    return list(g.instructions)


def production_measure(ir):
    """Compile `ir` exactly as production would and return raw counters."""
    opt = _optimize_like_production(copy.deepcopy(ir))
    opt = _superblock(opt, GB)
    body = CodeGen(global_base=GB).generate(copy.deepcopy(opt), global_base=GB)
    _text, n_instr, n_bundles = bundle_mcode(body, schedule=True)
    lines = [l for l in body.splitlines() if l.strip()]
    mem = sum(1 for l in lines if _MEM.match(l))
    vec = sum(1 for l in lines if _VEC.match(l))
    br = sum(1 for l in lines if _BR.match(l))
    return dict(static_instructions=n_instr, static_bundles=n_bundles,
                code_size=len(body), mem_ops=mem, vec_ops=vec, branches=br)


def measure(name, code, vectorize=True):
    """Full measurement of one benchmark. Returns a flat dict (one CSV row)."""
    t0 = time.time()
    ir = build_ir(code)
    decision, realisation, reason, dyn_s, dyn_v = 'scalar', '-', '', 0, 0
    used = ir
    if vectorize:
        out, stats, reps = vectorize_all_module(copy.deepcopy(ir))
        if stats.vectorized:
            decision, used = 'vectorized', out
            realisation = _vcl.realisation_of(out)
            r = reps[0]
            dyn_s, dyn_v = r.scalar_dynamic, r.vector_dynamic
            reason = 'ok'
        else:
            r = reps[0] if reps else None
            reason = (r.reason.split('|')[0] if r else 'not-recognised')
            decision = 'rolled-back' if stats.rolled_back else 'rejected'
    m = production_measure(used)
    compile_s = time.time() - t0
    b = max(1, m['static_bundles'])
    return dict(
        benchmark=name, decision=decision, realisation=realisation,
        reason=reason,
        static_instructions=m['static_instructions'],
        static_bundles=m['static_bundles'],
        code_size=m['code_size'],
        ipb=round(m['static_instructions'] / b, 4),
        bundle_occupancy=round(m['static_instructions'] / b / ISSUE_WIDTH, 4),
        mem_lane_occupancy=round(m['mem_ops'] / b / MEM_LANES, 4),
        vec_ops_per_bundle=round(m['vec_ops'] / b, 4),
        branch_density=round(m['branches'] / max(1, m['static_instructions']), 4),
        dynamic_scalar_ops=dyn_s, dynamic_vector_ops=dyn_v,
        dynamic_reduction=(round(100.0 * (dyn_s - dyn_v) / dyn_s, 1)
                           if dyn_s else 0.0),
        compile_seconds=round(compile_s, 3))


def oracle_of(code):
    """R3.0 oracle over the SCALAR form: the theoretical ceiling and the reason
    the ceiling is not met. Reused unmodified -- no second analyzer."""
    from loopopt.oracle_ilp import analyze_module
    try:
        loops = analyze_module(build_ir(code))
    except Exception:
        return None
    if not loops:
        return None
    best = max(loops, key=lambda l: getattr(l, 'n_instr', 0) or 0)
    return dict(
        theoretical_ipb=round(getattr(best, 'theoretical_ipb', 0.0), 4),
        local_ideal_ipb=round(getattr(best, 'local_ideal_ipb', 0.0), 4),
        achieved_ipb=round(getattr(best, 'achieved_ipb', 0.0), 4),
        utilization=round(getattr(best, 'utilization', 0.0), 4),
        pipelining_gap=round(getattr(best, 'pipelining_gap', 0.0), 4),
        scheduler_gap=round(getattr(best, 'scheduler_gap', 0.0), 4),
        limiter=getattr(best, 'limiter', '?'),
        top_opportunity=getattr(best, 'top_opportunity', '?'),
        n_loops=len(loops))

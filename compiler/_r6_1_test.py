"""_r6_1_test.py -- unit tests for the R6.1 Vector Backend Analysis Framework.

The framework is ANALYSIS ONLY, so the tests fall into three groups:
  1. the measurements are internally consistent (slot accounting closes, every
     empty slot is classified, bounds bound);
  2. the reconstruction is FAITHFUL -- the bundles it reports are the bundles
     the production bundler emits, instruction for instruction;
  3. running it changes NOTHING -- compiled output is byte-identical before and
     after the whole analysis runs.
"""
import os, sys, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# R6.4.1 NOTE: adaptive unroll-factor selection picks 4x for the dot kernel,
# which flips R4.2.5's realisation probe to the FULLY-UNROLLED form. There is
# then no compact loop body to build a dependence graph over and `r.hot` is
# None, so `test_depgraph` raised AttributeError. This is the same class of
# breakage R6.4 hit in _r4_2_5 and _r4_3, and it gets the same remedy: the R6.1
# properties under test (issue model, occupancy attribution, dependence graph of
# a vector loop) are independent of the unroll factor, so the factor is PINNED
# here rather than any assertion being weakened. `setdefault` keeps an explicit
# APARA_VECTOR_UNROLL from the environment authoritative. The realisation flip
# itself is measured in R6_5_CROSS_ITERATION_SCHEDULING.md.
os.environ.setdefault('APARA_VECTOR_UNROLL', '1')

import bundler as _b
from codegen import CodeGen
from vector_backend import latency as lat
from vector_backend import occupancy as occ
from vector_backend import dependency_graph as dep
from vector_backend import ilp_analysis as ia

_fails = []


def check(n, c):
    print(f"  [{'ok' if c else 'FAIL'}] {n}")
    if not c:
        _fails.append(n)


K_AXPY = ('axpy vi8', 'axpy',
          'long long f(){vi8_t X[64],Y[64];int i;int a=3;'
          'for(i=0;i<64;i++)Y[i]+=a*X[i];return Y[0];}')
K_DOT = ('dot vi8', 'dot',
         'long long f(){vi8_t a[64],b[64];int i;long long s=0;'
         'for(i=0;i<64;i++)s+=a[i]*b[i];return s;}')
K_RED = ('reduction vi8', 'reduction',
         'long long f(){vi8_t a[64];int i;long long s=0;'
         'for(i=0;i<64;i++)s+=a[i];return s;}')
K_CONV = ('conv 3-tap', 'convolution',
          'long long f(){vi8_t in[72],out[72];int i;'
          'for(i=0;i<61;i++)out[i]=in[i+0]+in[i+1]+in[i+2];return out[0];}')
SMALL = [K_AXPY, K_DOT, K_RED, K_CONV]

_CACHE = {}


def kern(k):
    if k[0] not in _CACHE:
        _CACHE[k[0]] = ia.analyze_kernel(*k)
    return _CACHE[k[0]]


# ── 1. the issue model comes from the capability database ─────────────────────

def test_model():
    print("issue/lane model is taken from the R4.0 capability database")
    from vector_capability_db import LANE_CAPS, REGISTER_POOL
    check("issue width", lat.ISSUE_WIDTH == LANE_CAPS['total'] == 8)
    check("memory lanes", lat.MEM_LANES == LANE_CAPS['mem'] == 4)
    check("divide lane", lat.DIV_LANES == LANE_CAPS['div_sqrt'] == 1)
    check("register pool", lat.REG_POOL == REGISTER_POOL == 28)
    check("vector ops classified",
          lat.mcode_class('$v * $r1 ($vi8) $r2 $r3') == 'VMUL' and
          lat.mcode_class('$v + $r1 ($vi8) $r2 $r3') == 'VADD' and
          lat.mcode_class('$dot $accumulate $r1 ($vi8) $r2 $r3') == 'VDOT' and
          lat.mcode_class('$vreduce + $r1 ($vi8) $r2') == 'VREDUCE')
    check("wide load is a vector load",
          lat.mcode_class('$ld ($u128) $r4 [$r5 + 0]') == 'VLOAD' and
          lat.mcode_class('$ld ($i64) $r4 [$r5 + 0]') == 'LOAD')
    check("resource classes match the lanes the bundler limits",
          lat.mcode_resource('$ld ($i64) $r1 [$r2 + 0]') == 'MEM' and
          lat.mcode_resource('? ($i64) $r1 > $goto L') == 'CTL' and
          lat.mcode_resource('/ $r1 ($i64) $r2 $r3') == 'DIV')
    check("latency is the frozen R2.4 model, not a new one",
          lat.ir_latency.__module__.endswith('latency') and
          lat.mcode_latency('$ld ($i64) $r1 [$r2 + 0]') == 3)


# ── 2. fidelity: the reconstruction IS the production bundler ─────────────────

def test_fidelity():
    print("reported bundles are identical to the production bundler's")
    for k in SMALL:
        r = kern(k)
        check(f"{k[0]}: verified against bundler._pack_bundles", r.occ.verified)
    # and against the public API, which is what compiler.py calls
    r = kern(K_AXPY)
    text = '\n'.join('||\n' + '\n'.join('    ' + t for t in b.instrs) + '\n;'
                     for b in r.occ.bundles)
    check("bundle count matches bundle_mcode's n_after",
          len(r.occ.bundles) == _b.bundle_mcode(
              CodeGen(global_base=0x400).generate(
                  copy.deepcopy(ia.production_codegen(
                      ia.vectorize_all_module(ia.build_ir(K_AXPY[2]))[0])[0]),
                  global_base=0x400))[2])
    check("no bundle exceeds the issue width",
          all(b.occupied <= lat.ISSUE_WIDTH for b in r.occ.bundles))
    check("capacity is a legal aligner capacity",
          all(b.capacity in (1, 2, 4, 8) for b in r.occ.bundles))


# ── 3. slot accounting closes, and every empty slot is classified ─────────────

def test_slot_accounting():
    print("slot accounting closes and 100% of empty slots are classified")
    for k in SMALL:
        r = kern(k)
        t = r.occ.totals()
        check(f"{k[0]}: slots = 8 x bundles",
              abs(t['issue_slots'] - 8 * t['bundles']) < 1e-9)
        check(f"{k[0]}: empty = slots - issued",
              abs(t['empty_slots'] - (t['issue_slots'] - t['instructions'])) < 1e-9)
        h = r.occ.cause_histogram()
        check(f"{k[0]}: causes sum EXACTLY to the empty slots",
              abs(sum(h.values()) - t['empty_slots']) < 1e-9)
        check(f"{k[0]}: every cause is in the taxonomy",
              all(c in occ.CAUSES for c in h))
        check(f"{k[0]}: encoded + issue-only = empty",
              abs(t['encoded_empty'] + t['issue_only_empty']
                  - t['empty_slots']) < 1e-9)
        d = r.occ.totals(dynamic=True)
        hd = r.occ.cause_histogram(dynamic=True)
        check(f"{k[0]}: dynamic causes also sum exactly",
              abs(sum(hd.values()) - d['empty_slots']) < 1e-9)
        check(f"{k[0]}: a full bundle is never charged for empty slots",
              all(b.empty > 0 or b.cause == 'bundle-full' or b.occupied == 8
                  for b in r.occ.bundles if b.occupied == 8))


def test_causes_are_specific():
    """The refinement that makes the report useful: a RAW split is attributed to
    the KIND of producer, and a packed 64-bit load feeding a $v is a VECTOR load
    even though its opcode is indistinguishable from a scalar one."""
    print("empty-slot causes name the actual producer")
    r = kern(K_AXPY)
    causes = {b.cause for b in r.body_bundles}
    check("axpy body waits on a vector load",
          'waiting-for-vector-load' in causes)
    check("the packed 64-bit load is classified VLOAD, not LOAD",
          any('VLOAD' in b.classes for b in r.body_bundles))
    check("the packed 64-bit store is classified VSTORE",
          any('VSTORE' in b.classes for b in r.body_bundles))
    rd = kern(K_DOT)
    check("dot body waits on the reduction",
          'waiting-for-reduction' in {b.cause for b in rd.body_bundles})


# ── 4. dependence graph ───────────────────────────────────────────────────────

def test_depgraph():
    """R6.2 NOTE: axpy vi8 is no longer realised as a compact loop. Stronger
    memory disambiguation made the fully-unrolled candidate pack better, so
    R4.2.5's size probe now selects it and there is no loop body to graph. The
    test moved to a kernel that is still a compact loop rather than being
    weakened -- the realisation change itself is asserted in _r6_2_test.py."""
    print("vector-IR dependence graph and its derived metrics")
    r = kern(K_DOT)
    g = r.hot
    check("a vector loop was found", g is not None and g.is_vector_loop)
    check("it contains a vector operation", g.n_vector_ops >= 1)
    check("edges are typed", g.edge_counts['RAW'] > 0)
    check("loop-carried edges exist", g.n_carried > 0)
    check("critical path >= the longest single latency",
          g.crit_path_true >= max(lat.ir_latency(g.graph.instrs[o])
                                  for o in g.ops))
    check("critical path <= total work", g.crit_path_true <= g.total_latency)
    check("parallelism = work / span",
          abs(g.available_parallelism
              - g.total_latency / g.crit_path_true) < 1e-9)
    check("dependency depth is under the op count", g.dep_depth < g.n_ops)
    check("ready-set histogram counts every scheduling step",
          sum(g.ready_hist.values()) == g.ideal_steps)
    check("the ideal schedule issues every operation",
          g.ideal_steps >= 1 and g.ideal_ipb <= lat.ISSUE_WIDTH)
    check("true-dependence span <= all-dependence span",
          g.crit_path_true <= g.crit_path_all)
    check("the body has little intra-iteration parallelism",
          g.avg_ready < 3.0)


def test_depgraph_analysis_only():
    print("building the graph mutates no IR")
    ir = ia.build_ir(K_DOT[2])
    vec, _st, _rp = ia.vectorize_all_module(copy.deepcopy(ir))
    before = [repr(i) for i in vec]
    dep.analyze_module(vec)
    check("IR unchanged", [repr(i) for i in vec] == before)


# ── 5. frequencies ────────────────────────────────────────────────────────────

def test_frequencies():
    """Uses a kernel still realised as a COMPACT LOOP. R6.2 moved axpy vi8 to the
    fully-unrolled realisation, which has no loop and therefore no trip count --
    an intended consequence, asserted in _r6_2_test.py."""
    print("execution frequencies come from proved trip counts")
    ir = ia.build_ir(K_DOT[2])
    vec, _st, _rp = ia.vectorize_all_module(copy.deepcopy(ir))
    freq, unknown = ia.label_frequencies(vec)
    check("the vector body's trip count is 64/8 = 8",
          freq.get('vcl_2_body') == 8)
    check("the loop header runs trip+1 times", freq.get('vcl_1_cond') == 9)
    check("code outside the loop runs once", freq.get('vcl_4_end') == 1)
    check("nothing unknown in a counted loop", not unknown)
    r = kern(K_DOT)
    d, s = r.dynamic(), r.static()
    check("dynamic bundles exceed static ones", d['bundles'] > s['bundles'])
    check("dynamic occupancy is lower (the hot loop is the sparse part)",
          d['occupancy'] < s['occupancy'])


# ── 6. bounds actually bound ──────────────────────────────────────────────────

def test_bounds():
    print("the scheduling bounds are sound")
    for k in SMALL:
        r = kern(k)
        ents = [r.occ.flat[i] for b in r.body_bundles for i in b.flat_idx]
        lb = ia.pack_lower_bound(ents)
        check(f"{k[0]}: lower bound <= shipped bundles",
              lb <= len(r.body_bundles))
        check(f"{k[0]}: lower bound >= ceil(N/8)",
              lb >= -(-len(ents) // 8))
        w = r.whatif.get('local_sched', {})
        check(f"{k[0]}: reported headroom is never negative",
              w.get('gain', 0) >= 0)


def test_whatif():
    print("what-if experiments repack with the production packer")
    r = kern(K_AXPY)
    u1 = ia.whatif_unroll(r, 1)
    check("u=1 model reproduces one iteration's work",
          u1['ok'] and u1['bundles_per_iter'] <= len(r.body_bundles))
    for u in (2, 4):
        a = ia.whatif_unroll(r, u)
        b = ia.whatif_unroll(r, u, dedup_bases=True, disambiguate=True)
        check(f"u={u}: unrolling never increases bundles per iteration",
              a['bundles_per_iter'] <= u1['bundles_per_iter'] + 1e-9)
        check(f"u={u}: disambiguation is at least as good as not having it",
              b['bundles_per_iter'] <= a['bundles_per_iter'] + 1e-9)
        check(f"u={u}: register cost is reported",
              'registers_needed' in b and 'registers_short' in b)
    # R6.2 moved axpy vi8 to the fully unrolled realisation, so "bundles per
    # iteration" now counts a body that already covers 8 vector chunks. The
    # invariant that still holds -- and the one worth asserting -- is that the
    # synthetic disambiguation model can no longer beat the real bundler by much,
    # because the real bundler now does the same reasoning.
    _u4 = ia.whatif_unroll(r, 4)
    _u4d = ia.whatif_unroll(r, 4, dedup_bases=True, disambiguate=True)
    check("the synthetic disambiguation model no longer beats the real "
          "bundler by more than 25% (R6.2 closed the gap it was modelling)",
          _u4d['bundles_per_iter'] >= 0.75 * _u4['bundles_per_iter'])
    d = kern(K_DOT)
    shared = ia.whatif_unroll(d, 4, dedup_bases=True, disambiguate=True)
    indep = ia.whatif_unroll(d, 4, dedup_bases=True, disambiguate=True,
                             rename_carried=True)
    check("dot needs INDEPENDENT accumulators to gain anything",
          indep['bundles_per_iter'] < shared['bundles_per_iter'])
    rr = ia.whatif_reassociate(kern(K_RED))
    check("the reduction's serial chain is found and shortened",
          rr['ok'] and rr['tree_depth'] < rr['chain_len'])


def test_alias_model():
    print("the perfect-disambiguation model preserves real conflicts")
    e = ia._entry
    ents = [e('$st ($i64) [$r8 + 0] $r12'), e('$ld ($i64) $r9 [$r8 + 0]'),
            e('$ld ($i64) $r10 [$r7 + 0]')]
    out = ia._perfect_alias_rewrite(ents)
    check("same base + same offset stays a conflict",
          _b._mem_may_alias(out[1]['mem_access'], (out[0]['mem_write'],)))
    check("different objects become provably disjoint",
          not _b._mem_may_alias(out[2]['mem_access'], (out[0]['mem_write'],)))
    check("duplicate base registers are detected",
          any(len(ia.base_alias_map(kern(k).occ.flat)) >= 1 for k in SMALL))


# ── 7. the whole thing changes nothing ────────────────────────────────────────

def test_analysis_only():
    print("running the analysis leaves compilation byte-identical")
    def build(src):
        ir = ia.build_ir(src)
        vec, _s, _r = ia.vectorize_all_module(copy.deepcopy(ir))
        return ia.production_codegen(vec)[1]
    before = {k[0]: build(k[2]) for k in SMALL}
    for k in SMALL:
        kern(k)
        ia.rank_opportunities([kern(x) for x in SMALL])
    after = {k[0]: build(k[2]) for k in SMALL}
    check("mcode identical before and after the analysis", before == after)
    # R6.2 deliberately changed this: bundler.py now consults the symbolic
    # memory model. The MEASUREMENT half of vector_backend must still be absent
    # from every production path, which is what is asserted here.
    import re as _re
    for f in ('compiler.py', 'bundler.py', 'codegen.py', 'vector_pipeline.py'):
        src = open(f).read()
        # match IMPORTS, not the word: bundler.py has always had a comment about
        # "bundle occupancy", and a substring test would flag it forever.
        imported = _re.findall(r'(?:^|\W)(?:import|from)\s+([\w.]+)', src)
        check(f"{f}: imports no R6.1 measurement module",
              not any(m.split('.')[-1] in ('ilp_analysis', 'occupancy',
                                           'dependency_graph', 'latency')
                      for m in imported))
    check("bundler consults the R6.2 memory model (intended integration)",
          'mem_dependence' in open('bundler.py').read())


def test_report():
    print("the report renders with every required section")
    rs = [kern(k) for k in SMALL]
    md = '\n'.join(ia.format_report(rs, ia.rank_opportunities(rs)))
    for sec in ('Per-kernel reports', 'Kernel statistics',
                'Per-bundle statistics', 'Occupancy histograms',
                'Dependency graphs', 'Critical path analysis',
                'Ready-queue analysis', 'Empty-slot classification',
                'Ranked optimization opportunities', 'Threats to validity'):
        check(f"section present: {sec}", sec in md)
    check("slot0..slot7 rendering present",
          'slot0' in md and 'slot7' in md and 'EMPTY' in md)


def main():
    for t in (test_model, test_fidelity, test_slot_accounting,
              test_causes_are_specific, test_depgraph, test_depgraph_analysis_only,
              test_frequencies, test_bounds, test_whatif, test_alias_model,
              test_analysis_only, test_report):
        t()
    print()
    if _fails:
        print(f"FAIL ({len(_fails)}): {_fails}")
        return 1
    print("ALL R6.1 UNIT TESTS PASS")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

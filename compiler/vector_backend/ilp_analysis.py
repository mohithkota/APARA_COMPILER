"""
ilp_analysis.py -- the R6.1 driver: measure why vector issue slots are empty and
rank what would fill them.

ANALYSIS ONLY.  Compiles kernels through the REAL production path and measures
the result.  No IR is mutated, no scheduling or bundling decision is changed, and
nothing in this file is imported by the compiler.

--------------------------------------------------------------------------------
PIPELINE POSITION
--------------------------------------------------------------------------------
    Scalar IR -> Vectorizer -> Vector IR -> [R6.2+ backend optimizer] ->
    Scheduler -> Bundler -> Code generation

Everything measured here is taken at the two points that bracket the empty box:

  VECTOR IR   the IR that codegen actually consumes -- i.e. AFTER the vectorizer
              AND after the scalar optimizer/superblock pass that production runs
              on it (`vector_size_probe._optimize_like_production` + `_superblock`,
              the same helpers `evaluation/metrics.py` uses).  This is the input
              language an R6.2 vector backend optimizer would rewrite, so it is
              the only honest place to measure the available parallelism.

  BUNDLES     the output of the real scheduler + bundler on that IR.  This is
              what ships, and where the empty slots are.

TRIP COUNTS come from the pre-optimizer vectorized IR, where the induction
variable is still in its memory-slot form and `loopopt.analysis_iv` can prove the
count; the scalar optimizer's register promotion erases that form (a known R2.6
interaction, STATUS.md).  Labels survive both, so the counts are attached by
LABEL and carried onto the mcode regions.

--------------------------------------------------------------------------------
WHAT-IF EXPERIMENTS  (the evidence behind the opportunity ranking)
--------------------------------------------------------------------------------
Every estimated gain in the ranking is produced by re-running the PRODUCTION
scheduler and packer on a synthesised instruction stream, not by a formula:

  unroll(u)          the vector loop body replicated u times with real register
                     renaming out of the measured free-register pool, in two
                     addressing forms (see `whatif_unroll`).
  accumulators(k)    the same, with the reduction accumulator renamed per copy
                     (legal only for an associative reduction).
  local schedule     the best bundle count ANY local reordering of the block can
                     reach, under the bundler's own ordering rules.
  pipelining         the MII lower bound from the measured dependence graph.

The ranking then converts each result into a projected DYNAMIC IPB for the whole
program using the measured execution frequencies.
"""

import copy
import csv
import os
import re
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_C = os.path.dirname(_HERE)
sys.path.insert(0, _C)
sys.path.insert(0, os.path.join(_C, 'evaluation'))

import bundler as _b                                                 # noqa: E402
from codegen import CodeGen                                          # noqa: E402
from ir_utils import func_slices                                     # noqa: E402
from dot_vectorizer import vectorize_all_module                      # noqa: E402
from vector_size_probe import _optimize_like_production, _superblock  # noqa: E402
from loopopt.discovery import discover_function                      # noqa: E402
from loopopt.analysis_iv import annotate_induction_vars, TripCount   # noqa: E402

from . import latency as lat                                         # noqa: E402
from . import occupancy as occ                                       # noqa: E402
from . import dependency_graph as dep                                # noqa: E402

GB = 0x400          # the global base every measurement in this repo uses


# ── the kernel suite ──────────────────────────────────────────────────────────
#
# The six vectorizable families of the frozen R4.6.5 suite (reused verbatim from
# `evaluation/runner.py` -- no second benchmark definition) plus the 2-D
# convolution family from `conv_corpus.py`, which R4.6.1 added after that suite
# was written.  Deliberately scalar kernels are excluded: R6.1 is about kernels
# that are ALREADY vectorized.

def _load_suite():
    import runner                                    # evaluation/runner.py
    families = {'elementwise', 'axpy', 'reduction', 'dot', 'gemm',
                'convolution', 'expression'}
    out = [(n, f, s) for (n, f, s) in runner.SUITE if f in families]
    conv2d = [
        ("conv2d 3-point row", 'long long f(){vi8_t in[320],out[320];int i,j;'
         'for(i=0;i<8;i++)for(j=0;j<28;j++)out[i*32+j]=in[i*32+j]+in[i*32+j+1]'
         '+in[i*32+j+2];return out[0];}'),
        ("conv2d 3x3 stencil", 'long long f(){vi8_t in[320],out[320];int i,j;'
         'for(i=0;i<6;i++)for(j=0;j<28;j++)out[i*32+j]=in[i*32+j]+in[i*32+j+1]'
         '+in[i*32+j+2]+in[(i+1)*32+j]+in[(i+1)*32+j+1]+in[(i+1)*32+j+2];'
         'return out[0];}'),
        ("conv2d 3-point weighted", 'long long f(){vi8_t in[320],out[320];'
         'int i,j;int a=1,b=2,c=3;for(i=0;i<8;i++)for(j=0;j<28;j++)'
         'out[i*32+j]=a*in[i*32+j]+b*in[i*32+j+1]+c*in[i*32+j+2];return out[0];}'),
        ("conv2d 3-point vi16", 'long long f(){vi16_t in[320],out[320];int i,j;'
         'for(i=0;i<8;i++)for(j=0;j<28;j++)out[i*32+j]=in[i*32+j]+in[i*32+j+1]'
         '+in[i*32+j+2];return out[0];}'),
    ]
    out += [(n, 'conv2d', s) for (n, s) in conv2d]
    return out


FAMILY_ORDER = ['elementwise', 'axpy', 'reduction', 'dot', 'gemm',
                'convolution', 'conv2d', 'expression']


# ── compile one kernel through the production path ────────────────────────────
#
# `compile_c_to_mcode` does not apply one fixed optimization recipe: it walks a
# LADDER of six tiers and keeps the first that compiles without spilling, then
# runs the R3.2 superblock pass.  Measuring only tier 1 (what
# `vector_size_probe._optimize_like_production` provides) would report a
# compile failure for a kernel that production compiles perfectly well by
# stepping down a tier -- the 3x3 stencil is exactly such a kernel.  The ladder
# is therefore reproduced here from compiler.py, importing every pass from the
# same module compiler.py imports it from.  The R3.1 SWP pass is NOT applied,
# matching the frozen R4.6.5 evaluation methodology.

def _tiers(ir):
    from strength_reduce import strength_reduce
    from loopopt.pipeline import (induction_strength_reduce,
                                  loop_invariant_code_motion)
    from licm import hoist_loop_invariants
    from loop_reg import promote_loop_counters
    from copyprop import copy_propagate
    from coalesce import copy_coalesce
    from dce import dead_code_eliminate
    from sccp import sparse_conditional_constant_propagation
    from gvn import global_value_numbering
    from mem2reg import mem2reg

    def _clean(x):
        return dead_code_eliminate(copy_coalesce(copy_propagate(x)))

    def _cp(x):
        x = _clean(x)
        x = dead_code_eliminate(sparse_conditional_constant_propagation(x))
        x = global_value_numbering(x)
        x = mem2reg(x)
        x = loop_invariant_code_motion(x)
        return _clean(x)

    def _sr(x):
        return strength_reduce(x)[0]

    def _ivsr(x):
        return induction_strength_reduce(x)

    base = _sr(list(ir))
    return [
        ("IVSR+LICM+loop-reg", lambda: _cp(promote_loop_counters(
            hoist_loop_invariants(_sr(_ivsr(list(ir))))))),
        ("IVSR+loop-reg", lambda: _cp(promote_loop_counters(_sr(_ivsr(list(ir)))))),
        ("IVSR only", lambda: _cp(_sr(_ivsr(list(ir))))),
        ("LICM+loop-reg", lambda: _cp(promote_loop_counters(
            hoist_loop_invariants(list(base))))),
        ("LICM only", lambda: _cp(hoist_loop_invariants(list(base)))),
        ("loop-reg only", lambda: _cp(promote_loop_counters(list(base)))),
        ("strength-reduce only", lambda: list(base)),
    ]


def production_codegen(ir):
    """(selected IR, mcode text, tier name) -- exactly what production ships."""
    for name, build in _tiers(ir):
        try:
            instrs = build()
            cg = CodeGen(global_base=GB)
            body = cg.generate(copy.deepcopy(instrs), global_base=GB)
            if cg.spilled:
                continue
            sb = _superblock(copy.deepcopy(instrs), GB)
            cg2 = CodeGen(global_base=GB)
            body2 = cg2.generate(copy.deepcopy(sb), global_base=GB)
            if not cg2.spilled:
                return sb, body2, name + "+superblock"
            return instrs, body, name
        except Exception:
            continue
    raise RuntimeError('no tier compiled')


def build_ir(code):
    import pycparser
    from compiler import _FAKE_TYPEDEFS
    from ir import Temp
    from ir_gen import IRGenerator
    ast = pycparser.CParser().parse(_FAKE_TYPEDEFS + code)
    Temp.reset()
    g = IRGenerator(global_base=GB)
    g.visit(ast)
    return list(g.instructions)


def label_frequencies(vec_ir):
    """label -> executions per call, from the loop nest of the vectorized IR.

    freq(block) = product over enclosing loops L of trip(L), except that the
    block that is L's own header runs trip(L)+1 times (the failing exit test).
    A loop whose trip count is not statically known makes every block inside it
    UNKNOWN; those regions are excluded from the dynamic totals and reported."""
    freq, unknown = {}, set()
    for (lo, hi) in func_slices(vec_ir):
        try:
            descs = discover_function(vec_ir, lo, hi)
            annotate_induction_vars(descs)
        except Exception:
            continue
        if not descs:
            continue
        cfg = descs[0].cfg
        # block -> the loops containing it
        containing = defaultdict(list)
        for d in descs:
            for b in d.body_blocks:
                containing[b].append(d)
        for b, blk in enumerate(cfg.blocks):
            ins = vec_ir[blk.lo]
            if type(ins).__name__ != 'IRLabel':
                continue
            f, bad = 1.0, False
            for d in containing.get(b, ()):
                tc = d.trip_count
                if not tc or tc.kind != TripCount.KNOWN or tc.value is None:
                    bad = True
                    break
                f *= (tc.value + 1) if b == d.header else tc.value
            if bad:
                unknown.add(ins.name)
            else:
                freq[ins.name] = f
    return freq, unknown


class KernelReport:
    """Everything R6.1 measures about one already-vectorized kernel."""

    def __init__(self, name, family, src):
        self.name = name
        self.family = family
        self.src = src
        self.vectorized = False
        self.reason = ''
        self.occ = None                 # OccupancyReport
        self.loops = []                 # [VectorLoopGraph] on the production IR
        self.hot = None                 # the hottest vector loop graph
        self.body_block = None          # mcode block key of the hot vector body
        self.body_bundles = []          # [BundleInfo] of that block
        self.trip = None
        self.realisation = '-'          # R4.2.5: 'compact' loop or 'unrolled'
        self.tier = '-'                 # which production optimization tier won
        self.region_kind = 'none'       # 'vector' | 'memory-only'
        self.unknown_freq_labels = set()
        self.whatif = {}

    # -- convenience views -----------------------------------------------------
    @property
    def vec_bundles(self):
        return self.occ.vector_bundles() if self.occ else []

    def static(self):
        return self.occ.totals()

    def dynamic(self):
        return self.occ.totals(dynamic=True)

    def vec_static(self):
        return self.occ.totals(self.vec_bundles)

    def vec_dynamic(self):
        return self.occ.totals(self.vec_bundles, dynamic=True)

    def body(self):
        return self.occ.totals(self.body_bundles)

    def __repr__(self):
        d = self.dynamic()
        return (f"KernelReport({self.name} {self.family} "
                f"dyn_ipb={d['ipb']:.2f} occ={d['occupancy']:.1%})")


def analyze_kernel(name, family, src):
    """Compile one kernel exactly as production does and measure it."""
    r = KernelReport(name, family, src)
    ir = build_ir(src)
    vec_ir, stats, reps = vectorize_all_module(copy.deepcopy(ir))
    r.vectorized = bool(stats.vectorized)
    if not r.vectorized:
        r.reason = (reps[0].reason.split('|')[0] if reps else 'not-recognised')
        return r

    try:
        import vector_compact_loop as _vcl
        r.realisation = _vcl.realisation_of(vec_ir)
    except Exception:
        pass
    prod_ir, body_text, r.tier = production_codegen(vec_ir)
    freq, unknown = label_frequencies(vec_ir)
    r.unknown_freq_labels = unknown
    r.occ = occ.analyze_mcode(body_text, label_freq=freq)
    r.loops = dep.vector_loops(prod_ir)

    # the hot vector region: the mcode block with the most DYNAMIC instructions
    per_block = defaultdict(list)
    for b in r.occ.bundles:
        if b.in_vector_region:
            per_block[b.block].append(b)
    if per_block:
        r.region_kind = 'vector'
    else:
        # A kernel can vectorize into WIDE MEMORY MOVEMENT ONLY (a packed copy
        # loop emits no $v at all -- eight elements move per 64-bit $ld/$st).
        # There is no vector region to find, so the hot block is measured
        # instead, and the kernel is flagged so it is never counted as vector
        # issue-slot loss.
        r.region_kind = 'memory-only'
        for b in r.occ.bundles:
            per_block[b.block].append(b)
    if per_block:
        r.body_block = max(per_block,
                           key=lambda k: sum(x.frequency * x.occupied
                                             for x in per_block[k]))
        r.body_bundles = per_block[r.body_block]
        r.trip = r.body_bundles[0].frequency
    # the IR loop matching that region (same label), else the largest
    if r.loops:
        lbl = r.body_block.split(':', 1)[1] if r.body_block else None
        r.hot = next((g for g in r.loops
                      if lbl and lbl in [g.label] + _body_labels(g)), None)
        if r.hot is None:
            r.hot = max(r.loops, key=lambda g: g.n_ops)
    return r


def _body_labels(g):
    out = []
    for b in sorted(g.desc.body_blocks):
        blk = g.graph.cfg.blocks[b]
        ins = g.graph.instrs[blk.lo]
        if type(ins).__name__ == 'IRLabel':
            out.append(ins.name)
    return out


# ══ what-if experiments ═══════════════════════════════════════════════════════
#
# All of them re-run the PRODUCTION scheduler (`bundler._schedule_within_blocks`)
# and the packer on a synthesised stream, so the numbers come from the same code
# that produced the baseline -- not from a cost formula.

_ADDR_RE = re.compile(r'\[(\$r\d+)\s*\+\s*(-?\d+|\$r\d+)\]')
_REG_RE = re.compile(r'\$r(\d+)')


def _free_registers(flat, lo=None, hi=None):
    """Registers a renaming experiment may use inside the region [lo, hi].

    r0 is the hardware zero and r26/r27 are FP/SP (codegen.py), so the pool is
    r1..r25.  A register is available inside the region when it is (a) not used
    by the region itself, and (b) not live THROUGH it -- i.e. not defined before
    the region and read after it.  With lo/hi omitted the whole program is the
    region, which is the strictly conservative answer.

    The size of this list is itself a measurement: it is the ceiling on how far
    a vector loop can be unrolled before the allocator has to spill."""
    if lo is None:
        used = set()
        for ins in flat:
            used |= ins['reads'] | ins['writes']
        return [f'$r{i}' for i in range(1, 26) if f'$r{i}' not in used]
    inside = set()
    for ins in flat[lo:hi + 1]:
        inside |= ins['reads'] | ins['writes']
    defined_before, used_after = set(), set()
    for ins in flat[:lo]:
        defined_before |= ins['writes']
    for ins in flat[hi + 1:]:
        used_after |= ins['reads']
    live_through = defined_before & used_after
    blocked = inside | live_through
    return [f'$r{i}' for i in range(1, 26) if f'$r{i}' not in blocked]


_BASE_DEF = re.compile(r'^\+\s+(\$r\d+)\s+\(\$i64\)\s+(\$r26|\$r0)\s+(-?\d+)$')


def base_alias_map(flat):
    """{register -> canonical register} for base registers that provably hold the
    SAME address.

    codegen materialises `FP + k` separately for every array reference, so one
    object routinely ends up in several registers (the axpy loop holds Y in two,
    the vi8 reduction holds `a` in eight).  The bundler can only prove two memory
    accesses disjoint when they share a base register and differ in a CONSTANT
    offset (`bundler._mem_may_alias`), so duplicated bases turn provably-distinct
    accesses into may-alias pairs and serialise them.

    Only registers defined EXACTLY ONCE, by an identical `FP + constant`, are
    mapped -- otherwise the equality is not established."""
    defs, count = {}, Counter()
    for ins in flat:
        for w in ins['writes']:
            count[w] += 1
        m = _BASE_DEF.match(ins['text'].strip())
        if m:
            defs[m.group(1)] = (m.group(2), m.group(3))
    canon, by_addr = {}, {}
    for reg, addr in sorted(defs.items(), key=lambda kv: int(kv[0][2:])):
        if count[reg] != 1:
            continue
        if addr in by_addr:
            canon[reg] = by_addr[addr]
        else:
            by_addr[addr] = reg
    return canon


def _alloc(free, n):
    """n registers for a what-if, taken from the measured free pool and topped up
    with VIRTUAL names when the pool runs out.

    A virtual register is not a cheat: these experiments model transforms that
    belong BEFORE register allocation, where values are unlimited temporaries and
    the allocator runs afterwards on the transformed code.  Reporting only
    'register-limited' would hide the size of the prize; reporting only the gain
    would hide the cost.  Both are returned: `regs` and how many of them the
    current allocation cannot supply."""
    regs = list(free[:n])
    short = n - len(regs)
    regs += [f'$r{90 + i}' for i in range(short)]
    return regs, short


def _classify_block_regs(entries):
    """(defined, carried, read_only, iv) for one loop-body block.

    `carried` = a register whose FIRST appearance in the block is a READ and
    which the block also writes: an accumulator or an induction variable, i.e.
    a loop-carried value.  `iv` = the carried register updated by `+ rX rX imm`
    (the induction variable), which addressing depends on."""
    defined, first_read, carried, iv = set(), set(), set(), None
    seen = set()
    for ins in entries:
        for rgs in (ins['reads'] - seen):
            first_read.add(rgs)
        seen |= ins['reads'] | ins['writes']
        defined |= ins['writes']
    carried = {r for r in defined if r in first_read}
    for ins in entries:
        m = re.match(r'\+\s+(\$r\d+)\s+\(\$\w+\)\s+(\$r\d+)\s+(-?\d+)$',
                     ins['text'].strip())
        if m and m.group(1) == m.group(2) and m.group(1) in carried:
            iv = m.group(1)
    read_only = set()
    for ins in entries:
        read_only |= (ins['reads'] - defined)
    return defined, carried, read_only, iv


def _entry(text, labels=()):
    w, rd, ctrl, ma, mw = _b._parse_deps(text)
    return {'text': text, 'labels': list(labels), 'writes': w, 'reads': rd,
            'is_ctrl': ctrl, 'mem_access': ma, 'mem_write': mw}


def _pack_block(entries):
    """Schedule + pack a synthesised block with the production code path."""
    sched = _b._schedule_within_blocks([dict(e) for e in entries])
    sched = occ.refine_vector_classes(sched)
    return occ.pack_with_attribution(sched)


_ACCESS = re.compile(r'\[(\$r\d+)\s*\+\s*(-?\d+)\]')


def _perfect_alias_rewrite(entries):
    """Model a memory disambiguator that KNOWS which accesses hit distinct
    objects, without touching the bundler.

    `bundler._mem_may_alias` can only prove two accesses disjoint when they use
    the SAME base register with different CONSTANT offsets; two different base
    registers prove nothing (deliberately -- two registers can hold the same
    address).  Distinct arrays therefore always may-alias at the mcode level,
    even though the R2.2 IR-level `MemoryDisambiguator` proves them distinct
    routinely.

    The model rewrites every base register to ONE shared base with a
    per-register offset block, so accesses through different bases become
    provably disjoint while accesses through the SAME base at the SAME offset
    (a genuine load/store pair on one element) stay in conflict.  Addresses
    become fictional; only the dependence structure -- which is what the packer
    reasons about -- is preserved."""
    bases = []
    for e in entries:
        for m in _ACCESS.finditer(e['text']):
            if m.group(1) not in bases:
                bases.append(m.group(1))
    block = {b: 1000 * (i + 1) for i, b in enumerate(bases)}
    out = []
    for e in entries:
        t = _ACCESS.sub(lambda m: f"[$r80 + {block[m.group(1)] + int(m.group(2))}]",
                        e['text'])
        out.append(_entry(t, e['labels']))
    return out


def whatif_unroll(report, u, rename_carried=False, addressing='constant',
                  dedup_bases=False, disambiguate=False):
    """Unroll the hot vector loop body u times and MEASURE the packed result.

    addressing:
      'constant' -- copy j addresses `[base + 8*j]`, i.e. the pointer-per-array
                    (IVSR) form.  Constant offsets off a shared base are the ONLY
                    form the bundler can prove disjoint (`_mem_may_alias`), so
                    this is the form an unroller must emit to pack memory ops.
      'indexed'  -- copy j addresses `[base + iv_j]` with a fresh index register
                    (iv_j = iv + 8j, one extra ALU op per copy): the shape that
                    falls out of naively cloning the existing address code.

    rename_carried=True gives each copy its own loop-carried register (multiple
    accumulators) -- legal only for an associative reduction.

    Returns a dict with the measured bundles for u iterations, bundles per
    iteration, and the register cost; `ok=False` with a reason when the free
    register pool cannot cover the renaming (itself a measured limit)."""
    ents = [report.occ.flat[i] for bnd in report.body_bundles
            for i in bnd.flat_idx]
    if not ents:
        return {'ok': False, 'reason': 'no-vector-body'}
    base = len(report.body_bundles)
    n_dedup = 0
    if dedup_bases:
        canon = base_alias_map(report.occ.flat)
        if canon:
            new = []
            for e in ents:
                t = e['text']
                for a, c in canon.items():
                    t2 = re.sub(r'\[' + re.escape(a) + r'\b', '[' + c, t)
                    if t2 != t:
                        n_dedup += 1
                    t = t2
                new.append(_entry(t, e['labels']))
            ents = new
    defined, carried, _ro, iv = _classify_block_regs(ents)
    rename_set = defined - carried
    if rename_carried:
        rename_set |= (carried - ({iv} if iv else set()))
    need = len(rename_set) * (u - 1) + (0 if addressing == 'constant'
                                        else (u - 1))
    idxs = [i for bnd in report.body_bundles for i in bnd.flat_idx]
    free = _free_registers(report.occ.flat, min(idxs), max(idxs))
    alloc, short = _alloc(free, need)

    ctrl = [e for e in ents if e['is_ctrl']]
    iv_upd = [e for e in ents
              if iv and e['writes'] == frozenset({iv}) and iv in e['reads']]
    core = [e for e in ents if e not in ctrl and e not in iv_upd]

    out, pool = [], list(alloc)
    for j in range(u):
        rmap = {}
        if j:
            for r in sorted(rename_set):
                rmap[r] = pool.pop(0)
        idx_reg = None
        if j and addressing == 'indexed' and iv:
            idx_reg = pool.pop(0)
            out.append(_entry(f"+ {idx_reg} ($i64) {iv} {8 * j}"))
        for e in core:
            t = e['text']
            for a, bnew in rmap.items():
                t = re.sub(re.escape(a) + r'\b', bnew, t)

            def _fix(m, j=j, idx_reg=idx_reg):
                bse, off = m.group(1), m.group(2)
                if not off.startswith('$r'):
                    return m.group(0)
                if addressing == 'constant':
                    return f"[{bse} + {8 * j}]"
                return f"[{bse} + {idx_reg or off}]"
            t = _ADDR_RE.sub(_fix, t)
            out.append(_entry(t))
    for e in iv_upd:                       # one IV update, stepping u chunks
        m = re.match(r'(\+\s+\$r\d+\s+\(\$\w+\)\s+\$r\d+\s+)(-?\d+)$',
                     e['text'].strip())
        out.append(_entry(f"{m.group(1)}{int(m.group(2)) * u}" if m
                          else e['text']))
    out.extend(_entry(e['text']) for e in ctrl)

    if disambiguate:
        out = _perfect_alias_rewrite(out)
    bundles = _pack_block(out)
    n = len(bundles)
    return {'ok': True, 'u': u, 'addressing': addressing,
            'disambiguate': disambiguate,
            'rename_carried': rename_carried, 'dedup_bases': dedup_bases,
            'rewritten_accesses': n_dedup,
            'bundles_base': base, 'bundles_unrolled': n,
            'bundles_per_iter': n / u, 'speedup': base / (n / u) if n else 0.0,
            'registers_needed': need, 'registers_free': len(free),
            'registers_short': short,
            'instrs': sum(b.occupied for b in bundles),
            'occupancy': sum(b.occupied for b in bundles) / (n * lat.ISSUE_WIDTH)
            if n else 0.0,
            'causes': Counter({c: v for c, v in
                               Counter(b.cause for b in bundles
                                       if b.empty).items()})}


def _forces_separate_bundle(a, b):
    """True when a and b can never share a bundle, whatever the order.

    Exactly the packer's hazard set minus the ones that are order artifacts:
      RAW / WAW      a value written and then read (or written twice)
      may-alias      a store and an access that cannot be proved disjoint
      memory phase   a non-memory write of a register a memory op addresses with
      control        an instruction after a control transfer
    WAR is excluded: a VLIW bundle reads every operand before any write, and the
    packer knowingly allows it."""
    if a['writes'] & b['reads']:
        return True
    if a['writes'] & b['writes']:
        return True
    if a['mem_write'] is not None and b['mem_access'] is not None \
            and _b._mem_may_alias(b['mem_access'], (a['mem_write'],)):
        return True
    if b['mem_write'] is not None and a['mem_access'] is not None \
            and _b._mem_may_alias(a['mem_access'], (b['mem_write'],)):
        return True
    if a['mem_access'] is not None and b['mem_access'] is None \
            and (b['writes'] & a['reads']):
        return True
    if a['is_ctrl']:
        return True
    return False


def pack_lower_bound(entries):
    """Fewest bundles this block can occupy under ANY local schedule.

    Three independent bounds, the largest wins:
      width       ceil(N / 8)                       -- issue width
      memory      ceil(memory ops / 4)              -- load/store lanes
      dependence  longest chain of pairs that can never share a bundle
                  (`_forces_separate_bundle`), counted in nodes

    Sound because each bound holds for every legal schedule of the block, and
    they are computed on hazards that are properties of the DATAFLOW, not of the
    current order.  If it equals the shipped bundle count, no local scheduler --
    latency-aware, bundle-aware or otherwise -- can do better."""
    m = len(entries)
    if m == 0:
        return 0
    n_mem = sum(1 for e in entries if e['mem_access'] is not None)
    width = -(-m // lat.ISSUE_WIDTH)
    mem = -(-n_mem // lat.MEM_LANES)
    chain = [1] * m
    for j in range(m):
        for i in range(j):
            if _forces_separate_bundle(entries[i], entries[j]):
                chain[j] = max(chain[j], chain[i] + 1)
    return max(width, mem, max(chain))


_ACC_DOT = re.compile(r'^\$dot\s+\$accumulate\s+(\$r\d+)\s+\((\$\w+)\)\s+(\$r\d+)\s+(\$r\d+)$')
_ACC_V = re.compile(r'^\$v\s+(\S+)\s+(\$r\d+)\s+\((\$\w+)\)\s+(\$r\d+)\s+(\$r\d+)$')
_ACC_ALU = re.compile(r'^([+*|&^])\s+(\$r\d+)\s+\((\$\w+)\)\s+(\$r\d+)\s+(\$r\d+)$')


def _accumulator_chain(entries):
    """The longest chain of instructions that accumulate into ONE register.

    Returns (register, [positions], combine_template) or None.  An accumulate is
    an instruction whose destination is also one of its sources -- `$dot
    $accumulate rA`, `$v + rA (t) rA rB`, or the scalar `+ rA (t) rA rB` -- i.e.
    exactly the reduction recurrence R4.1 emits.  Only associative operations
    are accepted, because splitting the chain reassociates it."""
    chains = defaultdict(list)
    kind = {}
    for i, e in enumerate(entries):
        t = e['text'].strip()
        m = _ACC_DOT.match(t)
        if m:
            chains[m.group(1)].append(i)
            kind[m.group(1)] = ('dot', m.group(2))
            continue
        m = _ACC_V.match(t)
        if m and m.group(1) in ('+', '*', '|', '&', '^') and \
                m.group(2) in (m.group(4), m.group(5)):
            chains[m.group(2)].append(i)
            kind[m.group(2)] = ('v' + m.group(1), m.group(3))
            continue
        m = _ACC_ALU.match(t)
        if m and m.group(2) in (m.group(4), m.group(5)):
            chains[m.group(2)].append(i)
            kind[m.group(2)] = ('alu' + m.group(1), m.group(3))
    if not chains:
        return None
    reg = max(chains, key=lambda r: len(chains[r]))
    if len(chains[reg]) < 2:
        return None
    return reg, chains[reg], kind[reg]


def whatif_multi_accumulator(report, k):
    """Split the hot block's reduction chain into k independent accumulators and
    MEASURE the repacked result.

    Legal only for an associative reduction (the chain detector accepts only
    associative operations); this is the classic reassociation transform.  The
    k-1 partial sums are combined at the end of the block, so the instruction
    count grows by k-1 -- both effects are in the measured numbers."""
    ents = [report.occ.flat[i] for bnd in report.body_bundles
            for i in bnd.flat_idx]
    if not ents:
        return {'ok': False, 'reason': 'no-body'}
    base = len(report.body_bundles)
    ch = _accumulator_chain(ents)
    if ch is None:
        return {'ok': False, 'reason': 'no-accumulator-chain', 'bundles_base': base}
    reg, positions, (op, ty) = ch
    if len(positions) < k:
        return {'ok': False, 'reason': f'chain-shorter-than-{k}',
                'chain_len': len(positions), 'bundles_base': base}
    idxs = [i for bnd in report.body_bundles for i in bnd.flat_idx]
    free = _free_registers(report.occ.flat, min(idxs), max(idxs))
    extra, short = _alloc(free, k - 1)
    accs = [reg] + extra

    out, started = [], {reg}
    pos_of = {p: j for j, p in enumerate(positions)}
    for i, e in enumerate(ents):
        if i not in pos_of:
            out.append(_entry(e['text'], e['labels']))
            continue
        a = accs[pos_of[i] % k]
        t = e['text'].strip()
        if a == reg:
            out.append(_entry(t, e['labels']))
            continue
        if a not in started:                # first op of a fresh accumulator
            started.add(a)
            if op == 'dot':
                t = t.replace('$accumulate ', '').replace(reg, a, 1)
            else:
                srcs = [x for x in _REG_RE.findall(t)]
                other = next((f'$r{x}' for x in srcs if f'$r{x}' != reg), reg)
                t = f"+ {a} ($i64) {other} 0"
        else:
            t = t.replace(reg, a)
        out.append(_entry(t, e['labels']))
    # combine the partial accumulators back into the original register
    tail = []
    for a in accs[1:]:
        if op == 'dot' or op.startswith('alu'):
            tail.append(_entry(f"+ {reg} ($i64) {reg} {a}"))
        else:
            tail.append(_entry(f"$v {op[1:]} {reg} ({ty}) {reg} {a}"))
    ctrl = [e for e in out if e['is_ctrl']]
    core = [e for e in out if not e['is_ctrl']]
    bundles = _pack_block(core + tail + ctrl)
    n = len(bundles)
    return {'ok': True, 'k': k, 'accumulator': reg, 'chain_len': len(positions),
            'bundles_base': base, 'bundles_new': n, 'speedup': base / n if n else 0.0,
            'registers_needed': k - 1, 'registers_free': len(free),
            'registers_short': short,
            'instrs': sum(b.occupied for b in bundles),
            'occupancy': sum(b.occupied for b in bundles) / (n * lat.ISSUE_WIDTH)
            if n else 0.0}


_BIN = re.compile(r'^([+*|&^])\s+(\$r\d+)\s+\((\$\w+)\)\s+(\$r\d+)\s+(\$r\d+)$')
_VBIN = re.compile(r'^\$v\s+([+*|&^])\s+(\$r\d+)\s+\((\$\w+)\)\s+(\$r\d+)\s+(\$r\d+)$')


def _parse_assoc(text):
    """(op, dest, type, src1, src2, is_vector) for an associative binary op."""
    t = text.strip()
    m = _BIN.match(t)
    if m:
        return (m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), False)
    m = _VBIN.match(t)
    if m:
        return (m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), True)
    return None


def reduction_chain(entries):
    """The longest LINEAR chain of associative ops, each consuming the previous
    one's result.

    After mem2reg the reduction R4.1 emits is in SSA form -- every partial sum
    lands in a NEW register -- so the recurrence is not an `rA = rA op x`
    accumulate but a chain `r1 = a op b; r2 = r1 op c; r3 = r2 op d; ...`.  It is
    still one serial dependence of length n, and it is still associative, so it
    can be re-shaped into a balanced tree of depth ceil(log2(n+1)).

    Returns (positions, op, type, is_vector) or None."""
    parsed = {i: _parse_assoc(e['text']) for i, e in enumerate(entries)}
    parsed = {i: p for i, p in parsed.items() if p}
    if not parsed:
        return None
    best = None
    for i in parsed:
        op, dest, ty, s1, s2, isv = parsed[i]
        chain = [i]
        cur = dest
        while True:
            nxt = None
            for j in sorted(parsed):
                if j <= chain[-1]:
                    continue
                o2, d2, t2, a2, b2, v2 = parsed[j]
                if o2 == op and t2 == ty and v2 == isv and cur in (a2, b2):
                    nxt = j
                    break
            if nxt is None:
                break
            chain.append(nxt)
            cur = parsed[nxt][1]
        if best is None or len(chain) > len(best[0]):
            best = (chain, op, ty, isv)
    if best is None or len(best[0]) < 3:
        return None
    return best


def whatif_reassociate(report):
    """Re-shape the hot block's serial reduction chain into a balanced tree and
    MEASURE the repacked result.

    Uses the SAME instruction count and the SAME destination registers as the
    original chain (the intermediate results simply move to different tree
    nodes), so it needs no extra registers -- only reassociation, which is legal
    for the associative operators the chain detector accepts.  Depth drops from
    n to ceil(log2(n+1))."""
    ents = [report.occ.flat[i] for bnd in report.body_bundles
            for i in bnd.flat_idx]
    if not ents:
        return {'ok': False, 'reason': 'no-body'}
    base = len(report.body_bundles)
    ch = reduction_chain(ents)
    if ch is None:
        return {'ok': False, 'reason': 'no-reduction-chain', 'bundles_base': base}
    positions, op, ty, isv = ch
    dests = [_parse_assoc(ents[i]['text'])[1] for i in positions]

    # Leaf operands: both sources of the first op, then the "other" source of
    # every later op (the one that is not the previous partial result).  A leaf
    # is identified by (register, defining instruction) rather than by register
    # alone: the register ALLOCATOR reuses names, so the same register can hold
    # two different leaf values in one block.  Reassociation reorders the reads,
    # so every such reused name must be renamed first -- which is exactly why
    # this transform belongs before register allocation.
    def _def_before(reg, pos):
        for i in range(pos - 1, -1, -1):
            if reg in ents[i]['writes']:
                return i
        return -1

    leaves = []
    p0 = _parse_assoc(ents[positions[0]]['text'])
    leaves.append((p0[3], _def_before(p0[3], positions[0])))
    leaves.append((p0[4], _def_before(p0[4], positions[0])))
    prev = p0[1]
    for i in positions[1:]:
        pk = _parse_assoc(ents[i]['text'])
        src = pk[4] if pk[3] == prev else pk[3]
        leaves.append((src, _def_before(src, i)))
        prev = pk[1]

    idxs = [i for bnd in report.body_bundles for i in bnd.flat_idx]
    free = _free_registers(report.occ.flat, min(idxs), max(idxs))
    n_free_at_start = len(free)
    rename_def = {}                       # def index -> fresh register
    by_reg = defaultdict(set)
    for (reg, d) in leaves:
        by_reg[reg].add(d)
    for reg, ds in by_reg.items():
        if len(ds) < 2:
            continue
        for d in sorted(ds)[1:]:          # keep the first definition's name
            if d < 0:
                return {'ok': False, 'reason': 'leaf-defined-outside-block',
                        'bundles_base': base}
            for j in range(d + 1, len(ents)):
                if j in positions:
                    continue
                if reg in ents[j]['reads']:
                    return {'ok': False, 'reason': 'reused-leaf-has-other-reader',
                            'bundles_base': base}
                if reg in ents[j]['writes']:
                    break
            rename_def[d] = (free.pop(0) if free
                             else f'$r{90 + len(rename_def)}')
    leaf_regs = [rename_def.get(d, reg) for (reg, d) in leaves]

    def emit(dest, a, b):
        return (f"$v {op} {dest} ({ty}) {a} {b}" if isv
                else f"{op} {dest} ({ty}) {a} {b}")

    pool = list(dests[:-1])
    final = dests[-1]
    level, tree, depth = list(leaf_regs), [], 0
    while len(level) > 1:
        depth += 1
        nxt = []
        for k in range(0, len(level) - 1, 2):
            d = final if len(level) == 2 else pool.pop(0)
            tree.append(emit(d, level[k], level[k + 1]))
            nxt.append(d)
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    if len(tree) != len(positions):
        return {'ok': False, 'reason': 'chain-rebuild-mismatch',
                'bundles_base': base}

    pos_set = set(positions)
    out = []
    for i, e in enumerate(ents):
        if i in pos_set:
            if i == positions[-1]:
                out.extend(_entry(t) for t in tree)
            continue
        text = e['text']
        if i in rename_def:               # this definition feeds a renamed leaf
            m = _parse_assoc(text)
            old = m[1] if m else None
            if old is None:               # e.g. a $vreduce / $ld / $dot dest
                text = re.sub(r'(\$\w+\s+(?:\S+\s+)?)' + re.escape(
                    sorted(ents[i]['writes'])[0]) + r'\b',
                    lambda mm: mm.group(1) + rename_def[i], text, count=1)
            else:
                text = text.replace(old, rename_def[i], 1)
        out.append(_entry(text, e['labels']))
    bundles = _pack_block(out)
    n = len(bundles)
    return {'ok': True, 'chain_len': len(positions), 'op': op,
            'tree_depth': depth, 'bundles_base': base, 'bundles_new': n,
            'speedup': base / n if n else 0.0,
            'renamed_leaves': len(rename_def),
            'registers_needed': len(rename_def),
            'registers_free': n_free_at_start,
            'registers_short': max(0, len(rename_def) - n_free_at_start),
            'instrs': sum(b.occupied for b in bundles),
            'occupancy': sum(b.occupied for b in bundles) / (n * lat.ISSUE_WIDTH)
            if n else 0.0}


def whatif_local_schedule(report):
    """Measured headroom for a better LOCAL scheduler on the hot vector body."""
    ents = [report.occ.flat[i] for bnd in report.body_bundles
            for i in bnd.flat_idx]
    if not ents:
        return {'ok': False}
    base = len(report.body_bundles)
    best = pack_lower_bound(ents)
    return {'ok': True, 'bundles_base': base, 'bundles_bound': best,
            'gain': max(0, base - best)}


def whatif_pipelining(report):
    """MII lower bound on bundles per iteration from the measured graph.

    RecMII is computed over TRUE loop-carried dependences only (RAW/MEM_RAW):
    carried WAR/WAW edges are name reuse, which modulo scheduling removes by
    renaming across stages, so counting them would overstate the recurrence."""
    g = report.hot
    if g is None:
        return {'ok': False}
    true_carried = [e for e in g.carried if e[3] in dep.TRUE_KINDS]
    true_intra = [e for e in g.intra if e[3] in dep.TRUE_KINDS]
    rec = dep._longest_recurrence(g.ops, true_intra, true_carried)
    # register-only recurrence: the bound that remains once a memory
    # disambiguator proves the store/load pair of consecutive chunks disjoint
    # (they differ by one vector width -- see the R2.2 same-base SIV rule).
    reg_carried = [e for e in true_carried if e[3] == dep.RAW]
    rec_reg = dep._longest_recurrence(g.ops, true_intra, reg_carried)
    mii = max(g.res_mii, max(1, rec))
    mii_reg = max(g.res_mii, max(1, rec_reg))
    base = len(report.body_bundles)
    return {'ok': True, 'res_mii': g.res_mii, 'rec_mii_true': max(1, rec),
            'rec_mii_all': g.rec_mii, 'rec_mii_reg': max(1, rec_reg),
            'mii': mii, 'mii_reg': mii_reg, 'bundles_base': base,
            'gain': max(0, base - mii), 'gain_reg': max(0, base - mii_reg)}


# ── module-level aggregation ──────────────────────────────────────────────────

def analyze_suite(kernels=None):
    kernels = kernels if kernels is not None else _load_suite()
    reports = []
    for (name, family, src) in kernels:
        try:
            r = analyze_kernel(name, family, src)
        except Exception as e:                       # never let one kernel stop the run
            r = KernelReport(name, family, src)
            r.reason = f'analysis-error: {type(e).__name__}: {e}'
        if r.vectorized and r.body_bundles:
            r.whatif = {
                'unroll2': whatif_unroll(r, 2),
                'unroll4': whatif_unroll(r, 4),
                'unroll2_indexed': whatif_unroll(r, 2, addressing='indexed'),
                'unroll1_model': whatif_unroll(r, 1),
                'unroll1_disamb': whatif_unroll(r, 1, dedup_bases=True,
                                                disambiguate=True),
                'unroll2_disamb': whatif_unroll(r, 2, dedup_bases=True,
                                                disambiguate=True),
                'unroll4_disamb': whatif_unroll(r, 4, dedup_bases=True,
                                                disambiguate=True),
                'unroll8_disamb': whatif_unroll(r, 8, dedup_bases=True,
                                                disambiguate=True),
                'unroll4_disamb_acc': whatif_unroll(r, 4, dedup_bases=True,
                                                    disambiguate=True,
                                                    rename_carried=True),
                'unroll8_disamb_acc': whatif_unroll(r, 8, dedup_bases=True,
                                                    disambiguate=True,
                                                    rename_carried=True),
                'unroll2_acc': whatif_unroll(r, 2, rename_carried=True),
                'unroll4_acc': whatif_unroll(r, 4, rename_carried=True),
                'accum2': whatif_multi_accumulator(r, 2),
                'accum4': whatif_multi_accumulator(r, 4),
                'reassociate': whatif_reassociate(r),
                'local_sched': whatif_local_schedule(r),
                'pipelining': whatif_pipelining(r),
            }
        reports.append(r)
    return reports


# ══ projection: what a measured body-bundle count means for the whole program ══

def project_dynamic(report, bundles_per_iter, instrs_per_iter):
    """Whole-program dynamic IPB if the hot vector body ran in
    `bundles_per_iter` bundles instead of the shipped count.

    Only the hot region's contribution changes; every other bundle keeps its
    measured frequency.  ASSUMPTION (stated, not hidden): the trip count divides
    the unroll factor, i.e. the remainder is handled by the peeling framework
    R4.4.5 already provides, at no extra dynamic cost."""
    d = report.dynamic()
    bd = report.occ.totals(report.body_bundles, dynamic=True)
    freq = report.trip or 1.0
    new_bundles = d['bundles'] - bd['bundles'] + freq * bundles_per_iter
    new_instrs = d['instructions'] - bd['instructions'] + freq * instrs_per_iter
    if new_bundles <= 0:
        return None
    return {'bundles': new_bundles, 'instructions': new_instrs,
            'ipb': new_instrs / new_bundles,
            'occupancy': new_instrs / (new_bundles * lat.ISSUE_WIDTH),
            'bundle_reduction': 1.0 - new_bundles / d['bundles']}


def _from_unroll(report, w):
    if not w or not w.get('ok'):
        return None
    return project_dynamic(report, w['bundles_per_iter'], w['instrs'] / w['u'])


def _from_block(report, w):
    if not w or not w.get('ok'):
        return None
    return project_dynamic(report, w['bundles_new'], w['instrs'])


# ── opportunity ranking ───────────────────────────────────────────────────────

class Opportunity:
    """One candidate optimization, with the evidence behind its estimate."""

    def __init__(self, name, difficulty, evidence):
        self.name = name
        self.difficulty = difficulty
        self.evidence = evidence
        self.per_kernel = {}          # kernel name -> (base_ipb, projected_ipb)
        self.applies = 0
        self.blocked = {}             # kernel -> why it does not apply

    def add(self, r, projected):
        base = r.dynamic()['ipb']
        if projected is None:
            return
        self.per_kernel[r.name] = (base, projected['ipb'],
                                   projected['bundle_reduction'])
        self.applies += 1

    @property
    def mean_gain(self):
        """Mean relative dynamic-IPB gain over the kernels it APPLIES to."""
        if not self.per_kernel:
            return 0.0
        return sum(p / b - 1.0 for (b, p, _x) in self.per_kernel.values()) \
            / len(self.per_kernel)

    @property
    def suite_gain(self):
        """Mean relative gain over the WHOLE suite (non-applicable kernels
        contribute 0) -- the honest ranking key."""
        if not self._suite_n:
            return 0.0
        return sum(p / b - 1.0 for (b, p, _x) in self.per_kernel.values()) \
            / self._suite_n

    @property
    def mean_bundle_reduction(self):
        if not self.per_kernel:
            return 0.0
        return sum(x for (_b, _p, x) in self.per_kernel.values()) / len(self.per_kernel)


def rank_opportunities(reports):
    """Rank every candidate optimization by MEASURED projected dynamic IPB."""
    vec = [r for r in reports if r.vectorized and r.body_bundles]
    opps = []

    def mk(name, diff, ev, picker):
        o = Opportunity(name, diff, ev)
        o._suite_n = len(vec)
        for r in vec:
            o.add(r, picker(r))
        opps.append(o)
        return o

    mk('Vector loop unrolling + memory disambiguation (u=4)', 'high',
       'unroll the vector body 4x with renaming, plus a disambiguator that '
       'separates distinct arrays; measured by repacking with the production '
       'bundler',
       lambda r: _from_unroll(r, r.whatif.get('unroll4_disamb_acc')
                              if _better(r.whatif.get('unroll4_disamb_acc'),
                                         r.whatif.get('unroll4_disamb'))
                              else r.whatif.get('unroll4_disamb')))
    mk('Vector loop unrolling + disambiguation (u=8)', 'high',
       'same, unroll factor 8 -- shows where the gain saturates and what it '
       'costs in registers',
       lambda r: _from_unroll(r, r.whatif.get('unroll8_disamb_acc')
                              if _better(r.whatif.get('unroll8_disamb_acc'),
                                         r.whatif.get('unroll8_disamb'))
                              else r.whatif.get('unroll8_disamb')))
    mk('Vector loop unrolling alone (u=4, no disambiguation)', 'medium',
       'unroll only: the may-alias rule then serialises every store against '
       'the next copy\'s loads',
       lambda r: _from_unroll(r, r.whatif.get('unroll4')))
    mk('Multiple accumulators (u=4, reduction kernels)', 'medium',
       'unroll with the loop-carried accumulator renamed per copy -- legal for '
       'an associative reduction',
       lambda r: _from_unroll(r, r.whatif.get('unroll4_acc')))
    mk('Memory disambiguation alone (no unrolling)', 'medium',
       'distinct-object information at the bundle packer, body unchanged',
       lambda r: _from_unroll(r, r.whatif.get('unroll1_disamb')))
    mk('Reduction-tree reassociation', 'medium',
       'the serial chain of partial sums re-shaped into a balanced tree of '
       'depth ceil(log2 n)',
       lambda r: _from_block(r, r.whatif.get('reassociate')))
    mk('Vector software pipelining (MII bound)', 'high',
       'the recurrence/resource lower bound on bundles per iteration from the '
       'measured dependence graph -- a CEILING, not a measured schedule',
       lambda r: (project_dynamic(r, r.whatif['pipelining']['mii_reg'],
                                  r.body()['instructions'])
                  if r.whatif.get('pipelining', {}).get('ok') else None))
    mk('Latency-aware / bundle-aware local scheduling', 'low',
       'the fewest bundles ANY local reordering of the shipped block can reach',
       lambda r: (project_dynamic(r, r.whatif['local_sched']['bundles_bound'],
                                  r.body()['instructions'])
                  if r.whatif.get('local_sched', {}).get('ok') else None))

    opps.sort(key=lambda o: -o.suite_gain)
    return opps


def _better(a, b):
    if not (a and a.get('ok')):
        return False
    if not (b and b.get('ok')):
        return True
    return a['bundles_per_iter'] < b['bundles_per_iter']


# ══ report generation ═════════════════════════════════════════════════════════

def _bar(n, total, width=40):
    if not total:
        return ''
    return '#' * max(0, int(round(width * n / total)))


def _tbl(headers, rows):
    out = ['| ' + ' | '.join(headers) + ' |',
           '|' + '|'.join('---' for _ in headers) + '|']
    for r in rows:
        out.append('| ' + ' | '.join(str(c) for c in r) + ' |')
    return '\n'.join(out)


def _pct(x):
    return f"{100.0 * x:.1f}%"


def _family_groups(reports):
    g = defaultdict(list)
    for r in reports:
        g[r.family].append(r)
    return [(f, g[f]) for f in FAMILY_ORDER if f in g]


def _hist_block(hist, total, label='occupied'):
    lines = []
    for k in range(0, lat.ISSUE_WIDTH + 1):
        n = hist.get(k, 0)
        if not n and k == 0:
            continue
        lines.append(f"  {k} {label:9s} {n:8.0f}  {_pct(n / total) if total else '':>6}  "
                     f"{_bar(n, total)}")
    return '\n'.join(lines)


def format_report(reports, opps):
    vec = [r for r in reports if r.vectorized and r.body_bundles]
    L = []
    A = L.append

    # ── header ────────────────────────────────────────────────────────────────
    A("# R6.1 — Vector Backend ILP Analysis")
    A("")
    A("**Milestone R6.1 — measurement only.** No optimization was performed. "
      "No scheduler, bundler, code generator, vectorizer, IR, legality or "
      "profitability file was modified. Everything below is measured on the "
      "code the frozen compiler actually emits.")
    A("")
    A(f"* kernels analysed: **{len(reports)}** ({len(vec)} with a measurable "
      f"vector region) across **{len(_family_groups(reports))}** families")
    A(f"* bundle reconstruction verified against the production bundler on "
      f"**{sum(1 for r in reports if r.occ and r.occ.verified)}/"
      f"{sum(1 for r in reports if r.occ)}** programs (identical bundles)")
    A(f"* empty issue slots classified: **100%** "
      f"(every slot inherits the single reason its bundle closed)")
    A("")

    # ── headline ──────────────────────────────────────────────────────────────
    tot_slots = sum(r.dynamic()['issue_slots'] for r in vec)
    tot_instr = sum(r.dynamic()['instructions'] for r in vec)
    body_slots = sum(r.occ.totals(r.body_bundles, dynamic=True)['issue_slots']
                     for r in vec)
    body_instr = sum(r.occ.totals(r.body_bundles, dynamic=True)['instructions']
                     for r in vec)
    A("## 0. Answer first")
    A("")
    A(f"Across the vectorized suite the shipped code issues "
      f"**{tot_instr / tot_slots * 100:.1f}%** of its dynamic issue slots "
      f"({tot_instr:.0f} instructions in {tot_slots:.0f} slots); inside the hot "
      f"vector loop bodies it issues **{body_instr / body_slots * 100:.1f}%**.")
    A("")
    real = next((o for o in opps if 'MII' not in o.name), opps[0])
    A("The single highest-impact compiler optimization, by measurement, is "
      "**vector loop unrolling combined with memory disambiguation** — "
      "unrolling the vector loop body and giving the bundle packer the "
      "distinct-object information the IR-level analysis already has. Neither "
      "half is worth much alone; together they are worth "
      f"**{real.suite_gain:+.1%}** dynamic IPB across the whole suite "
      f"({real.name.split('(')[-1].rstrip(')')}), every number obtained by "
      "repacking the synthesised code with the production bundler.")
    A("")
    A("The measurement that decides it: unrolling ALONE is worth only "
      + _u_summary(vec, 'unroll4') +
      ", because the bundler can prove two memory accesses disjoint only when "
      "they share a base register and differ in a constant offset — so every "
      "store serialises against the next copy's loads. With that one rule "
      "informed by distinct-object information, the same unroll is worth "
      + _u_summary(vec, 'unroll4_disamb') + ".")
    A("")

    # ── method ────────────────────────────────────────────────────────────────
    A("## 1. Method, and what is fact vs model")
    A("")
    A("```")
    A("Scalar IR -> Vectorizer -> Vector IR -> [ R6.2+ optimizer ] -> Scheduler"
      " -> Bundler -> Codegen")
    A("                             ^                                        ^")
    A("                             |                                        |")
    A("             dependency_graph.py measures                occupancy.py measures")
    A("             the ILP that EXISTS                         the ILP that is DELIVERED")
    A("```")
    A("")
    A("Each kernel is compiled through the real production path: the "
      "vectorizer, then the six-tier scalar optimizer ladder from "
      "`compiler.py` (first tier that compiles spill-free wins), then the R3.2 "
      "superblock pass, then codegen, then the scheduler and bundler. The "
      "winning tier is reported per kernel.")
    A("")
    A("**FACT** (structural properties of the emitted code): 8 issue slots per "
      "bundle, 4 memory lanes, 1 divide/sqrt lane, 28 allocatable registers "
      "(`vector_capability_db`); every bundle's occupancy, instruction mix and "
      "closing reason; register lifetimes; execution frequencies from proved "
      "trip counts.")
    A("")
    A("**MODEL** (relative weights, never a cycle count): per-instruction "
      "latency. APARA publishes no instruction timings and no cycle-accurate "
      "run has ever been made on this project, so `latency.py` reuses the "
      "frozen R2.4 weights rather than inventing new ones. Critical paths and "
      "MII bounds are therefore relative rankings, not cycles.")
    A("")
    A("**Dynamic weighting.** R6 is graded on dynamic IPB, so every bundle "
      "carries an execution frequency: the product of the enclosing loops' "
      "proved trip counts (the header runs trip+1 times). Trip counts are read "
      "from the pre-optimizer vectorized IR, where the induction variable is "
      "still memory-backed and `analysis_iv` can prove them, and attached to "
      "the mcode by label.")
    A("")

    # ── per-kernel ────────────────────────────────────────────────────────────
    A("## 2. Per-kernel reports")
    A("")
    rows = []
    for r in reports:
        if not r.vectorized:
            rows.append([r.name, r.family, 'NOT VECTORIZED', r.reason,
                         '', '', '', '', '', ''])
            continue
        d, sT = r.dynamic(), r.static()
        b = r.occ.totals(r.body_bundles, dynamic=True) if r.body_bundles else None
        g = r.hot
        rows.append([
            r.name + ('  *(no $v emitted)*' if r.region_kind == 'memory-only'
                      else ''),
            r.family, r.realisation,
            f"{sT['bundles']:.0f}", f"{sT['ipb']:.2f}",
            f"{d['bundles']:.0f}", f"{d['ipb']:.2f}", _pct(d['occupancy']),
            f"{b['bundles'] / d['bundles'] * 100:.0f}%" if b else '-',
            f"{g.crit_path_true}" if g else 'n/a (no loop)',
        ])
    A(_tbl(['kernel', 'family', 'realisation', 'static bundles', 'static IPB',
            'dyn bundles', 'dyn IPB', 'dyn occupancy', 'body share of dyn '
            'bundles', 'crit path (model)'], rows))
    A("")
    A("`realisation` is R4.2.5's per-kernel choice between a compact vector "
      "loop and a fully unrolled chunk sequence; `body share` is the fraction "
      "of all dynamic bundles spent in the hot vector region — the Amdahl "
      "ceiling on anything that optimizes only that region.")
    A("")

    A("### 2.1 Per-family statistics")
    A("")
    rows = []
    for fam, rs in _family_groups(reports):
        rs = [r for r in rs if r.vectorized and r.body_bundles]
        if not rs:
            continue
        occs = [r.dynamic()['occupancy'] for r in rs]
        peak = max(r.dynamic()['peak_occupancy'] for r in rs)
        bocc = [r.occ.totals(r.body_bundles, dynamic=True)['occupancy'] for r in rs]
        cps = [r.hot.crit_path_true for r in rs if r.hot]
        rdy = [r.hot.avg_ready for r in rs if r.hot]
        dep_ = [r.hot.dep_depth for r in rs if r.hot]
        rows.append([fam, len(rs),
                     _pct(sum(occs) / len(occs)),
                     _pct(sum(bocc) / len(bocc)),
                     peak,
                     f"{sum(cps) / len(cps):.1f}" if cps else '-',
                     f"{sum(rdy) / len(rdy):.2f}" if rdy else '-',
                     f"{sum(dep_) / len(dep_):.1f}" if dep_ else '-'])
    A(_tbl(['family', 'kernels', 'avg bundle occupancy (program)',
            'avg bundle occupancy (vector body)', 'peak occupancy',
            'critical path', 'avg ready queue', 'dependency depth'], rows))
    A("")
    A("Peak occupancy reaching 8 while the average sits near 2 is the whole "
      "story in one line: the machine can be filled, and almost never is.")
    A("")
    A("### 2.2 Kernel statistics")
    A("")
    rows = []
    for r in reports:
        if not (r.vectorized and r.body_bundles):
            continue
        bs = r.occ.totals(r.body_bundles, dynamic=True)
        g = r.hot
        rows.append([r.name, _pct(bs['occupancy']), bs['peak_occupancy'],
                     g.crit_path_true if g else 'n/a',
                     f"{g.avg_ready:.2f}" if g else 'n/a',
                     g.max_ready if g else 'n/a',
                     g.dep_depth if g else 'n/a'])
    A(_tbl(['kernel', 'average bundle occupancy (vector body, dynamic)',
            'peak occupancy', 'critical path (model)',
            'average ready-queue size', 'max ready', 'dependency depth'], rows))
    A("")
    A("`n/a` marks a kernel R4.2.5 realised as a fully UNROLLED chunk sequence: "
      "there is no loop left, so there is no loop body to build a recurrence "
      "graph over. Its bundles are still measured in full.")
    A("")
    _sec_bundles(A, reports, vec)
    _sec_histograms(A, reports, vec)
    _sec_depgraphs(A, vec)
    _sec_critpath(A, vec)
    _sec_ready(A, vec)
    _sec_causes(A, reports, vec)
    _sec_opportunities(A, vec, opps)
    _sec_secondary(A, vec)
    _sec_validity(A)
    return L


def _u_summary(vec, key):
    vals = [w['bundles_base'] / w['bundles_per_iter']
            for w in (r.whatif.get(key) for r in vec)
            if w and w.get('ok') and w['bundles_per_iter']]
    if not vals:
        return 'nothing measurable'
    return (f"**{sum(vals) / len(vals):.2f}x** fewer bundles per iteration "
            f"(range {min(vals):.2f}x..{max(vals):.2f}x)")


def _pick(vec, *names):
    out = []
    for n in names:
        r = next((x for x in vec if x.name == n), None)
        if r:
            out.append(r)
    return out


def _sec_bundles(A, reports, vec):
    A("## 3. Per-bundle statistics")
    A("")
    A("Every bundle of the hot vector region, slot by slot, with the reason it "
      "closed. `EMPTY` slots carry that reason.")
    A("")
    for r in _pick(vec, 'axpy vi8', 'dot vi8', 'gemm vi8 16^3',
                   'conv2d 3x3 stencil'):
        d = r.dynamic()
        A(f"### 3.{1 + _pick(vec, 'axpy vi8', 'dot vi8', 'gemm vi8 16^3', 'conv2d 3x3 stencil').index(r)} "
          f"{r.name} — hot region `{r.body_block}`, executed "
          f"{r.trip:.0f}x, {len(r.body_bundles)} bundles/iteration")
        A("")
        A("```")
        for b in r.body_bundles:
            A(f"Bundle {b.index}   {b.occupied}/8 issued   "
              f"(aligner capacity {b.capacity})   closed by: {b.cause}")
            for k, line in enumerate(b.slot_lines()):
                txt = b.instrs[k] if k < b.occupied else ''
                A(f"    {line:16s} {txt}")
            A("")
        A("```")
        A("")
    A("The axpy body is the canonical case: two loads, a multiply, an add and "
      "a store, each in its own bundle, six of eight slots idle every time. "
      "The 3x3 stencil is the counter-example — a body large enough to fill "
      "bundles reaches 78% occupancy with the SAME scheduler and bundler, "
      "which is the direct evidence that the loss is a shortage of independent "
      "work, not a backend defect.")
    A("")


def _sec_histograms(A, reports, vec):
    A("## 4. Occupancy histograms")
    A("")
    tot_s = Counter()
    tot_d = Counter()
    for r in vec:
        for k, v in r.occ.occupancy_histogram().items():
            tot_s[k] += v
        for k, v in r.occ.occupancy_histogram(dynamic=True).items():
            tot_d[k] += v
    A("Whole programs, STATIC (one count per emitted bundle):")
    A("")
    A("```")
    A(_hist_block(tot_s, sum(tot_s.values()), 'instr'))
    A("```")
    A("")
    A("Whole programs, DYNAMIC (each bundle weighted by its execution count):")
    A("")
    A("```")
    A(_hist_block(tot_d, sum(tot_d.values()), 'instr'))
    A("```")
    A("")
    bs, bd = Counter(), Counter()
    for r in vec:
        for k, v in r.occ.occupancy_histogram(r.body_bundles).items():
            bs[k] += v
        for k, v in r.occ.occupancy_histogram(r.body_bundles, dynamic=True).items():
            bd[k] += v
    A("Hot VECTOR BODIES only, dynamic:")
    A("")
    A("```")
    A(_hist_block(bd, sum(bd.values()), 'instr'))
    A("```")
    A("")
    single = sum(v for k, v in bd.items() if k <= 2)
    A(f"**{_pct(single / sum(bd.values()))}** of dynamic vector-body bundles "
      f"issue two instructions or fewer.")
    A("")
    A("Per-family dynamic bundle occupancy:")
    A("")
    rows = []
    for fam, rs in _family_groups(vec):
        h = Counter()
        for r in rs:
            for k, v in r.occ.occupancy_histogram(r.body_bundles, dynamic=True).items():
                h[k] += v
        t = sum(h.values())
        rows.append([fam] + [f"{100.0 * h.get(k, 0) / t:.0f}%" if t else '-'
                             for k in range(1, 9)])
    A(_tbl(['family'] + [f"{k}/8" for k in range(1, 9)], rows))
    A("")


def _sec_depgraphs(A, vec):
    A("## 5. Dependency graphs (vector IR, one loop body)")
    A("")
    A("Node tags: `[V]` vector operation, `[M]` memory, `[C]` loop-carried "
      "edge. Edges are `target(kind,latency)`; `lat` is the model latency and "
      "`h` the latency-weighted height (its distance to the end of the body).")
    A("")
    for r in _pick(vec, 'axpy vi8', 'dot vi8', 'conv 3-tap', 'elementwise add'):
        if not r.hot:
            continue
        g = r.hot
        A(f"### {r.name} — `{g.label}`, {g.n_ops} operations, "
          f"{g.n_edges} edges ({g.n_carried} loop-carried)")
        A("")
        A("```")
        A(g.ascii_graph())
        A("```")
        A("")
        A(f"work {g.total_latency} / span {g.crit_path_true} = available "
          f"parallelism **{g.available_parallelism:.2f}**; "
          f"edge census {dict((k, v) for k, v in g.edge_counts.items() if v)}")
        A("")
    A("Note what the census shows: the carried WAR edges are name reuse, not "
      "dataflow — they disappear under renaming and are exactly what a "
      "software pipeliner or an unroller removes. The carried RAW edges are "
      "the induction variable; the carried MEM_RAW edges are the "
      "store-then-load pair of the SAME element, which is intra-iteration in "
      "truth and only appears carried because the disambiguator will not prove "
      "the next chunk distinct.")
    A("")


def _sec_critpath(A, vec):
    A("## 6. Critical path analysis")
    A("")
    rows = []
    for r in vec:
        g = r.hot
        if not g:
            continue
        p = r.whatif.get('pipelining', {})
        rows.append([r.name, g.n_ops, g.total_latency, g.crit_path_true,
                     g.crit_path_all, g.dep_depth,
                     f"{g.available_parallelism:.2f}",
                     g.res_mii, p.get('rec_mii_reg', '-'), p.get('rec_mii_all', '-'),
                     len(r.body_bundles)])
    A(_tbl(['kernel', 'ops', 'work (sum lat)', 'span (true deps)',
            'span (all deps)', 'depth (hops)', 'parallelism = work/span',
            'ResMII', 'RecMII (register)', 'RecMII (all carried edges)',
            'bundles/iter shipped'], rows))
    A("")
    A("Available parallelism below 2 means a single iteration of the body is "
      "very nearly a straight dependence chain: there is essentially nothing "
      "for a scheduler to interleave WITHIN one iteration. Every kernel whose "
      "ResMII is 1 could in principle run one bundle per iteration — the whole "
      "body fits in one bundle's worth of lanes — and every one of them ships "
      "at three to seven.")
    A("")


def _sec_ready(A, vec):
    A("## 7. Ready-queue analysis")
    A("")
    A("Ideal ready-set list schedule of the body under TRUE dependences only "
      "(infinite registers, real lane caps): how many operations are ready at "
      "each scheduling step.")
    A("")
    rows = []
    agg = Counter()
    for r in vec:
        g = r.hot
        if not g:
            continue
        for k, v in g.ready_hist.items():
            agg[k] += v
        rows.append([r.name, g.n_ops, f"{g.avg_ready:.2f}", g.max_ready,
                     g.ideal_steps, f"{g.ideal_ipb:.2f}",
                     len(r.body_bundles)])
    A(_tbl(['kernel', 'ops', 'avg ready', 'max ready', 'ideal steps',
            'ideal IPB', 'bundles/iter shipped'], rows))
    A("")
    A("Distribution of ready-set sizes over all vector loop bodies:")
    A("")
    A("```")
    t = sum(agg.values())
    for k in sorted(agg):
        A(f"  {k} ready {agg[k]:6.0f}  {_pct(agg[k] / t):>6}  {_bar(agg[k], t)}")
    A("```")
    A("")
    small = sum(v for k, v in agg.items() if k <= 2)
    A(f"**{_pct(small / t)}** of scheduling steps have at most two ready "
      f"operations on an 8-wide machine. This is the measurement that rules "
      f"out every scheduling-only optimization: a better scheduler cannot "
      f"issue instructions that are not ready.")
    A("")


def _sec_causes(A, reports, vec):
    A("## 8. Empty-slot classification — 100% attributed")
    A("")
    A("Every empty issue slot is attributed to the single reason its bundle "
      "closed. The categories come from the packer's own if/elif cascade "
      "(exactly one branch fires per rejection), refined for dependences by "
      "asking WHICH instruction in the bundle produced the value that was "
      "waited on. The partition is exhaustive by construction: the counts sum "
      "to the total.")
    A("")
    for scope, subset in (('whole programs', None), ('hot vector bodies', 'body')):
        st = Counter()
        dy = Counter()
        tot_s = tot_d = 0.0
        for r in vec:
            bs = r.body_bundles if subset else None
            st += r.occ.cause_histogram(bs)
            dy += r.occ.cause_histogram(bs, dynamic=True)
            tot_s += r.occ.totals(bs)['empty_slots']
            tot_d += r.occ.totals(bs, dynamic=True)['empty_slots']
        A(f"### 8.{1 if subset is None else 2} {scope}")
        A("")
        rows = []
        for cause, n in sorted(dy.items(), key=lambda kv: -kv[1]):
            fam, desc = occ.CAUSES.get(cause, ('?', ''))
            rows.append([cause, fam, f"{st[cause]:.0f}", _pct(st[cause] / tot_s),
                         f"{n:.0f}", _pct(n / tot_d), desc])
        rows.append(['**total**', '', f"{sum(st.values()):.0f}",
                     _pct(sum(st.values()) / tot_s),
                     f"{sum(dy.values()):.0f}",
                     _pct(sum(dy.values()) / tot_d), ''])
        A(_tbl(['cause', 'family', 'static empty slots', 'static %',
                'dynamic empty slots', 'dynamic %', 'meaning'], rows))
        A("")
    A("### 8.3 Grouped by family of cause (dynamic, hot vector bodies)")
    A("")
    fam = Counter()
    tot = 0.0
    for r in vec:
        fam += r.occ.family_histogram(r.body_bundles, dynamic=True)
        tot += r.occ.totals(r.body_bundles, dynamic=True)['empty_slots']
    rows = [[k, f"{v:.0f}", _pct(v / tot), _bar(v, tot, 30)]
            for k, v in sorted(fam.items(), key=lambda kv: -kv[1])]
    A(_tbl(['cause family', 'dynamic empty slots', 'share', ''], rows))
    A("")
    A("* **dependence** — the next instruction needed a value produced in this "
      "bundle. Real program structure; only more independent work (unrolling, "
      "pipelining, reassociation) removes it.")
    A("* **bundler** — a rule of the packer or aligner, not a dataflow fact: "
      "the memory-phase rule that keeps a store apart from a later write of "
      "its address register, the 4-lane memory limit, the divide lane, the "
      "call/SP rule.")
    A("* **region** — a label or a control transfer ended the bundle. This is "
      "the cost of scheduling one basic block at a time.")
    A("* **register** — a WAW hazard, i.e. a name reused with no renaming.")
    A("")
    A("### 8.4 Second, orthogonal decomposition: encoded vs issue-only")
    A("")
    enc = sum(r.occ.totals(r.body_bundles, dynamic=True)['encoded_empty']
              for r in vec)
    iss = sum(r.occ.totals(r.body_bundles, dynamic=True)['issue_only_empty']
              for r in vec)
    A(f"Of {enc + iss:.0f} dynamic empty slots in the vector bodies, "
      f"**{enc:.0f} ({_pct(enc / (enc + iss))})** are ENCODED — the aligner "
      f"pads the bundle to an 8-word capacity because it contains a load, "
      f"store, branch or divide, so the nulls occupy instruction memory as "
      f"well as issue slots — and **{iss:.0f} ({_pct(iss / (enc + iss))})** "
      f"are ISSUE-ONLY (capacity 1/2/4). This is a different question from "
      f"*why* the slot is empty and is counted separately so no slot is "
      f"double-attributed. It explains the +57.4% code-size cost R4.6.5 "
      f"measured: a one-instruction bundle holding a load still costs eight "
      f"words.")
    A("")


def _sec_opportunities(A, vec, opps):
    A("## 9. Ranked optimization opportunities")
    A("")
    A("Every estimate below is produced by re-running the PRODUCTION scheduler "
      "and bundle packer on a synthesised instruction stream — not by a cost "
      "formula. `suite gain` is the mean relative dynamic-IPB gain over ALL "
      f"{len(vec)} kernels (a kernel the transform does not apply to "
      "contributes zero); `where it applies` averages only over the kernels it "
      "fires on.")
    A("")
    rows = []
    for i, o in enumerate(opps, 1):
        rows.append([i, o.name, o.difficulty, f"{o.suite_gain:+.1%}",
                     f"{o.mean_gain:+.1%}", o.applies,
                     f"{o.mean_bundle_reduction:+.1%}"])
    A(_tbl(['#', 'optimization', 'difficulty', 'suite dynamic-IPB gain',
            'where it applies', 'kernels', 'dynamic bundles removed'], rows))
    A("")
    A("The ranking key is the MEASURED rows. Read them in this order: "
      "unrolling alone (#6) and disambiguation alone (#4) are each worth "
      "little; TOGETHER they are worth an order of magnitude more than either, "
      "because the unroll supplies the independent work and the "
      "disambiguation lets it share bundles.")
    A("")
    A("> The software-pipelining row is a **bound, not a measurement**: it is "
      "the MII (max of the resource and register-recurrence lower bounds) from "
      "the measured dependence graph, i.e. the best any modulo schedule could "
      "reach. It is listed for calibration — it says the ceiling is high — but "
      "it is not a schedule that has been produced, whereas every unrolling "
      "row IS a bundle count the production packer actually produced.")
    A("")

    A("### 9.1 Evidence — bundles per iteration, measured by the real packer")
    A("")
    rows = []
    for r in vec:
        w = r.whatif

        def bp(k):
            x = w.get(k)
            return f"{x['bundles_per_iter']:.2f}" if x and x.get('ok') else '-'
        rows.append([r.name, len(r.body_bundles), bp('unroll1_model'),
                     bp('unroll4'), bp('unroll1_disamb'), bp('unroll4_disamb'),
                     bp('unroll4_disamb_acc'), bp('unroll8_disamb_acc'),
                     w.get('unroll4_disamb_acc', {}).get('registers_short', '-')])
    A(_tbl(['kernel', 'shipped', 'model u=1', 'u=4 unroll only',
            'u=1 + disambiguation', 'u=4 + disambiguation',
            'u=4 + disamb + accumulators', 'u=8 + disamb + accumulators',
            'registers short at u=4'], rows))
    A("")
    A("For a kernel R4.2.5 realised as an UNROLLED chunk sequence the body "
      "already covers eight vector chunks, so `u=4` there means a 32-chunk "
      "body -- which is why those rows report tens of registers short. Their "
      "realistic transform is not more unrolling but the accumulator and "
      "reassociation work of 9.2/9.3.")
    A("")
    A("Read the two middle columns together. Unrolling without disambiguation "
      "barely moves: the copies cannot share bundles because copy 0's store "
      "and copy 1's loads use different base registers, and "
      "`bundler._mem_may_alias` treats different base registers as a possible "
      "alias by design (two registers may hold the same address). Adding "
      "distinct-object information — which the IR-level R2.2 "
      "`MemoryDisambiguator` already computes for the IR scheduler, but which "
      "never reaches the bundle packer — is what unlocks the unroll.")
    A("")

    A("### 9.2 Evidence — where multiple accumulators matter")
    A("")
    rows = []
    for r in vec:
        a = r.whatif.get('unroll4_disamb')
        b = r.whatif.get('unroll4_disamb_acc')
        if not (a and a.get('ok') and b and b.get('ok')):
            continue
        if abs(a['bundles_per_iter'] - b['bundles_per_iter']) < 1e-9:
            continue
        rows.append([r.name, r.family, f"{a['bundles_per_iter']:.2f}",
                     f"{b['bundles_per_iter']:.2f}",
                     f"{a['bundles_per_iter'] / b['bundles_per_iter']:.2f}x"])
    if rows:
        A(_tbl(['kernel', 'family', 'u=4 shared accumulator',
                'u=4 independent accumulators', 'extra gain'], rows))
    else:
        A("_No kernel in the suite separates the two variants._")
    A("")
    A("Renaming the loop-carried accumulator per copy is legal only because "
      "the operation is associative; it is the classic reassociation trade "
      "(a different, equally valid summation order) and it is the difference "
      "between a reduction that overlaps and one that does not.")
    A("")

    A("### 9.3 Evidence — reduction-tree reassociation")
    A("")
    rows = []
    for r in vec:
        w = r.whatif.get('reassociate')
        if not w:
            continue
        if w.get('ok'):
            rows.append([r.name, w['chain_len'], w['tree_depth'],
                         w['bundles_base'], w['bundles_new'],
                         f"{w['speedup']:.2f}x", w['registers_short']])
        else:
            rows.append([r.name, '-', '-', w.get('bundles_base', '-'), '-',
                         w.get('reason', '-'), '-'])
    A(_tbl(['kernel', 'chain length', 'tree depth', 'bundles now',
            'bundles as a tree', 'gain', 'registers short'], rows))
    A("")
    A("The reduction kernels sum eight partial results in a SEVEN-STEP SERIAL "
      "CHAIN because mem2reg leaves the recurrence in SSA form — each partial "
      "sum in a fresh register — and nothing reassociates it. A balanced tree "
      "is the same instruction count at logarithmic depth. The measured gain "
      "is small only because those kernels are already fully unrolled, so the "
      "chain overlaps with the loads that feed it; the transform is cheap and "
      "it composes with unrolling rather than competing with it.")
    A("")

    A("### 9.4 Evidence — why no scheduling-only optimization is ranked high")
    A("")
    rows = []
    for r in vec:
        w = r.whatif.get('local_sched', {})
        if not w.get('ok'):
            continue
        rows.append([r.name, w['bundles_base'], w['bundles_bound'], w['gain']])
    A(_tbl(['kernel', 'bundles shipped', 'lower bound for ANY local schedule',
            'headroom'], rows))
    A("")
    A("The bound is the largest of three quantities that hold for every legal "
      "schedule of the block: ceil(N/8) for the issue width, ceil(memory ops/4) "
      "for the lanes, and the longest chain of instruction pairs that can "
      "never share a bundle. The shipped schedule already meets it almost "
      "everywhere. Latency-aware scheduling, bundle-aware scheduling and "
      "better tie-breaks therefore have essentially nothing to win: the "
      "existing R2.3/R2.4 list scheduler is already extracting all the local "
      "ILP that exists. **The problem is the supply of independent work, not "
      "the scheduling of it.**")
    A("")


def _sec_secondary(A, vec):
    A("## 10. Secondary findings (measured, not the main recommendation)")
    A("")
    dup = [(r.name, len(base_alias_map(r.occ.flat))) for r in vec]
    dup = [(n, k) for (n, k) in dup if k]
    A("### 10.1 Duplicate base registers")
    A("")
    A("codegen materialises `FP + constant` separately for every array "
      "reference, so one object routinely occupies several registers at once. "
      "Two costs follow: the instructions themselves, and the loss of the "
      "only disjointness proof the bundler has (same base register, different "
      "constant offsets).")
    A("")
    if dup:
        A(_tbl(['kernel', 'base registers that duplicate another'],
               [[n, k] for (n, k) in sorted(dup, key=lambda x: -x[1])[:12]]))
    A("")
    A("The vi8 reduction is the extreme case: eight registers are loaded with "
      "the SAME address `FP-64`, one per chunk, filling an entire bundle with "
      "identical address arithmetic.")
    A("")
    A("### 10.2 Register headroom")
    A("")
    rows = []
    for r in vec:
        idxs = [i for b in r.body_bundles for i in b.flat_idx]
        free = _free_registers(r.occ.flat, min(idxs), max(idxs))
        reg = r.occ.registers
        rows.append([r.name, reg['registers_used'], reg['peak_live'],
                     len(free), f"{reg['avg_lifetime']:.1f}",
                     reg['max_lifetime']])
    A(_tbl(['kernel', 'registers used', 'peak live', 'free in the loop region',
            'avg lifetime (instrs)', 'max lifetime'], rows))
    A("")
    A("This is the constraint on how far the top-ranked transform can go: "
      "kernels with a large unrolled body have zero registers left, so an "
      "unroller must run BEFORE register allocation (on the vector IR, where "
      "values are unlimited temporaries) rather than as a peephole on mcode.")
    A("")
    A("### 10.3 The store-ordering rule")
    A("")
    tot = Counter()
    for r in vec:
        tot += r.occ.cause_histogram(r.body_bundles, dynamic=True)
    A(f"`store-ordering` alone accounts for {tot['store-ordering']:.0f} dynamic "
      f"empty slots in the vector bodies. It is the aligner's memory-phase "
      f"rule (a store may not share a bundle with a later instruction that "
      f"writes a register the store addresses with), which in a vector loop "
      f"means the induction-variable increment can never join the store's "
      f"bundle. It is a real hardware-imposed rule, not a compiler defect, but "
      f"an unroller sidesteps it: with u copies there are u-1 other stores and "
      f"loads available to fill that bundle instead.")
    A("")


def _sec_validity(A):
    A("## 11. Threats to validity")
    A("")
    A("* **Latency is a model, not hardware.** No cycle-accurate simulation has "
      "ever been run on this project. Critical paths, RecMII and the "
      "pipelining bound rank alternatives; they do not predict cycles. "
      "Occupancy, instruction mixes, empty-slot causes and bundle counts do "
      "not depend on latency at all.")
    A("* **Dynamic IPB here is bundle-weighted, not simulated.** Frequencies "
      "come from statically proved trip counts, so a kernel whose trip count "
      "cannot be proved is excluded from the dynamic totals rather than "
      "guessed at.")
    A("* **The what-if experiments synthesise code; they do not compile it.** "
      "They rewrite the shipped instruction stream and re-run the real "
      "scheduler and packer, so the bundle counts are real packer output for "
      "real instruction sequences — but no differential oracle has proved the "
      "synthesised sequences equivalent, because they are measurements, not "
      "candidate code. The transforms they stand for (unrolling, "
      "reassociation, multiple accumulators) all need the usual R1.x/R4.x "
      "legality and differential machinery when they are actually built.")
    A("* **The disambiguation experiment models a capability the compiler "
      "already has at IR level** (`loopopt.depgraph_disambig`, R2.2) but "
      "loses at mcode level. Whether that information can be carried to the "
      "packer cheaply is an engineering question R6.2 must answer; the "
      "measurement only says what it would be worth.")
    A("* **Unroll factors assume the trip count divides.** The remainder "
      "framework R4.4.5 already exists, and its cost is not charged in these "
      "projections.")
    A("* **25 hand-written kernels** across 8 families are a characterisation "
      "suite, not a workload mix. Six families are reused verbatim from the "
      "frozen R4.6.5 suite; the 2-D convolution family is taken from "
      "`conv_corpus.py`.")
    A("* **One kernel (`elementwise copy`) emits no vector operation at all** "
      "— a packed copy is pure wide memory movement — so its 'vector region' "
      "is its hot block, and it is flagged rather than silently counted as a "
      "vector kernel.")
    A("")
    A("## 12. Reproduction")
    A("")
    A("```sh")
    A("cd compiler")
    A("python3 -m vector_backend.ilp_analysis        # regenerates this report")
    A("python3 _r6_1_test.py                         # unit + invariant tests")
    A("```")
    A("")
    A("The analysis imports the compiler but never modifies it; running it "
      "leaves the repository byte-identical.")


# ── CSV + entry point ─────────────────────────────────────────────────────────

def write_csv(reports, path):
    rows = []
    for r in reports:
        if not r.vectorized:
            rows.append({'kernel': r.name, 'family': r.family,
                         'vectorized': 0, 'reason': r.reason})
            continue
        d, sT = r.dynamic(), r.static()
        b = r.occ.totals(r.body_bundles, dynamic=True) if r.body_bundles else {}
        g = r.hot
        w = r.whatif

        def bp(k):
            x = w.get(k)
            return round(x['bundles_per_iter'], 3) if x and x.get('ok') else ''
        rows.append({
            'kernel': r.name, 'family': r.family, 'vectorized': 1,
            'realisation': r.realisation, 'tier': r.tier,
            'region_kind': r.region_kind,
            'static_bundles': sT['bundles'], 'static_ipb': round(sT['ipb'], 4),
            'dynamic_bundles': d['bundles'],
            'dynamic_ipb': round(d['ipb'], 4),
            'dynamic_occupancy': round(d['occupancy'], 4),
            'body_bundles': len(r.body_bundles),
            'body_frequency': r.trip,
            'body_dynamic_occupancy': round(b.get('occupancy', 0), 4),
            'crit_path': getattr(g, 'crit_path_true', ''),
            'dep_depth': getattr(g, 'dep_depth', ''),
            'avg_ready': round(getattr(g, 'avg_ready', 0), 3),
            'max_ready': getattr(g, 'max_ready', ''),
            'parallelism': round(getattr(g, 'available_parallelism', 0), 3),
            'res_mii': getattr(g, 'res_mii', ''),
            'registers_used': r.occ.registers['registers_used'],
            'peak_live': r.occ.registers['peak_live'],
            'u1_model': bp('unroll1_model'), 'u4_plain': bp('unroll4'),
            'u1_disamb': bp('unroll1_disamb'), 'u4_disamb': bp('unroll4_disamb'),
            'u4_disamb_acc': bp('unroll4_disamb_acc'),
            'u8_disamb_acc': bp('unroll8_disamb_acc'),
            'local_sched_bound': w.get('local_sched', {}).get('bundles_bound', ''),
        })
    keys = sorted({k for row in rows for k in row})
    with open(path, 'w', newline='') as f:
        wr = csv.DictWriter(f, fieldnames=keys)
        wr.writeheader()
        wr.writerows(rows)
    return path


def main():
    reports = analyze_suite()
    opps = rank_opportunities(reports)
    md = '\n'.join(format_report(reports, opps)) + '\n'
    out_md = os.path.join(_C, 'R6_1_VECTOR_ILP_ANALYSIS.md')
    with open(out_md, 'w') as f:
        f.write(md)
    out_csv = os.path.join(_HERE, 'r6_1_results.csv')
    write_csv(reports, out_csv)
    print(f"wrote {out_md} ({len(md.splitlines())} lines)")
    print(f"wrote {out_csv}")
    for i, o in enumerate(opps, 1):
        print(f"  {i}. {o.suite_gain:+7.1%} suite  {o.difficulty:6s}  {o.name}")
    return reports, opps


if __name__ == '__main__':
    main()

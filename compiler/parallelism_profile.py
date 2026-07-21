"""
APARA Parallelism Profiler
==========================

Answers one question, per loop, in hard numbers:

    "Is this loop's bundle count limited by a real dependence recurrence
     (a dead end), or by resources / local scheduling (something the
     unroller or a software pipeliner could compress)?"

It does NOT change any code. It reads a finished .mcode file, flattens the
bundles back into a straight-line instruction stream (reusing bundler.py's
exact dependency parser so the hazard model matches the real compiler),
finds every loop via its back-edge, and reports for each loop body:

  N        real instructions in one iteration
  B        bundles the compiler currently emits for one iteration
           (= the achieved Initiation Interval, "II", in cycles/iteration)
  H        acyclic critical-path height (longest true-dependence chain).
           This is the floor a PERFECT local scheduler of ONE iteration
           could reach. If B == H the local scheduler is already optimal
           for a single iteration and only cross-iteration techniques help.
  RecMII   recurrence-bound minimum II: the tightest loop-carried
           dependence cycle. NO schedule of any kind can beat this without
           breaking the recurrence itself. This is the true dead end.
  ResMII   resource-bound minimum II: forced by lane limits
           (4 load/store, 1 div/sqrt, 8-wide bundle).
  MII      = max(RecMII, ResMII) -- the best II any pipeliner could reach.
  peak/free  register pressure inside the loop and how many of the 28
           allocatable registers are still free (the budget an unroller
           or pipeliner has to work with before it spills).

Interpretation (printed per loop):

  B / MII  = the throughput headroom. 1.0 means the loop is already
             optimal; 3.0 means a perfect pipeliner could run it ~3x
             faster per iteration.
  If MII is set by ResMII  -> resource-bound: unrolling / pipelining helps.
  If MII is set by RecMII  -> recurrence-bound: DEAD END, stop pushing
                              density; attack the recurrence or the
                              algorithm instead.

Usage:
    python3 compiler/parallelism_profile.py path/to/prog.mcode [more.mcode ...]

Runs on the already-bundled .mcode (the compiler's normal output) -- no
recompilation needed.
"""

import sys
import os
import re
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bundler import _parse_deps, _must_precede, _is_div_sqrt, _mem_may_alias  # noqa: E402

# Allocatable register pool (matches codegen.py / startup header):
#   $r1-$r25 $r29-$r31  -> 28 registers. r0=ZERO r26=FP r27=SP r28=GBASE.
_RESERVED = {'$r0', '$r26', '$r27', '$r28'}
_TOTAL_ALLOCATABLE = 28


# ── Raw bundle parser (keeps real bundle boundaries, unlike _parse_flat) ──────

def _parse_bundles(mcode_text):
    """Parse mcode into an ordered list of bundles, preserving the real
    bundle boundaries so we can count the ACHIEVED bundles-per-iteration.

        [{'labels': [str], 'instrs': [str]}, ...]
    """
    lines = mcode_text.split('\n')
    bundles = []
    cur = None
    pending = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith('//'):
            continue
        if s == '||':
            cur = {'labels': pending, 'instrs': []}
            pending = []
        elif s == ';':
            if cur is not None:
                bundles.append(cur)
            cur = None
        elif s.endswith(':') and cur is None:
            pending.append(s[:-1])
        elif cur is not None:
            cur['instrs'].append(s)
    return bundles


# ── Loop detection via back-edges ─────────────────────────────────────────────

_GOTO_RE = re.compile(r'\$goto\s+(\w+)')


def _find_loops(bundles):
    """Find natural loops as (head_bundle_idx, backedge_bundle_idx, label).

    A back-edge is any '$goto L' inside bundle j whose target label L is
    defined at bundle i <= j. The loop body is bundles[i .. j] inclusive.
    Handles the APARA while-loop shape (wc_/wb_/we_ labels) and nesting.
    """
    label_to_idx = {}
    for idx, b in enumerate(bundles):
        for lbl in b['labels']:
            label_to_idx[lbl] = idx

    loops = []
    for j, b in enumerate(bundles):
        for itext in b['instrs']:
            m = _GOTO_RE.search(itext)
            if not m:
                continue
            tgt = m.group(1)
            i = label_to_idx.get(tgt)
            if i is not None and i <= j:
                loops.append((i, j, tgt))
    return loops


# ── Per-loop instruction records ──────────────────────────────────────────────

def _records_for_body(bundles, head, backedge):
    """Flatten bundles[head..backedge] into dependency records (real instrs
    only). Returns list of dicts with writes/reads/mem_access/mem_write/text."""
    recs = []
    for idx in range(head, backedge + 1):
        for itext in bundles[idx]['instrs']:
            if not itext or itext == '$null':
                continue
            w, r, ctrl, mem_access, mem_write = _parse_deps(itext)
            recs.append({
                'text': itext, 'writes': w, 'reads': r, 'is_ctrl': ctrl,
                'mem_access': mem_access, 'mem_write': mem_write,
            })
    return recs


# ── Dependence classification: TRUE (value-flow) vs FALSE (name-reuse) ────────
#
# This distinction is the whole point of the profiler. With only 28 physical
# registers, codegen recycles the same register names every iteration, which
# manufactures WAR/WAW "anti-dependences" that serialize the schedule but carry
# NO value -- they vanish the instant you rename (unroll or software-pipeline).
# Counting them as real recurrences would falsely brand every loop a dead end.
#
#   TRUE  dep (survives renaming): RAW  a writes a reg b reads   -> real value
#                                  mem  a stores, b loads/stores the same addr
#   FALSE dep (removable by rename): WAR  a reads a reg b writes
#                                    WAW  a writes a reg b writes (diff values)
#                                    mem  load-then-store anti-order

def _true_dep(a, b):
    """a produces a value b consumes (RAW / true memory). Survives renaming.

    Memory edges use PROVABLY-SAME addresses only: RecMII built on _true_dep is
    claimed as a hard lower bound ('the wall'), so it must never rest on a
    may-alias guess -- an over-connected memory graph would invent recurrences
    that don't exist and falsely brand a loop a dead end. Under-counting an
    unprovable memory recurrence is the safe direction here (it can only
    understate the wall, never overstate it)."""
    if a['writes'] & b['reads']:
        return True                                  # register RAW
    if (a['mem_write'] is not None and b['mem_access'] is not None
            and _mem_same(a['mem_write'], b['mem_access'])):
        return True                                  # store -> load/store (true)
    return False


def _mem_same(x, y):
    """Provably-identical address: same base register AND same offset."""
    return x == y


# ── Metric 1: acyclic critical-path height H ──────────────────────────────────

def _critical_path(recs, dep_fn):
    """Longest chain of dependent instructions within ONE iteration (nodes),
    under the given dependence predicate. dep_fn=_must_precede gives the floor
    for the CURRENT register assignment; dep_fn=_true_dep gives the floor a
    renamed / pipelined schedule could reach."""
    n = len(recs)
    if n == 0:
        return 0
    depth = [1] * n
    for j in range(n):
        for i in range(j):
            if dep_fn(recs[i], recs[j]) and depth[i] + 1 > depth[j]:
                depth[j] = depth[i] + 1
    return max(depth)


# ── Metric 2: resource-bound MII ──────────────────────────────────────────────

def _res_mii(recs):
    """Lower bound on II forced by hardware lanes: 4 load/store lanes,
    1 divide/sqrt lane, 8 instruction slots. Returns the II only."""
    return _res_mii_detail(recs)[0]


def _res_mii_detail(recs):
    """Resource-bound II plus the breakdown that says WHICH resource binds.

    Returns (res_mii, n_ls, mem_term, width_term, div_term) where:
      mem_term   = ceil(load/store count / 4 lanes)   <- the APARA memory wall
      width_term = ceil(total instrs / 8 slots)
      div_term   = divide/sqrt count (1 lane)
    res_mii = max(the three). If mem_term binds, fewer memory ops is the ONLY
    way down -- better scheduling cannot beat the 4-lane limit. If width_term
    binds, fewer instructions of any kind (incl. memory) helps."""
    n = len(recs)
    n_ls = sum(1 for r in recs if r['text'].startswith(('$ld', '$st')))
    n_ds = sum(1 for r in recs if _is_div_sqrt(r['text']))
    mem_term = math.ceil(n_ls / 4) if n_ls else 0
    width_term = math.ceil(n / 8) if n else 0
    div_term = n_ds
    res = max(mem_term, width_term, div_term, 1)
    return res, n_ls, mem_term, width_term, div_term


# ── Metric 3: recurrence-bound MII (TRUE loop-carried dependence cycle) ───────

def _carried_true_edges(recs):
    """Loop-carried TRUE (value-flow) dependences only -- the recurrences no
    amount of renaming or pipelining can remove.

    A register R read at position i with NO earlier write of R in the same
    iteration is 'upward-exposed': it consumes the value R held at the end of
    the PREVIOUS iteration. If R is also written somewhere in the loop (i.e.
    it is updated, not a loop-invariant input), that is a genuine
    loop-carried RAW: an edge from R's last writer -> this reader, distance 1.
    Induction variables (i = i + 1) and accumulators (acc += x) are exactly
    this pattern. Memory-carried recurrences (a value stored this iteration,
    reloaded next) are handled the same way on (base, offset) addresses.

    Returns (edges, has_mem_carried) where edges are (u, v, lat=1, dist=1) and
    has_mem_carried flags that at least one recurrence flows through a stack
    slot (a store-then-reload of the same address) -- i.e. a variable that is
    a candidate for register promotion to break the recurrence."""
    n = len(recs)
    last_wr_reg = {}
    last_wr_mem = {}
    for i, r in enumerate(recs):
        for w in r['writes']:
            last_wr_reg[w] = i
        if r['mem_write'] is not None:
            last_wr_mem[r['mem_write']] = i

    edges = []
    has_mem_carried = False
    written = set()
    stored = set()
    for i, r in enumerate(recs):
        for rd in r['reads']:
            if rd in _RESERVED:
                continue
            if rd not in written and rd in last_wr_reg:
                edges.append((last_wr_reg[rd], i, 1, 1))     # carried reg RAW
        if r['mem_access'] is not None and r['mem_write'] is None:  # a load
            addr = r['mem_access']
            if addr not in stored:
                # reloading a slot written only later in the iteration =>
                # it reads the previous iteration's stored value. Provably-same
                # address only (see _true_dep): keeps RecMII a sound lower bound.
                for waddr, wi in last_wr_mem.items():
                    if _mem_same(addr, waddr):
                        edges.append((wi, i, 1, 1))          # carried mem RAW
                        has_mem_carried = True
                        break
        written |= r['writes']
        if r['mem_write'] is not None:
            stored.add(r['mem_write'])
    return edges, has_mem_carried


def _rec_mii(recs):
    """Recurrence-bound minimum II via the textbook cycle-ratio test, using
    ONLY true value-flow dependences (so removable register-reuse anti-deps do
    not inflate it).

    Graph:
       intra-iteration TRUE RAW  u->v (u<v, u produces a value v reads): dist 0
       loop-carried    TRUE RAW  u->v (_carried_true_edges):             dist 1
    A cycle's ratio is (sum latency)/(sum distance); with unit latency (a
    bundle's result is available to the NEXT bundle) RecMII is the smallest
    integer II with no positive-weight cycle under weight (lat - II*dist),
    found with Bellman-Ford. Returns (rec_mii, recurrence_node_count,
    mem_recurrence) where mem_recurrence flags a stack-slot recurrence that
    register promotion would break."""
    n = len(recs)
    if n == 0:
        return 1, 0, False

    edges = []  # (u, v, lat, dist)
    for u in range(n):
        for v in range(u + 1, n):
            if _true_dep(recs[u], recs[v]):
                edges.append((u, v, 1, 0))          # intra-iteration value flow
    carried, mem_recurrence = _carried_true_edges(recs)
    edges.extend(carried)                           # cross-iteration value flow

    # No true recurrence at all -> not recurrence-bound.
    if not any(d == 1 for *_, d in edges):
        return 1, 0, False

    def has_positive_cycle(ii):
        w = [(u, v, lat - ii * dist) for (u, v, lat, dist) in edges]
        dist = [0.0] * n
        for _ in range(n):
            changed = False
            for (u, v, ew) in w:
                if dist[u] + ew > dist[v] + 1e-9:
                    dist[v] = dist[u] + ew
                    changed = True
            if not changed:
                return False
        # one more pass: still relaxable => positive cycle
        for (u, v, ew) in w:
            if dist[u] + ew > dist[v] + 1e-9:
                return True
        return False

    ii = 1
    while ii <= n and has_positive_cycle(ii):
        ii += 1

    # size of the tightest recurrence (nodes on the binding cycle), approx:
    # the largest strongly-connected component under loop-carried+intra edges
    rec_nodes = _largest_scc_size(n, edges)
    return ii, rec_nodes, (mem_recurrence and ii > 1)


def _largest_scc_size(n, edges):
    """Tarjan SCC; returns the size of the largest component with >1 node or
    a self-loop (i.e. an actual recurrence)."""
    adj = [[] for _ in range(n)]
    self_loop = [False] * n
    for (u, v, *_ ) in edges:
        adj[u].append(v)
        if u == v:
            self_loop[u] = True
    index = [None] * n
    low = [0] * n
    on_stack = [False] * n
    stack = []
    counter = [0]
    best = [0]

    import sys as _sys
    _sys.setrecursionlimit(max(10000, n * 4))

    def strongconnect(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack[v] = True
        for w in adj[v]:
            if index[w] is None:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif on_stack[w]:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or (len(comp) == 1 and self_loop[comp[0]]):
                best[0] = max(best[0], len(comp))

    for v in range(n):
        if index[v] is None:
            strongconnect(v)
    return best[0]


# ── Metric 4: register pressure inside the loop ───────────────────────────────

def _reg_pressure(recs):
    """Peak simultaneous live allocatable registers over the loop body
    (straight-line approximation). Returns (peak, free_headroom)."""
    n = len(recs)
    if n == 0:
        return 0, _TOTAL_ALLOCATABLE
    # backward liveness over the straight-line body
    live = set()
    peak = 0
    for i in range(n - 1, -1, -1):
        r = recs[i]
        live -= r['writes']
        live |= r['reads']
        alloc_live = {x for x in live if x not in _RESERVED}
        peak = max(peak, len(alloc_live))
    return peak, _TOTAL_ALLOCATABLE - peak


# ── Analysis (shared by single-file report and corpus rank) ───────────────────

def _analyze(path):
    """Return (rows, had_loops). Each row is a fully-computed loop record."""
    with open(path) as f:
        text = f.read()
    bundles = _parse_bundles(text)
    loops = _find_loops(bundles)
    if not loops:
        return [], False

    heads = [h for (h, _, _) in loops]
    rows = []
    for (head, back, lbl) in sorted(loops):
        recs = _records_for_body(bundles, head, back)
        N = len(recs)
        B = back - head + 1                       # achieved II (bundles/iter)
        H_now = _critical_path(recs, _must_precede)   # floor for CURRENT regalloc
        H_true = _critical_path(recs, _true_dep)      # floor after renaming
        res, n_ls, mem_term, width_term, div_term = _res_mii_detail(recs)
        rec, rec_nodes, mem_rec = _rec_mii(recs)
        mii = max(res, rec)                        # best II any pipeliner reaches
        peak, free = _reg_pressure(recs)
        inner = not any(head < h2 <= back for h2 in heads if h2 != head)
        n_arith = N - n_ls                        # non-memory instrs
        intensity = (n_arith / n_ls) if n_ls else float('inf')
        # which resource binds ResMII?
        if res <= 1:
            res_driver = 'none'
        elif mem_term >= width_term and mem_term >= div_term:
            res_driver = 'mem-lanes'              # the APARA 4-mem-op wall
        elif width_term >= div_term:
            res_driver = '8-wide'
        else:
            res_driver = 'div-lane'
        rows.append(dict(file=os.path.basename(path), lbl=lbl, N=N, B=B,
                         Hnow=H_now, Htrue=H_true, res=res, rec=rec, mii=mii,
                         peak=peak, free=free, inner=inner, rec_nodes=rec_nodes,
                         mem_rec=mem_rec, n_ls=n_ls, mem_term=mem_term,
                         width_term=width_term, res_driver=res_driver,
                         intensity=intensity))
    return rows, True


# ── Report ────────────────────────────────────────────────────────────────────

def profile_file(path):
    rows, had = _analyze(path)
    print("=" * 82)
    print(f"  {os.path.basename(path)}")
    print("=" * 82)
    if not had:
        print("  (no loops detected)")
        print()
        return

    hdr = (f"  {'loop':<9}{'N':>4}{'mem':>4}{'B(II)':>7}{'Hnow':>6}{'Htrue':>7}"
           f"{'RecMII':>8}{'ResMII':>8}{'MII':>5}{'B/MII':>7}{'free':>6}  bound")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        bound = ("RECURRENCE" if r['rec'] >= r['res'] and r['rec'] > 1
                 else f"RESOURCE:{r['res_driver']}" if r['res'] > 1 else "false-dep")
        headroom = r['B'] / r['mii'] if r['mii'] else 1.0
        tag = "  <-- innermost" if r['inner'] else ""
        print(f"  {r['lbl']:<9}{r['N']:>4}{r['n_ls']:>4}{r['B']:>7}{r['Hnow']:>6}"
              f"{r['Htrue']:>7}{r['rec']:>8}{r['res']:>8}{r['mii']:>5}{headroom:>7.2f}"
              f"{r['free']:>6}  {bound}{tag}")
    print()
    print("  N=instrs/iter  mem=load/store ops/iter  B=bundles/iter (achieved II)")
    print("  Hnow=crit path now   Htrue=crit path after renaming")
    print("  RecMII=true recurrence floor  ResMII=lane floor (driver: mem-lanes/8-wide/div)")
    print("  MII=max(Rec,Res)=best II any pipeliner can reach   B/MII=speedup headroom")
    print()

    # focused verdict on innermost loops
    for r in rows:
        if not r['inner']:
            continue
        print(f"  [{r['lbl']}]  N={r['N']} instrs, emitting B={r['B']} bundles/iter.")
        # 1. how much is removable false (name-reuse) dependence?
        if r['Hnow'] > r['Htrue']:
            print(f"      - False-dependence serialization: critical path is "
                  f"{r['Hnow']} now but only {r['Htrue']} once register reuse is "
                  f"renamed away ({r['Hnow'] - r['Htrue']} instrs of the chain are "
                  f"NOT real value flow).")
        else:
            print(f"      - Critical path {r['Hnow']} is all true value flow "
                  f"(renaming alone buys nothing).")
        # 2. what is the real floor and who sets it?
        if r['rec'] >= r['res'] and r['rec'] > 1:
            if r['mem_rec']:
                print(f"      - RECURRENCE-BOUND via a STACK SLOT: II>={r['rec']} is "
                      f"forced by a counter/accumulator that is stored and reloaded "
                      f"each iteration. Promote it to a register to break the "
                      f"recurrence and drop this floor.")
            else:
                print(f"      - RECURRENCE-BOUND: true loop-carried recurrence forces "
                      f"II>={r['rec']}. This is the genuine wall; attack the "
                      f"recurrence or the algorithm, not the scheduler.")
        elif r['res'] > 1:
            if r['res_driver'] == 'mem-lanes':
                print(f"      - RESOURCE-BOUND by the 4 MEMORY LANES: {r['n_ls']} "
                      f"load/store ops/iter -> II>={r['res']}. Scheduling gets B "
                      f"down to {r['res']}, but NO schedule beats {r['res']}: to go "
                      f"lower you must issue FEWER memory ops (arith intensity "
                      f"{r['intensity']:.1f} arith/mem).")
            else:
                print(f"      - RESOURCE-BOUND by {r['res_driver']}: II>={r['res']} "
                      f"(true recurrence only needs {r['rec']}). Unrolling / software "
                      f"pipelining compresses II from {r['B']} toward {r['mii']}; "
                      f"fewer instrs lowers the floor further.")
        else:
            print(f"      - Bound only by false deps + issue order (Rec={r['rec']}, "
                  f"Res={r['res']}). Overlapping iterations can compress II from "
                  f"{r['B']} toward {r['mii']}.")
        # 3. the headroom number
        print(f"      - Headroom: B/MII = {r['B']}/{r['mii']} = "
              f"~{r['B']/r['mii']:.1f}x faster per iteration if fully scheduled.")
        # 4. register budget for the technique
        print(f"      - Register budget: peak {r['peak']}/{_TOTAL_ALLOCATABLE} live, "
              f"{r['free']} free -> can unroll ~{max(1, r['free'] // max(1, r['peak']))}x "
              f"before spilling.")
        print()


def _expand_paths(args):
    """Accept files and/or directories; recurse dirs for real .mcode files
    (skipping .aligned/.disass and backup/not_used trees)."""
    out = []
    for a in args:
        if os.path.isdir(a):
            for root, _dirs, files in os.walk(a):
                if 'backup' in root or 'not_used' in root:
                    continue
                for fn in files:
                    if (fn.endswith('.mcode') and not fn.endswith('.aligned.mcode')
                            and not fn.endswith('.disass.mcode')):
                        out.append(os.path.join(root, fn))
        elif os.path.exists(a):
            out.append(a)
        else:
            print(f"skip (not found): {a}")
    return sorted(set(out))


def rank_corpus(paths):
    """Sweep many files; rank innermost loops by speedup headroom and tally
    what bounds them, so the whole corpus's opportunity is visible at once."""
    allrows = []
    for p in paths:
        try:
            rows, _ = _analyze(p)
        except Exception as e:
            print(f"  (skip {p}: {e})")
            continue
        allrows.extend(r for r in rows if r['inner'])

    if not allrows:
        print("no innermost loops found in corpus")
        return
    allrows.sort(key=lambda r: (r['B'] / r['mii']), reverse=True)

    print("=" * 92)
    print(f"  CORPUS RANK: {len(allrows)} innermost loops across {len(paths)} files, "
          f"by per-iteration headroom")
    print("=" * 92)
    hdr = (f"  {'file':<34}{'loop':<8}{'N':>4}{'mem':>4}{'B':>5}{'MII':>5}"
           f"{'B/MII':>7}{'free':>6}  bound")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in allrows:
        bound = ("RECURRENCE" if r['rec'] >= r['res'] and r['rec'] > 1
                 else f"RES:{r['res_driver']}" if r['res'] > 1 else "false-dep")
        f = r['file'] if len(r['file']) <= 33 else r['file'][:30] + '...'
        print(f"  {f:<34}{r['lbl']:<8}{r['N']:>4}{r['n_ls']:>4}{r['B']:>5}"
              f"{r['mii']:>5}{r['B']/r['mii']:>7.2f}{r['free']:>6}  {bound}")

    # tallies
    mem_bound = [r for r in allrows if r['res_driver'] == 'mem-lanes' and r['res'] > 1]
    width_bound = [r for r in allrows if r['res_driver'] == '8-wide' and r['res'] > 1]
    rec_bound = [r for r in allrows if r['rec'] >= r['res'] and r['rec'] > 1]
    print()
    print(f"  memory-lane bound : {len(mem_bound):>3}  (fewer mem ops is the ONLY lever)")
    print(f"  8-wide bound      : {len(width_bound):>3}  (fewer instrs of any kind helps)")
    print(f"  recurrence bound  : {len(rec_bound):>3}  (break the recurrence / algorithm)")
    avg = sum(r['B'] / r['mii'] for r in allrows) / len(allrows)
    print(f"  mean headroom B/MII: {avg:.2f}x   (avg per-iteration speedup left on table)")
    print()


def main(argv):
    args = [a for a in argv[1:] if a != '--rank']
    do_rank = '--rank' in argv
    if not args:
        print(__doc__)
        print("error: give at least one .mcode file or directory")
        return 1
    paths = _expand_paths(args)
    if not paths:
        return 1
    if do_rank:
        rank_corpus(paths)
    else:
        for path in paths:
            profile_file(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))

"""
descriptor.py -- LoopDescriptor (Loop Optimization Framework, Milestone M0).

The LoopDescriptor is the single, immutable, per-loop input that every future
loop optimization (LICM, IVSR, loop-reg, rotation, unrolling, peeling,
unswitching, software pipelining, modulo scheduling) will consume. A pass reads
a descriptor; it never rebuilds loop infrastructure.

M0 populates ONLY the identity / structural / nesting fields and the borrowed
analysis handles. The InductionVars, MemEffects and Profile fields named in the
frozen architecture arrive in later milestones (M1/M2/M3) as ADDED fields on
this same object -- no pass adds descriptor fields for its own private use.

A descriptor is a SNAPSHOT keyed by `epoch`: it borrows (does not copy) the
analysis objects it was built from. If the IR is mutated, the epoch is stale --
discard every descriptor and re-run discovery. Nothing here mutates IR.
"""

# Canonical loop shapes (observations, never mutated by the descriptor):
#   TOP_TESTED    -- guard tests at the header; header has an exit edge
#   BOTTOM_TESTED  -- test at the latch; header has no exit edge (do-while form)
#   IRREGULAR     -- neither clean form (multiple/other exit structure)
# Rotation (a later LoopTransform) is the only thing that turns TOP_TESTED into
# BOTTOM_TESTED; canonicalization never changes shape.
TOP_TESTED = 'top-tested'
BOTTOM_TESTED = 'bottom-tested'
IRREGULAR = 'irregular'


class LoopDescriptor:
    """Immutable structural view of one natural loop within one function slice.

    All block ids are LOCAL to `func_slice` (the CFG is built per slice, block
    ids restart at 0), so descriptors from different functions never share an id
    space -- never compare block ids across descriptors of different slices.
    """

    __slots__ = (
        # ── provenance ──
        'func_slice', 'epoch', 'loop',
        # ── borrowed analysis handles (never copied) ──
        'cfg', 'dom', 'loop_info', 'liveness',
        # ── identity & structure ──
        'header', 'preheader', 'latches', 'back_edges',
        'body_blocks', 'exiting_blocks', 'exit_edges', 'exit_blocks',
        'shape', 'reducible', 'is_natural',
        # ── nesting ──
        'depth', 'parent', 'children', 'is_innermost', 'is_outermost',
        # ── induction variables (M1; populated by loopopt.analysis_iv) ──
        # Defaults leave this section empty/unknown so a descriptor is valid
        # before IV analysis runs (M0 behaviour is unchanged). None of these
        # fields are read by discovery or the M0 verifier.
        'iv_form', 'basic_ivs', 'derived_ivs', 'iv_terms',
        'primary_iv', 'trip_count', 'is_counted', 'guard_predicate',
        # ── memory effects (M2; populated by loopopt.analysis_mem) ──
        # Facts about the loop's memory operations. Defaults are empty/false so a
        # descriptor is valid before MemEffects runs. Read by no earlier stage.
        'mem_analyzed', 'loads', 'stores', 'calls', 'global_accesses',
        'may_read_memory', 'may_write_memory', 'has_side_effects',
        'invariant_insts', 'aliasing_summary',
        # ── profile (M3; populated by loopopt.analysis_profile) ──
        # IR-level performance FACTS (measurements, not cost decisions), computed
        # by reusing parallelism_profile.py's metric functions. Defaults are
        # zero/false so a descriptor is valid before Profile runs.
        'profile_analyzed', 'body_inst_count',
        'crit_path_height', 'crit_path_true',
        'res_mii', 'rec_mii', 'mii', 'est_ipb',
        'reg_pressure_peak', 'reg_free', 'profile_stats',
    )

    def __init__(self, func_slice, epoch, loop, cfg, dom, loop_info, liveness):
        # provenance
        self.func_slice = func_slice        # (lo, hi) inclusive IR indices
        self.epoch = epoch                  # invalidation token; stale ⇒ rediscover
        self.loop = loop                    # underlying analysis.loopinfo.Loop

        # borrowed analyses (shared across all descriptors of this slice)
        self.cfg = cfg
        self.dom = dom
        self.loop_info = loop_info
        self.liveness = liveness

        # identity & structure (filled by discovery)
        self.header = loop.header
        self.preheader = None
        self.latches = []                   # sorted unique back-edge tails
        self.back_edges = list(loop.back_edges)   # [(tail, header), ...]
        self.body_blocks = frozenset(loop.body)
        self.exiting_blocks = []            # body blocks with an edge leaving the loop
        self.exit_edges = []                # [(in_body_block, out_of_body_block), ...]
        self.exit_blocks = []               # unique targets outside the loop
        self.shape = IRREGULAR
        self.reducible = True               # LoopInfo yields only natural (reducible) loops
        self.is_natural = True

        # nesting (linked by discovery, per slice)
        self.depth = loop_info.depth(loop.header)
        self.parent = None
        self.children = []
        self.is_innermost = True
        self.is_outermost = True

        # induction variables -- UNPOPULATED until loopopt.analysis_iv runs.
        # `trip_count` defaults to an UNKNOWN TripCount (set by the analysis).
        self.iv_form = None             # 'memory-slot' / 'register' once analyzed
        self.basic_ivs = {}             # slot(int) -> BasicIV
        self.derived_ivs = {}           # temp_name  -> DerivedIV (scaled IVs)
        self.iv_terms = {}              # temp_name  -> (slot, scale) linear-in-IV
        self.primary_iv = None          # slot governing the loop's exit test
        self.trip_count = None          # TripCount (None until analyzed)
        self.is_counted = False         # True iff trip_count is statically KNOWN
        self.guard_predicate = None     # (slot, op, bound) of the exit test

        # memory effects -- UNPOPULATED until loopopt.analysis_mem runs.
        self.mem_analyzed = False
        self.loads = []                 # [MemAccess] (stack + computed reads)
        self.stores = []                # [MemAccess] (stack + computed writes)
        self.global_accesses = []       # [MemAccess] (IRGlobalLoad/Store)
        self.calls = []                 # [CallSite]
        self.may_read_memory = False
        self.may_write_memory = False
        self.has_side_effects = False   # a store or a call is present
        self.invariant_insts = frozenset()   # IR indices proven loop-invariant
        self.aliasing_summary = None    # AliasSummary (written keys + oracle)

        # profile -- UNPOPULATED until loopopt.analysis_profile runs.
        self.profile_analyzed = False
        self.body_inst_count = 0        # N: IR instructions in one iteration
        self.crit_path_height = 0       # Hnow: longest chain under all deps
        self.crit_path_true = 0         # Htrue: longest chain under true deps only
        self.res_mii = 1                # resource-bound MII (lanes)
        self.rec_mii = 1                # recurrence-bound MII (loop-carried cycle)
        self.mii = 1                    # max(res_mii, rec_mii)
        self.est_ipb = 0.0              # N / mii : IPB ceiling a pipeliner reaches
        self.reg_pressure_peak = 0      # peak simultaneous live values (estimate)
        self.reg_free = 0               # 28 - peak (estimate)
        self.profile_stats = {}         # resource/dependency breakdown (dict)

    # ── convenience accessors (read-only; delegate to borrowed analyses) ──────

    def header_block(self):
        """The CFG Block object for the header."""
        return self.cfg.blocks[self.header]

    def header_terminator(self):
        """The IR instruction that terminates the header block."""
        b = self.cfg.blocks[self.header]
        return self.cfg.instrs[b.hi]

    def body_instr_count(self):
        """Number of IR instructions across all body blocks (structural size)."""
        n = 0
        for b in self.body_blocks:
            blk = self.cfg.blocks[b]
            n += blk.hi - blk.lo + 1
        return n

    def label(self):
        """The header's label name, if any (loops are identified in reports by
        their header label, matching the profiler's fc_/wc_ naming)."""
        return self.cfg.blocks[self.header].label

    def __repr__(self):
        lbl = self.label()
        tag = f" '{lbl}'" if lbl else ""
        return (f"LoopDescriptor(hdr=B{self.header}{tag} slice={self.func_slice} "
                f"|body|={len(self.body_blocks)} depth={self.depth} "
                f"shape={self.shape} latches={self.latches} "
                f"exits={len(self.exit_edges)})")

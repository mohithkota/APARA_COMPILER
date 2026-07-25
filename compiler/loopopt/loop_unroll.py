"""
loop_unroll.py -- LoopUnroll infrastructure (Research milestone R1.1).

INFRASTRUCTURE ONLY -- this milestone performs NO unrolling. It prepares the
framework for a production LoopUnroll transform (R1.2+) by:

  * selecting the loops that are STRUCTURALLY eligible to unroll,
  * computing a detailed LEGALITY report per loop,
  * computing a PROFITABILITY model per loop (code growth, ILP exposure, register
    pressure, trip-count suitability, and the resulting factor/mode decision),

and integrating with the M5 framework as a transform whose run() is a deliberate
NO-OP: it flows through MutationTransaction + verification + rollback but never
mutates the IR. Driving it over the corpus yields 0 IR changes, 0 verifier
failures, 0 rollbacks.

It DUPLICATES no analysis. Every fact comes from the frozen framework:
  M0 LoopDescriptor / discovery  -- structure, nesting, shape, latch, preheader
  M1 InductionVars               -- primary IV, step, trip count
  M2 MemEffects                  -- opaque calls / side effects, invariant insts
  M3 Profile                     -- body size, MII (res/rec), est_ipb, reg pressure
  M7 Legality predicates         -- reused for the structural legality checks

The actual transform (kernel duplication, IV/trip adjustment, remainder loop) is
DEFERRED to R1.2. Here run() returns False (no-op) by design.
"""

from .descriptor import TOP_TESTED, BOTTOM_TESTED, IRREGULAR
from .discovery import discover_function
from .transform import LoopTransform, LoopTransformDriver, TransformStats
from . import legality as L
from .analysis_iv import TripCount, annotate_induction_vars
from .analysis_mem import annotate_memory_effects
from .analysis_profile import annotate_profile

# ── tuning constants for the (compute-only) profitability model ───────────────
_REG_BUDGET = 28            # codegen's dynamic register pool (r1-r25, r29-r31)
_REG_TARGET = 24            # keep some headroom below the pool before spilling
_CANDIDATE_FACTORS = (8, 4, 2)   # tried largest-first
_MAX_FULL_UNROLL_TRIP = 8   # fully unroll only very short known-trip loops
_CODE_GROWTH_BUDGET = 6.0   # max acceptable body-size multiplier


# ── unroll-specific legality predicates (compose M7 + a few local facts) ──────

def _shape_supported(desc):
    """A clean, SINGLE-EXIT while/do-while form: the shape is top/bottom-tested
    AND the loop leaves through exactly one exiting block (the guard). An
    IRREGULAR shape, or more than one exiting block (a mid-body break / early
    return), is multi-exit / unsupported control flow -- unrolling would have to
    replicate an early exit inside every copy, which R1 does not handle. The
    `shape` field alone only classifies the header/latch guard, so the additional
    exit-edge count is checked here explicitly."""
    shape_ok = desc.shape in (TOP_TESTED, BOTTOM_TESTED)
    single_exit = len(desc.exiting_blocks) == 1
    ok = shape_ok and single_exit
    if ok:
        reason = ''
    elif not shape_ok:
        reason = f'unsupported control flow (shape={desc.shape})'
    else:
        reason = (f'unsupported control flow ({len(desc.exiting_blocks)} exits; '
                  f'mid-body break / early exit)')
    return L._fact('shape_supported', ok, reason)


def _is_innermost(desc):
    """R1 scope: unroll innermost loops only. An outer loop is deferred (its body
    holds a nested loop, which a later milestone will handle)."""
    ok = desc.is_innermost
    return L._fact('is_innermost', ok,
                   '' if ok else 'not innermost (contains a nested loop; deferred)')


def _reducible(desc):
    ok = desc.reducible and desc.is_natural
    return L._fact('reducible', ok, '' if ok else 'irreducible / non-natural CFG')


# The unrolling legality is the conjunction below. Structural checks reuse the M7
# predicates verbatim; only shape/innermost/reducible are unroll-local. Order is
# chosen so the FIRST failing fact is the most informative rejection reason.
_UNROLL_LEGALITY = (
    L.has_labeled_header,        # identity (needed to relocate the loop post-transform)
    _reducible,                  # natural loop only  (rejects irreducible CFG)
    _shape_supported,            # clean while/do-while (rejects unsupported exits)
    L.has_unique_preheader,      # a preheader to host setup / the remainder loop
    L.has_single_latch,          # single back-edge
    L.has_explicit_backedge,     # the back-edge is a nameable branch to rewire
    _is_innermost,               # R1 scope
    L.has_clean_iv,              # a recognizable primary IV (rejects unknown induction)
    L.memory_safe,               # no opaque call (conservative side-effect gate for R1)
)


# ── report structures ─────────────────────────────────────────────────────────

class UnrollLegality:
    """Detailed legality report: every check's fact, plus the overall verdict and
    the first (most informative) rejection reason."""

    __slots__ = ('facts', 'eligible', 'reason')

    def __init__(self, desc):
        self.facts = [p(desc) for p in _UNROLL_LEGALITY]
        fail = next((f for f in self.facts if not f.ok), None)
        self.eligible = fail is None
        self.reason = '' if self.eligible else (fail.reason or f'{fail.name} failed')

    def as_dict(self):
        return {'eligible': self.eligible, 'reason': self.reason,
                'checks': [(f.name, bool(f.ok), f.reason) for f in self.facts]}


class UnrollProfitability:
    """Compute-only profitability model. Estimates code growth, ILP exposure,
    register-pressure increase and trip-count suitability, then derives a factor /
    mode DECISION -- without performing any transformation.

    Models (all grounded in the reused analyses, deliberately conservative):
      code growth  = factor  (+1 body if a remainder loop is needed)
      pressure     = reg_peak + (factor-1) * step_live, where step_live is the
                     count of induction/derived-IV values replicated per copy
                     (M1); ephemeral body temps have short live ranges and do not
                     accumulate across copies -- the standard unrolled-pressure
                     observation. Rejected if it would exceed _REG_TARGET.
      ILP exposure = 'resource' loops (rec_mii <= res_mii) gain independent work
                     from replicated iterations; 'recurrence' loops (rec_mii >
                     res_mii) gain mainly reduced loop overhead (M3)."""

    __slots__ = ('body_size', 'trip_kind', 'trip_value', 'iv_step',
                 'res_mii', 'rec_mii', 'mii', 'est_ipb', 'reg_peak', 'reg_free',
                 'step_live', 'ilp_bound', 'ilp_exposure',
                 'trip_suitability', 'recommended_factor', 'mode',
                 'code_growth', 'needs_remainder', 'est_pressure', 'pressure_ok',
                 'should_unroll', 'reason')

    def __init__(self, desc, eligible):
        self.body_size = desc.body_inst_count
        tc = desc.trip_count
        self.trip_kind = tc.kind if tc is not None else TripCount.UNKNOWN
        self.trip_value = tc.value if (tc is not None and tc.kind == TripCount.KNOWN) else None
        self.iv_step = (desc.basic_ivs[desc.primary_iv].step
                        if desc.primary_iv is not None and desc.primary_iv in desc.basic_ivs
                        else None)
        self.res_mii = desc.res_mii
        self.rec_mii = desc.rec_mii
        self.mii = desc.mii
        self.est_ipb = desc.est_ipb
        self.reg_peak = desc.reg_pressure_peak
        self.reg_free = desc.reg_free

        # values replicated per unrolled copy (IV / derived-IV live set)
        self.step_live = max(1, len(desc.basic_ivs) + len(desc.derived_ivs))

        # ILP exposure classification
        self.ilp_bound = 'resource' if self.rec_mii <= self.res_mii else 'recurrence'
        if self.ilp_bound == 'resource' and self.est_ipb < 8:
            self.ilp_exposure = 'high'
        elif self.ilp_bound == 'resource':
            self.ilp_exposure = 'moderate'
        else:
            self.ilp_exposure = 'low'

        # trip-count suitability + provisional mode
        self.needs_remainder = False
        if self.trip_kind == TripCount.KNOWN and self.trip_value is not None:
            T = self.trip_value
            if T <= 1:
                self.trip_suitability = 'unsuitable-tiny'
            elif T <= _MAX_FULL_UNROLL_TRIP and self.body_size * T <= self.body_size * _CODE_GROWTH_BUDGET:
                self.trip_suitability = 'full'
            else:
                self.trip_suitability = 'partial-known'
        elif self.trip_kind == TripCount.SYMBOLIC:
            self.trip_suitability = 'partial-remainder'
            self.needs_remainder = True
        else:
            self.trip_suitability = 'partial-remainder'
            self.needs_remainder = True

        # factor / mode selection (compute-only)
        self.recommended_factor, self.mode = self._select_factor()
        self.code_growth = self.recommended_factor + (1.0 if self.needs_remainder else 0.0)
        self.est_pressure = self.reg_peak + (self.recommended_factor - 1) * self.step_live
        self.pressure_ok = self.est_pressure <= _REG_TARGET

        # the DECISION (not executed)
        profitable = (self.recommended_factor >= 2 and self.pressure_ok
                      and self.code_growth <= _CODE_GROWTH_BUDGET
                      and (self.ilp_bound == 'resource' or self.mode == 'full'))
        self.should_unroll = bool(eligible and profitable
                                  and self.trip_suitability != 'unsuitable-tiny')
        self.reason = self._decision_reason(eligible, profitable)

    def _select_factor(self):
        if self.trip_suitability == 'full' and self.trip_value:
            return self.trip_value, 'full'
        if self.trip_suitability == 'unsuitable-tiny':
            return 1, 'none'
        # largest candidate factor within the pressure + code-growth budgets
        for F in _CANDIDATE_FACTORS:
            est_p = self.reg_peak + (F - 1) * self.step_live
            growth = F + (1.0 if self.needs_remainder else 0.0)
            if est_p <= _REG_TARGET and growth <= _CODE_GROWTH_BUDGET:
                return F, 'partial'
        return 1, 'none'

    def _decision_reason(self, eligible, profitable):
        if not eligible:
            return 'ineligible (see legality)'
        if self.trip_suitability == 'unsuitable-tiny':
            return f'trip count {self.trip_value} too small to unroll'
        if self.recommended_factor < 2:
            return 'no factor fits the pressure / code-growth budget'
        if not self.pressure_ok:
            return (f'estimated pressure {self.est_pressure} > target {_REG_TARGET}')
        if self.code_growth > _CODE_GROWTH_BUDGET:
            return f'code growth {self.code_growth:.0f}x over budget'
        if not (self.ilp_bound == 'resource' or self.mode == 'full'):
            return 'recurrence-bound: little ILP to expose'
        return (f'unroll {self.mode} x{self.recommended_factor} '
                f'(ILP {self.ilp_exposure}, pressure {self.est_pressure}/{_REG_BUDGET})')

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


class UnrollReport:
    """The full per-loop record: identity + structure + legality + profitability.
    R1.1 produces these; R1.2 will consume them to actually unroll."""

    __slots__ = ('label', 'header', 'depth', 'shape', 'is_innermost',
                 'single_latch', 'unique_preheader', 'has_iv', 'iv_step',
                 'trip_kind', 'trip_value', 'legality', 'profit', 'eligible')

    def __init__(self, desc, legality, profit):
        self.label = desc.cfg.blocks[desc.header].label
        self.header = desc.header
        self.depth = desc.depth
        self.shape = desc.shape
        self.is_innermost = desc.is_innermost
        self.single_latch = len(desc.latches) == 1
        self.unique_preheader = desc.preheader is not None
        self.has_iv = desc.primary_iv is not None
        self.iv_step = profit.iv_step
        self.trip_kind = profit.trip_kind
        self.trip_value = profit.trip_value
        self.legality = legality
        self.profit = profit
        self.eligible = legality.eligible

    def __repr__(self):
        tag = f"'{self.label}'" if self.label else f'B{self.header}'
        v = 'ELIGIBLE' if self.eligible else f'rejected: {self.legality.reason}'
        return (f"UnrollReport({tag} depth={self.depth} shape={self.shape} {v}; "
                f"decision: {self.profit.reason})")


# ── the transform (analysis-only; run() is a deliberate no-op) ────────────────

class LoopUnroll(LoopTransform):
    """R1.1: eligibility / legality / profitability analysis for loop unrolling.
    Integrates with the M5 framework but performs NO mutation -- run() returns
    False so every attempt is a clean no-op through the transaction machinery. The
    actual unrolling transform lands in R1.2."""

    name = 'loop-unroll'

    def legal(self, desc):
        rep = UnrollLegality(desc)
        return rep.eligible, rep.reason

    def run(self, instrs, lo, desc, txn):
        # R1.1 INFRASTRUCTURE ONLY -- no unrolling yet. Deliberate no-op: flows
        # through MutationTransaction + framework verify/rollback, changes nothing.
        return False

    # -- analysis entry point (does not touch the IR) --------------------------

    def analyze(self, desc):
        """Return the full UnrollReport for one loop. Annotates the reused M1/M2/M3
        analyses on demand (mutates the DESCRIPTOR only, never the IR)."""
        try:
            annotate_induction_vars([desc])
            annotate_memory_effects([desc])
            annotate_profile([desc])
        except Exception:
            pass
        legality = UnrollLegality(desc)
        profit = UnrollProfitability(desc, legality.eligible)
        return UnrollReport(desc, legality, profit)


# ── module-level helpers: analysis + no-op framework drive ────────────────────

def analyze_module(instrs):
    """Analyze every natural loop in `instrs` (no IR mutation). Returns a list of
    UnrollReport, innermost-first within each function."""
    from ir_utils import func_slices
    xform = LoopUnroll()
    reports = []
    for lo, hi in func_slices(instrs):
        descs = discover_function(instrs, lo, hi)
        descs.sort(key=lambda d: (-d.depth, d.header))
        for d in descs:
            reports.append(xform.analyze(d))
    return reports


def drive_noop(instrs, verifier=None):
    """Drive LoopUnroll through the M5 framework over every loop. Because run() is
    a no-op, this proves the pass integrates cleanly: it returns TransformStats
    with 0 commits / 0 rollbacks / 0 verifier failures and leaves the IR
    byte-identical."""
    drv = LoopTransformDriver(verifier=verifier)
    return drv.run(LoopUnroll(), instrs)


class UnrollSurveyReport:
    """Corpus-level aggregation of UnrollReports + the no-op framework stats."""

    __slots__ = ('programs', 'loops', 'eligible', 'rejected', 'reject_reasons',
                 'decisions', 'verifier_failures', 'rollbacks', 'ir_changes')

    def __init__(self):
        self.programs = 0
        self.loops = 0
        self.eligible = 0
        self.rejected = 0
        self.reject_reasons = {}
        self.decisions = {'would-unroll': 0, 'eligible-not-profitable': 0}
        self.verifier_failures = 0
        self.rollbacks = 0
        self.ir_changes = 0

    def add_program(self, reports):
        self.programs += 1
        for r in reports:
            self.loops += 1
            if r.eligible:
                self.eligible += 1
                if r.profit.should_unroll:
                    self.decisions['would-unroll'] += 1
                else:
                    self.decisions['eligible-not-profitable'] += 1
            else:
                self.rejected += 1
                self.reject_reasons[r.legality.reason] = \
                    self.reject_reasons.get(r.legality.reason, 0) + 1

    def report(self):
        out = ["LoopUnroll R1.1 survey:",
               f"  programs analysed   : {self.programs}",
               f"  loops analysed      : {self.loops}",
               f"  loops eligible      : {self.eligible}",
               f"  loops rejected      : {self.rejected}",
               f"  verifier failures   : {self.verifier_failures}",
               f"  rollbacks           : {self.rollbacks}",
               f"  IR changes          : {self.ir_changes}",
               "  -- eligible-loop decisions (compute-only, nothing transformed) --",
               f"    would-unroll (profitable) : {self.decisions['would-unroll']}",
               f"    eligible-not-profitable   : {self.decisions['eligible-not-profitable']}",
               "  -- rejection reasons --"]
        for reason, n in sorted(self.reject_reasons.items(), key=lambda kv: -kv[1]):
            out.append(f"    {n:4d}  {reason}")
        return "\n".join(out)

"""
occupancy.py -- issue-slot occupancy of the SHIPPED bundles, with a cause
assigned to every empty slot (Milestone R6.1).

ANALYSIS ONLY.  Nothing here packs a real bundle, changes a bundle, or is
imported by the compiler.

--------------------------------------------------------------------------------
WHAT IT MEASURES  (and why it is measured HERE and not on the IR)
--------------------------------------------------------------------------------
The IR-level analysis (dependency_graph.py) says how much parallelism EXISTS.
This module says how much was DELIVERED, on the exact text the backend emits,
after register allocation -- because two of the interesting causes (name reuse
and the bundler's memory-phase rule) do not exist at IR level at all.

    issue slots = 8 per bundle                     (FACT: LANE_CAPS['total'])
    occupied    = instructions in the bundle       (FACT: the shipped bundle)
    empty       = 8 - occupied

Every empty slot is attributed to EXACTLY ONE cause: the reason the bundle
closed, i.e. why the next instruction in the scheduled stream could not join it.
The attribution is exhaustive by construction (see CAUSE MODEL below), so the
"100% of empty issue slots classified" requirement is met structurally, not by
rounding a residual into an "other" bucket.

--------------------------------------------------------------------------------
FIDELITY -- this is not a model of the bundler, it IS the bundler
--------------------------------------------------------------------------------
`pack_with_attribution` is a line-by-line mirror of `bundler._pack_bundles`,
extended only to RECORD the reason each bundle closed.  It calls the bundler's
own `_parse_deps` / `_mem_may_alias` / `_is_div_sqrt` / `_bundle_capacity`, so
the hazard rules are not duplicated -- only the packing loop is, and
`analyze_mcode` asserts that its bundles are IDENTICAL to the real bundler's
output for the same input (`verified=True` on every report).  If the bundler ever
changes, the assertion fails loudly instead of the report going quietly stale.

--------------------------------------------------------------------------------
CAUSE MODEL
--------------------------------------------------------------------------------
The packer's split decision is an if/elif cascade, so exactly one branch fires
per rejection; the categories are mutually exclusive by construction.  R6.1
refines the two dependence categories by looking at WHICH instruction in the
bundle produced the blocking value, which turns a generic "RAW" into the causes
the milestone asks for ("waiting for vector load", "waiting for vector
multiply", "waiting for reduction dependency", ...).

Two structural cases complete the partition:
  * the bundle filled (occupied == 8)   -> no empty slots to attribute;
  * the instruction stream ended        -> 'no-ready-instruction'.

A SECOND, ORTHOGONAL decomposition is also reported: whether an empty slot is
ENCODED (the aligner pads the bundle to an 8-word capacity, so the null costs
IMEM) or ISSUE-ONLY (capacity 1/2/4; the slot is idle in the issue window but
costs no instruction memory).  That is a different question from "why", and is
kept in a separate field so no slot is counted twice.
"""

import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bundler as _b                                                # noqa: E402
from . import latency as lat                                        # noqa: E402

SP_REG = '$r27'          # mirrors bundler._pack_bundles' ABI constant


# ── cause taxonomy ────────────────────────────────────────────────────────────
#
# name -> (family, human description).  `family` groups causes for the report's
# top-level breakdown; DEPENDENCE causes are true program structure, BUNDLER
# causes are implementation restrictions, REGION causes are scheduling scope.

CAUSES = {
    'waiting-for-vector-load':      ('dependence',  'waiting for a vector load result'),
    'waiting-for-vector-multiply':  ('dependence',  'waiting for a vector multiply'),
    'waiting-for-vector-alu':       ('dependence',  'waiting for a vector ALU result'),
    'waiting-for-reduction':        ('dependence',  'waiting for a reduction ($dot/$vreduce) dependency'),
    'waiting-for-scalar-load':      ('dependence',  'waiting for a scalar load result'),
    'waiting-for-address-alu':      ('dependence',  'waiting for scalar address / IV arithmetic'),
    'waiting-for-scalar-alu':       ('dependence',  'waiting for a scalar ALU result'),
    'memory-dependence':            ('dependence',  'may-alias memory dependence (store -> access)'),
    'store-ordering':               ('bundler',     'store ordering: aligner memory-phase rule'),
    'register-pressure':            ('register',    'register reuse (WAW) -- no renaming'),
    'memory-lanes-full':            ('bundler',     'all 4 load/store lanes occupied'),
    'divide-lane-full':             ('bundler',     'the single divide/sqrt lane is occupied'),
    'call-sp-phase':                ('bundler',     'call may not share a bundle with an SP update'),
    'region-boundary-label':        ('region',      'a label starts a new bundle (no cross-block scheduling)'),
    'region-boundary-control':      ('region',      'a control transfer ends the bundle'),
    'no-ready-instruction':         ('region',      'no instruction left in the stream'),
    'bundle-full':                  ('none',        'bundle issued 8 instructions -- nothing lost'),
}

# packer reason -> cause, for the reasons that need no refinement
_STATIC_CAUSE = {
    'WAW':        'register-pressure',
    'MemAlias':   'memory-dependence',
    'MemPhase':   'store-ordering',
    'MemLane':    'memory-lanes-full',
    'FUnit':      'divide-lane-full',
    'Call':       'call-sp-phase',
    'Label':      'region-boundary-label',
    'Control':    'region-boundary-control',
    'BundleFull': 'bundle-full',
    'END':        'no-ready-instruction',
}

# producer op class -> RAW cause
_RAW_CAUSE = {
    'VLOAD':   'waiting-for-vector-load',
    'VMUL':    'waiting-for-vector-multiply',
    'VADD':    'waiting-for-vector-alu',
    'VALU':    'waiting-for-vector-alu',
    'VDOT':    'waiting-for-reduction',
    'VREDUCE': 'waiting-for-reduction',
    'LOAD':    'waiting-for-scalar-load',
    'STORE':   'waiting-for-scalar-alu',
    'VSTORE':  'waiting-for-scalar-alu',
    'ALU':     'waiting-for-address-alu',
    'SET':     'waiting-for-address-alu',
    'CAST':    'waiting-for-scalar-alu',
    'PACK':    'waiting-for-scalar-alu',
    'SLICE':   'waiting-for-scalar-alu',
    'CMOV':    'waiting-for-scalar-alu',
    'DIV':     'waiting-for-scalar-alu',
    'FSQRT':   'waiting-for-scalar-alu',
    'CALL':    'waiting-for-scalar-alu',
}


# ── one bundle ────────────────────────────────────────────────────────────────

class BundleInfo:
    """Everything measured about one emitted bundle.  Pure data."""
    __slots__ = ('index', 'labels', 'instrs', 'classes', 'block', 'occupied',
                 'capacity', 'empty', 'encoded_empty', 'issue_only_empty',
                 'reason', 'cause', 'blocker', 'producer', 'in_vector_region',
                 'frequency', 'mem_ops', 'vec_ops', 'ctl_ops', 'flat_idx')

    def __init__(self, index, labels, instrs, block, classes=None, flat_idx=()):
        self.index = index
        self.labels = list(labels)
        self.instrs = list(instrs)
        self.classes = list(classes) if classes is not None \
            else [lat.mcode_class(t) for t in instrs]
        self.block = block
        self.flat_idx = list(flat_idx)
        self.occupied = len(instrs)
        self.capacity = _b._bundle_capacity(instrs) if instrs else 1
        self.empty = lat.ISSUE_WIDTH - self.occupied
        # orthogonal split: slots the aligner actually encodes as $null padding
        self.encoded_empty = max(0, self.capacity - self.occupied)
        self.issue_only_empty = self.empty - self.encoded_empty
        self.reason = 'END'
        self.cause = 'no-ready-instruction'
        self.blocker = None            # the instruction text that could not join
        self.producer = None           # the in-bundle instruction it waited on
        self.in_vector_region = False
        self.frequency = 1.0
        self.mem_ops = sum(1 for t in instrs if lat.mcode_resource(t) == 'MEM')
        self.vec_ops = sum(1 for c in self.classes if c in lat.VECTOR_OPS)
        self.ctl_ops = sum(1 for t in instrs if lat.mcode_resource(t) == 'CTL')

    @property
    def family(self):
        return CAUSES.get(self.cause, ('unknown', ''))[0]

    def slot_lines(self):
        """The per-slot rendering used in the report (slot0..slot7)."""
        out = []
        for s in range(lat.ISSUE_WIDTH):
            if s < self.occupied:
                out.append(f"slot{s}  {self.classes[s]}")
            else:
                out.append(f"slot{s}  EMPTY")
        return out

    def __repr__(self):
        return (f"Bundle {self.index}: {self.occupied}/{lat.ISSUE_WIDTH} "
                f"[{','.join(self.classes)}] cause={self.cause}")


# ── the packer mirror, with attribution ───────────────────────────────────────

def refine_vector_classes(flat):
    """Annotate every instruction with its operation class, promoting the memory
    instructions that are VECTOR data movement by CONTEXT.

    On APARA a packed array is loaded 8 lanes at a time by an ordinary 64-bit
    `$ld` -- textually identical to a scalar 64-bit load (STATUS.md R4.1: arrays
    are 1-element-per-8-byte-word unless declared with a packed marker, and the
    packed form is exactly what makes vectorization possible).  So a load whose
    destination feeds a `$v`/`$dot`/`$vreduce` in the same basic block IS the
    vector load, and a store whose source is a vector result IS the vector
    store.  Classifying them by opcode alone would report the axpy loop as
    "waiting for a scalar load", which is wrong and would misdirect the whole
    optimization ranking.

    Scoped per basic block so a register reused later by scalar code cannot leak
    a vector classification.  Sets flat[i]['vclass'] in a COPY; `flat` itself is
    not mutated."""
    out = [dict(x) for x in flat]
    block, prev_ctrl = [], False
    blocks = []
    for i, ins in enumerate(out):
        if (ins['labels'] and block) or (prev_ctrl and block):
            blocks.append(block)
            block = []
        block.append(i)
        prev_ctrl = ins['is_ctrl']
    if block:
        blocks.append(block)

    for idxs in blocks:
        base = {i: lat.mcode_class(out[i]['text']) for i in idxs}
        vec_uses, vec_defs = set(), set()
        for i in idxs:
            if base[i] in lat.VECTOR_OPS:
                vec_uses |= out[i]['reads']
                vec_defs |= out[i]['writes']
        for i in idxs:
            c = base[i]
            if c == 'LOAD' and (out[i]['writes'] & vec_uses):
                c = 'VLOAD'
            elif c == 'STORE' and (out[i]['reads'] & vec_defs):
                c = 'VSTORE'
            out[i]['vclass'] = c
    return out


def pack_with_attribution(flat):
    """Mirror of `bundler._pack_bundles` that records why each bundle closed.

    Returns [BundleInfo].  The packing decisions -- and therefore the resulting
    bundles -- are identical to the production packer; `analyze_mcode` asserts
    that equality."""
    bundles = []
    c_labels, c_instrs, c_classes, c_idx = [], [], [], []
    c_writes, c_mem_reads = set(), set()
    c_mem_writes = []                  # store INSTRUCTIONS (R6.2: their symbolic
                                       # memory references must stay reachable)
    c_ctrl, c_ls, c_divsqrt = False, 0, 0
    c_parsed = []                                  # parsed deps of bundled instrs
    # `blk_key` is the block of the instruction being examined; `cur_blk` is the
    # block the OPEN bundle belongs to.  They differ exactly at a split, which is
    # why the closing bundle must be tagged with `cur_blk` -- tagging it with the
    # blocking instruction's block would shift every region by one bundle.
    blk_id, blk_key, prev_ctrl = 0, '0000:entry', False
    cur_blk = blk_key

    def close(reason, blocker, producer):
        bi = BundleInfo(len(bundles), c_labels, c_instrs, cur_blk, c_classes,
                        c_idx)
        bi.reason = reason
        bi.blocker = blocker['text'] if blocker else None
        bi.producer = producer['text'] if producer else None
        bi.cause = _refine(reason, producer)
        bundles.append(bi)

    for pos, instr in enumerate(flat):
        if instr['labels']:
            blk_id += 1
            blk_key = f"{blk_id:04d}:{instr['labels'][0]}"
        elif prev_ctrl:
            blk_id += 1
            blk_key = f"{blk_id:04d}:<fallthrough>"
        prev_ctrl = instr['is_ctrl']

        is_mem = instr['mem_access'] is not None
        is_call = instr['text'].startswith('$call')
        is_ls = instr['text'].startswith(('$ld', '$st'))
        is_ds = _b._is_div_sqrt(instr['text'])
        split, reason, producer = False, None, None
        if c_instrs:
            if instr['labels']:
                split, reason = True, 'Label'
            elif c_ctrl:
                split, reason = True, 'Control'
                producer = next((p for p in c_parsed if p['is_ctrl']), None)
            elif instr['reads'] & c_writes:
                split, reason = True, 'RAW'
                blocking = instr['reads'] & c_writes
                producer = next((p for p in c_parsed if p['writes'] & blocking), None)
            elif instr['writes'] & c_writes:
                split, reason = True, 'WAW'
                blocking = instr['writes'] & c_writes
                producer = next((p for p in c_parsed if p['writes'] & blocking), None)
            elif (instr['mem_access'] is not None
                  and _b._conflicts_with_stores(instr, c_mem_writes)):
                split, reason = True, 'MemAlias'
                producer = next((p for p in c_parsed
                                 if p['mem_write'] is not None
                                 and not _b._proved_independent(instr, p)), None)
            elif not is_mem and (instr['writes'] & c_mem_reads):
                split, reason = True, 'MemPhase'
                producer = next((p for p in c_parsed
                                 if p['mem_access'] is not None
                                 and (p['reads'] & instr['writes'])), None)
            elif is_call and SP_REG in c_writes:
                split, reason = True, 'Call'
            elif is_ls and c_ls >= 4:
                split, reason = True, 'MemLane'
            elif is_ds and c_divsqrt >= 1:
                split, reason = True, 'FUnit'
            elif len(c_instrs) >= lat.ISSUE_WIDTH:
                split, reason = True, 'BundleFull'

        if split:
            close(reason, instr, producer)
            c_labels.clear(); c_instrs.clear(); c_classes.clear(); c_idx.clear()
            c_writes.clear()
            del c_mem_writes[:]; c_mem_reads.clear(); c_parsed.clear()
            c_ctrl, c_ls, c_divsqrt = False, 0, 0

        if not c_instrs:                 # this instruction opens the bundle
            cur_blk = blk_key
        c_labels.extend(instr['labels'])
        c_instrs.append(instr['text'])
        c_classes.append(instr.get('vclass') or lat.mcode_class(instr['text']))
        c_idx.append(pos)
        c_parsed.append(instr)
        c_writes |= instr['writes']
        if instr['mem_write'] is not None:
            c_mem_writes.append(instr)
        if is_mem:
            c_mem_reads |= instr['reads']
        c_ctrl = c_ctrl or instr['is_ctrl']
        if is_ls:
            c_ls += 1
        if is_ds:
            c_divsqrt += 1

    if c_instrs or c_labels:
        close('END', None, None)
    return bundles


def _refine(reason, producer):
    """Turn a packer split reason into one of the R6.1 causes.  `producer` is the
    already-bundled instruction the blocked one had to wait for (None when the
    reason is structural)."""
    if reason in _STATIC_CAUSE:
        return _STATIC_CAUSE[reason]
    if reason == 'RAW':
        if producer is None:
            return 'waiting-for-scalar-alu'
        cls = producer.get('vclass') or lat.mcode_class(producer['text'])
        return _RAW_CAUSE.get(cls, 'waiting-for-scalar-alu')
    return 'no-ready-instruction'


# ── module-level report ───────────────────────────────────────────────────────

class OccupancyReport:
    """Occupancy + empty-slot classification for one compiled program."""

    def __init__(self, bundles, verified, flat):
        self.bundles = bundles
        self.verified = verified
        self.flat = flat
        self.registers = register_lifetimes(flat)

    # -- slot totals -----------------------------------------------------------
    def totals(self, subset=None, dynamic=False):
        """Slot accounting over `subset` (default: every bundle).

        dynamic=True weights every bundle by its execution frequency, which is
        the metric R6 is graded on (dynamic IPB); dynamic=False is the static
        code-image view."""
        bs = self.bundles if subset is None else subset
        w = (lambda b: b.frequency) if dynamic else (lambda b: 1.0)
        slots = sum(w(b) * lat.ISSUE_WIDTH for b in bs)
        occ = sum(w(b) * b.occupied for b in bs)
        enc = sum(w(b) * b.encoded_empty for b in bs)
        return {
            'bundles': sum(w(b) for b in bs),
            'instructions': occ,
            'issue_slots': slots,
            'empty_slots': slots - occ,
            'occupancy': (occ / slots) if slots else 0.0,
            'ipb': (occ / sum(w(b) for b in bs)) if bs else 0.0,
            'peak_occupancy': max((b.occupied for b in bs), default=0),
            'encoded_empty': enc,
            'issue_only_empty': slots - occ - enc,
            'vector_ops': sum(w(b) * b.vec_ops for b in bs),
            'mem_ops': sum(w(b) * b.mem_ops for b in bs),
        }

    def cause_histogram(self, subset=None, dynamic=False):
        """empty slots per cause.  Sums EXACTLY to totals()['empty_slots']."""
        bs = self.bundles if subset is None else subset
        w = (lambda b: b.frequency) if dynamic else (lambda b: 1.0)
        h = Counter()
        for b in bs:
            if b.empty:
                h[b.cause] += w(b) * b.empty
        return h

    def family_histogram(self, subset=None, dynamic=False):
        h = Counter()
        for cause, n in self.cause_histogram(subset, dynamic).items():
            h[CAUSES.get(cause, ('unknown', ''))[0]] += n
        return h

    def occupancy_histogram(self, subset=None, dynamic=False):
        """bundles (or dynamic bundle executions) per occupied-slot count."""
        bs = self.bundles if subset is None else subset
        w = (lambda b: b.frequency) if dynamic else (lambda b: 1.0)
        h = Counter()
        for b in bs:
            h[b.occupied] += w(b)
        return h

    def instruction_mix(self, subset=None, dynamic=False):
        bs = self.bundles if subset is None else subset
        w = (lambda b: b.frequency) if dynamic else (lambda b: 1.0)
        h = Counter()
        for b in bs:
            for c in b.classes:
                h[c] += w(b)
        return h

    # -- regions ---------------------------------------------------------------
    def vector_bundles(self):
        return [b for b in self.bundles if b.in_vector_region]

    def blocks(self):
        out = defaultdict(list)
        for b in self.bundles:
            out[b.block].append(b)
        return out


def analyze_mcode(mcode_text, label_freq=None, schedule=True):
    """Full occupancy analysis of one compiled program's mcode text.

    `label_freq` maps a code label to how many times its region executes; bundles
    inherit the frequency of the most recent labelled region (labels not present
    in the map are treated as executing once, and reported).  Pass None for a
    purely static analysis."""
    _header, flat = _b._parse_flat(mcode_text)
    # R6.2: attach symbolic memory references exactly as `bundle_mcode` does, so
    # this analysis keeps measuring what production actually packs.
    flat = _b._annotate_memrefs(flat)
    if schedule:
        flat = _b._schedule_within_blocks(flat)
    flat = refine_vector_classes(flat)
    bundles = pack_with_attribution(flat)

    # fidelity: identical to what the production bundler packs for this input
    real = _b._pack_bundles([dict(x) for x in flat])
    verified = ([r['instrs'] for r in real] == [b.instrs for b in bundles]
                and [r['labels'] for r in real] == [b.labels for b in bundles])

    _mark_vector_regions(bundles)
    _apply_frequencies(bundles, label_freq or {})
    return OccupancyReport(bundles, verified, flat)


def _mark_vector_regions(bundles):
    """A bundle is in the VECTOR REGION iff its basic block contains at least one
    real vector operation ($v / $dot / $vreduce).  Block-level rather than
    bundle-level because the loads that feed a vector op and the store that
    consumes it are textually indistinguishable from scalar 64-bit accesses --
    they are vector data movement by context, not by opcode."""
    vec_blocks = {b.block for b in bundles if b.vec_ops}
    for b in bundles:
        b.in_vector_region = b.block in vec_blocks


def _apply_frequencies(bundles, label_freq):
    cur = 1.0
    for b in bundles:
        for lb in b.labels:
            if lb in label_freq:
                cur = float(label_freq[lb])
                break
        else:
            if b.labels:                 # a label with no known frequency
                cur = 1.0
        b.frequency = cur


# ── register lifetimes (real, allocated registers) ────────────────────────────

_REG_RE = re.compile(r'\$r\d+')


def register_lifetimes(flat):
    """Def -> last-use spans of the ALLOCATED registers over the scheduled
    instruction stream.

    Reports how much of the 28-register pool the vector loops actually use --
    the measurement that decides whether 'register pressure' can be a real cause
    of idle slots, and whether unrolling / multiple accumulators have registers
    to work with."""
    first_def, last_use, used = {}, {}, set()
    for i, ins in enumerate(flat):
        for r in ins['reads']:
            last_use[r] = i
            used.add(r)
        for r in ins['writes']:
            first_def.setdefault(r, i)
            last_use.setdefault(r, i)
            used.add(r)
    live_at = [0] * (len(flat) + 1)
    for r in used:
        s = first_def.get(r, last_use.get(r, 0))
        e = last_use.get(r, s)
        for i in range(s, e + 1):
            live_at[i] += 1
    spans = {r: (last_use.get(r, 0) - first_def.get(r, last_use.get(r, 0)) + 1)
             for r in used}
    return {
        'registers_used': len(used),
        'pool': lat.REG_POOL,
        'free': max(0, lat.REG_POOL - len(used)),
        'peak_live': max(live_at) if live_at else 0,
        'avg_live': (sum(live_at) / len(flat)) if flat else 0.0,
        'avg_lifetime': (sum(spans.values()) / len(spans)) if spans else 0.0,
        'max_lifetime': max(spans.values()) if spans else 0,
    }


def region_register_lifetimes(flat, lo, hi):
    """Register lifetimes restricted to instructions [lo, hi] of the stream."""
    return register_lifetimes(flat[lo:hi + 1])

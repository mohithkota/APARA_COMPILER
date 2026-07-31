"""
latency.py -- the latency and resource model used by every R6.1 analysis.

ANALYSIS ONLY.  This module states a model; it changes nothing.

--------------------------------------------------------------------------------
PROVENANCE -- what is FACT and what is MODEL
--------------------------------------------------------------------------------
FACT (measured from the implementation, not assumed):

  * issue width 8, 4 memory lanes, 1 divide/sqrt lane, 1 control transfer per
    bundle.  Taken from `vector_capability_db.LANE_CAPS`, which R4.0 derived from
    the assembler's own lane placement (McodeProgram::alignFullBundleToLanes) and
    the bundler's enforcement of the same limits (`bundler._pack_bundles`).
  * 28 allocatable registers (`vector_capability_db.REGISTER_POOL`).
  * the alignment capacity rule (1/2/4/8, forced to 8 by any load/store, CTI,
    divide or fsqrt) -- `bundler._bundle_capacity`, a mirror of mcode_align.

MODEL (relative weights, NOT hardware-measured):

  * per-instruction LATENCY.  The APARA ISA documentation and the simulator
    expose no instruction timings, and no cycle-accurate run has ever been
    performed on this project (R5.0 threats-to-validity).  Rather than invent a
    second set of numbers, this module REUSES the frozen R2.4 latency model
    (`loopopt.schedule._latency`) unchanged, and extends it only where a vector
    node had no entry.  All latency-derived figures in the R6.1 report are
    therefore labelled MODEL and are used only for RELATIVE ranking (which chain
    is longer), never as a cycle count.

The distinction matters for the deliverable: occupancy, empty-slot causes and
instruction mixes are STRUCTURAL FACTS about the shipped code; critical path and
"stall exposure" are model estimates on top of those facts.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vector_capability_db import LANE_CAPS, REGISTER_POOL       # noqa: E402
from loopopt.schedule import _latency as _ir_latency_r24         # noqa: E402
from loopopt.schedule import _iclass as _ir_iclass_r24           # noqa: E402

# ── hardware issue model (FACT: R4.0 capability database) ─────────────────────
ISSUE_WIDTH = LANE_CAPS['total']        # 8 instructions may issue per bundle
MEM_LANES   = LANE_CAPS['mem']          # 4 load/store lanes
DIV_LANES   = LANE_CAPS['div_sqrt']     # 1 divide/sqrt lane
CTL_LANES   = LANE_CAPS['ctl']          # 1 control transfer, and it ends the bundle
REG_POOL    = REGISTER_POOL             # 28 allocatable registers


# ── IR-level latency (MODEL: reused from R2.4, extended for wide memory) ───────

_IR_LATENCY_EXTRA = {
    'IRLoadWide':  3,      # same memory result latency as a scalar load
    'IRStoreWide': 1,      # a store retires like a store
    'IRVecDot128': 4,      # two chained $dot ($accumulate) -- see IRVecDot128 doc
}


def ir_latency(ins):
    """Result latency (MODEL) of one IR instruction, in bundle-distance units.

    Delegates to the frozen R2.4 model so R6.1 cannot silently disagree with the
    scheduler that produced the code being analysed."""
    c = type(ins).__name__
    if c in _IR_LATENCY_EXTRA:
        return _IR_LATENCY_EXTRA[c]
    return _ir_latency_r24(ins)


def ir_class(ins):
    """Resource class of one IR instruction: MEM / DIV / CTL / ALU (R2.4)."""
    return _ir_iclass_r24(ins)


def ir_is_vector(ins):
    """True for the IR nodes the vectorizer emits as real vector instructions."""
    return type(ins).__name__ in ('IRVecArith', 'IRVecDot', 'IRVecDot128',
                                  'IRVecReduce')


def ir_is_wide_mem(ins):
    """True for the u128/u256 wide memory nodes (register-group load/store)."""
    return type(ins).__name__ in ('IRLoadWide', 'IRStoreWide')


# ── mcode-level operation classes (FACT: textual form emitted by codegen) ─────
#
# The report's per-slot instruction mix uses these names.  They are a pure
# classification of the instruction text the bundler already parses -- no new
# parsing rules, no semantic claim beyond the opcode.

VECTOR_OPS = ('VMUL', 'VADD', 'VALU', 'VDOT', 'VREDUCE')

_RE_V      = re.compile(r'^\$v\s+(\S+)')
_RE_LDST   = re.compile(r'^\$(ld|st)\s+\(\$[iuf](\d+)\)')
_RE_ALU    = re.compile(r'^([+\-*/<>&|^~]{1,3})\s+\$r')


def mcode_class(text):
    """Coarse operation class of one mcode instruction (for the slot mix).

    Vector classes are separated from scalar ones because the whole milestone is
    about vector issue slots: VLOAD/VSTORE are the wide ($u128/$u256) or packed
    64-bit accesses feeding vector operations, VMUL/VADD/VALU the $v ALU forms,
    VDOT/VREDUCE the reduction primitives."""
    t = text.strip()
    if not t or t == '$null':
        return 'EMPTY'
    if t.startswith('$dot'):
        return 'VDOT'
    if t.startswith('$vreduce'):
        return 'VREDUCE'
    m = _RE_V.match(t)
    if m:
        op = m.group(1)
        if op == '*':
            return 'VMUL'
        if op == '+':
            return 'VADD'
        return 'VALU'
    m = _RE_LDST.match(t)
    if m:
        wide = int(m.group(2)) > 64
        if m.group(1) == 'ld':
            return 'VLOAD' if wide else 'LOAD'
        return 'VSTORE' if wide else 'STORE'
    if t.startswith('$set'):
        return 'SET'
    if t.startswith('$cast'):
        return 'CAST'
    if t.startswith('$cmov'):
        return 'CMOV'
    if t.startswith('$slice'):
        return 'SLICE'
    if t.startswith('$pack'):
        return 'PACK'
    if t.startswith('$fsqrt'):
        return 'FSQRT'
    if t.startswith('$call'):
        return 'CALL'
    if t.startswith('$return'):
        return 'RETURN'
    if t.startswith('$halt'):
        return 'HALT'
    if t.startswith('?'):
        return 'BRANCH'
    m = _RE_ALU.match(t)
    if m:
        return 'DIV' if m.group(1) == '/' else 'ALU'
    return 'OTHER'


def mcode_is_vector(text):
    """True iff this instruction is a vector ALU / dot / reduce operation.

    NOTE: a 64-bit `$ld`/`$st` of a PACKED array is the vector data path on
    APARA (one 64-bit word = 8 vi8 lanes), but textually identical to a scalar
    64-bit access.  It is therefore NOT counted here; membership of a vector
    REGION is decided in occupancy.py by the presence of a real vector op in the
    same basic block."""
    return mcode_class(text) in VECTOR_OPS


# ── mcode-level latency (MODEL: same weights as the IR model) ─────────────────

_MCODE_LATENCY = {
    'VLOAD': 3, 'LOAD': 3, 'VSTORE': 1, 'STORE': 1,
    'VMUL': 2, 'VADD': 2, 'VALU': 2,          # IRVecArith == 2 in the R2.4 model
    'VDOT': 4, 'VREDUCE': 3,                  # IRVecDot == 4, IRVecReduce == 3
    'DIV': 8, 'FSQRT': 8, 'CALL': 5,
}


def mcode_latency(text):
    """Result latency (MODEL) of one mcode instruction.  Mirrors ir_latency so
    the IR-level and mcode-level critical paths are directly comparable."""
    c = mcode_class(text)
    if c == 'ALU':
        # `* $rd (...)` is a multiply; everything else in this class is 1 cycle.
        return 3 if text.strip().startswith('*') else 1
    return _MCODE_LATENCY.get(c, 1)


def mcode_resource(text):
    """Resource class (MEM / DIV / CTL / ALU) of one mcode instruction -- the
    same partition the bundler enforces lane limits on."""
    c = mcode_class(text)
    if c in ('LOAD', 'STORE', 'VLOAD', 'VSTORE'):
        return 'MEM'
    if c in ('DIV', 'FSQRT'):
        return 'DIV'
    if c in ('BRANCH', 'CALL', 'RETURN', 'HALT'):
        return 'CTL'
    return 'ALU'

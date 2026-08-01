"""
rematerialization.py -- Register Rematerialization for address temporaries (R7.1).

R7.0 measured the dominant remaining bottleneck: **26 of the 33 registers the
allocator spills across the register-pressure-rejected software-pipelined kernels
hold `FP + constant`**. They are `IRLoadAddr` results -- the compact vector loop's
stack-slot addresses (`_vcl1`, `_vcb7`, `_vct23`, ...). The allocator pays a store
plus a later reload, two memory operations, to preserve a value it could recompute
with ONE ALU instruction and NO register inputs, because it has no
rematerialization concept at all.

This module supplies that concept. It owns two decisions and nothing else:

    recipe_for(...)   is this value recomputable with no live register inputs?
    choose_victim(..) which live value should be evicted, given that some can be
                      recomputed for free and others must go to memory?

`codegen.py` keeps ownership of allocation and emission; the only behaviour it
changes is what happens to a value that is about to be evicted.

WHAT IS ELIGIBLE, AND WHY ONLY THIS
-----------------------------------
`IRLoadAddr` lowers to `+ dest ($i64) $r28 <fp_off>` when the offset fits the
signed 10-bit immediate field, and to a `_load_const` into a BORROWED SCRATCH
register plus an add when it does not. Only the first form qualifies:

  * it is one instruction;
  * `$r28` (FP) is fixed for the whole function, so the value is correct at any
    point in it -- there is no ordering constraint and no input to keep alive;
  * it needs no free register beyond the destination.

The large-offset form is deliberately NOT eligible. Recomputing it would need to
borrow a scratch register, and the moment rematerialization matters is exactly the
moment no register is free. Rematerializing it could therefore trigger a further
eviction -- the opposite of the point.

Nothing else is rematerialized. Memory loads are never duplicated (a load may
observe a different value later), and values computed from other registers are
excluded because they would keep their inputs alive, which is the cost this pass
exists to avoid.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The signed immediate range `_gen_IRLoadAddr` can fold directly into the add.
# Kept in step with codegen's own test rather than widened: outside it, lowering
# needs a scratch register and the value stops being free to recompute.
FP_IMM_LO, FP_IMM_HI = -512, 511


def _disabled():
    return os.environ.get('APARA_NO_REMAT', '') not in ('', '0')


class Recipe:
    """How to recompute one value. Currently the only kind is `FP + constant`;
    the class exists so a second kind can be added without touching callers."""

    __slots__ = ('kind', 'fp_offset')

    def __init__(self, fp_offset):
        self.kind = 'fp+const'
        self.fp_offset = fp_offset

    def emit(self, reg, fp_reg):
        """The instruction text that reconstructs the value into `reg`. One
        instruction, no register inputs beyond the frame pointer."""
        return f"+ {reg} ($i64) {fp_reg} {self.fp_offset}"

    def __repr__(self):
        return f'<remat {self.kind} FP{self.fp_offset:+d}>'


def recipe_for_loadaddr(fp_offset):
    """A Recipe for `IRLoadAddr`, or None if this one is not free to recompute."""
    if _disabled():
        return None
    if FP_IMM_LO <= fp_offset <= FP_IMM_HI:
        return Recipe(fp_offset)
    return None


def choose_victim(live_items, protect, recipes, rebuilt=()):
    """Pick the temp to evict. Returns (name, reg, recipe_or_None).

    The ONLY allocation-policy change R7.1 makes: among the values that may be
    evicted, prefer one that can be rematerialized, because evicting it costs
    nothing -- no store now, no reload later, just one ALU instruction at each
    later use. If none qualifies, the allocator's existing choice (the first
    evictable value) is returned unchanged and spills to memory exactly as before.

    `rebuilt` is the ANTI-THRASH guard and it is load-bearing. A value that has
    already been rebuilt once is no longer *preferred*, though it stays
    recomputable if chosen anyway. Without this, a rematerializable value is
    evicted, rebuilt at its next use, immediately becomes the preferred victim
    again, and the pair ping-pongs while real values still have to spill.
    Measured on `axpy vi32`: 8 spills without preference, 56 with unguarded
    preference, 3 with the guard.

    Among candidates the scan order is the allocator's own, so output stays
    deterministic."""
    plain = None          # first evictable value with no recipe
    rebuilt_remat = None   # first recomputable value that has ALREADY been rebuilt
    for name, reg in live_items:
        if name in protect:
            continue
        r = recipes.get(name)
        if r is None:
            if plain is None:
                plain = (name, reg, None)
            continue
        if name not in rebuilt:
            return name, reg, r            # free to evict, and not yet rebuilt
        if rebuilt_remat is None:
            rebuilt_remat = (name, reg, r)
    # A value already rebuilt once is evicted only as a LAST resort, after any
    # ordinary value: preferring it again is what produced the ping-pong.
    if plain is not None:
        return plain
    if rebuilt_remat is not None:
        return rebuilt_remat
    return None, None, None


class RematStats:
    """What the pass actually did, for the report and the tests."""

    __slots__ = ('tracked', 'evictions_avoided', 'recomputations', 'spills')

    def __init__(self):
        self.tracked = 0             # values registered as rematerializable
        self.evictions_avoided = 0   # evicted WITHOUT a store, thanks to a recipe
        self.recomputations = 0      # instructions emitted to rebuild a value
        self.spills = 0              # evictions that still went to memory

    def __repr__(self):
        return (f'<remat tracked={self.tracked} '
                f'avoided={self.evictions_avoided} '
                f'recomputed={self.recomputations} spills={self.spills}>')

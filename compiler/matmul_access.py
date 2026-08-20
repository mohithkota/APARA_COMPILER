"""
matmul_access.py -- R13.0 Phase 1: the generic matmul access representation.

ANALYSIS ONLY. This module decides nothing about codegen, emits no IR, and is
not wired into the production pipeline. It answers exactly one question:

    is this loop's multiplicand pair expressible as

        access = invariant_base + IV * elem_bytes

    such that the EXISTING $dot lowering could consume it?

WHY THIS EXISTS
---------------
`vector_lowering.plan_lowering` requires every array load's offset to be a BARE
IV term scaled by `elem_bytes` (`vector_lowering.py:201-209`). A matmul offset
is `invariant_row_base + IV*elem_bytes`, so no array is extracted and the plan
dies with `pattern:array-bases-not-extracted` (`vector_lowering.py:217`).

The missing capability is the ACCESS REPRESENTATION, not the `$dot` instruction:
detection (`kernel_detector.py:283`), the capability mapping
(`vector_legality.py:44`), profitability and the `$dot`/`$vreduce` emitter all
already work.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No matrix-size checks, no benchmark or function or variable name checks, no
vu8-specific logic, no tile assumptions, no fixed unroll factors. Every decision
below is structural, derived from the IR and the capability database. The
element width is read FROM THE SOURCE and is never changed to win lanes.

REUSE
-----
Nothing here re-implements affine analysis. `vector_affine` supplies
`classify_access` (CONTIGUOUS/INVARIANT/STRIDED/UNKNOWN with `coeff`,
`const_off`, `sym_div`), `word_aligned`, and `LoopAffineContext.varies` -- the
R4.2.8 machinery. This module composes those into ten named predicates.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vector_affine as _va
from vector_affine import (LoopAffineContext, classify_access, word_aligned,
                           CONTIGUOUS, INVARIANT, STRIDED, UNKNOWN)
from vector_capability import VectorCapability as _VectorCapability
import vector_capability_db as _vdb

WORD_BYTES = _vdb.WORD_BITS // 8

# One shared capability oracle; the database is the authority on which element
# types the ISA's $dot actually supports (e.g. it knows there is no 32-bit dot).
_CAPS = _VectorCapability()


def _cname(x):
    return type(x).__name__


# ── the representation ─────────────────────────────────────────────────────────

class MatmulAccess:
    """ONE resolved multiplicand access, in the generic form

        base_slot[invariant_base + IV * coeff]

    Every field is explicit so ACCEPT/REJECT can cite it. `invariant_base` is
    split into the parts the affine resolver can prove separately:

        const_off   compile-time byte constant
        sym_div     a positive integer that provably divides every symbolic
                    (non-constant, non-IV) byte term; 0 = no symbolic part,
                    1 = present but nothing proven
    """

    __slots__ = ('ok', 'reason', 'index', 'base_slot', 'iv_slot', 'coeff',
                 'elem_bytes', 'const_off', 'sym_div', 'kind', 'invariant_base',
                 'contiguous', 'aligned', 'signed')

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))
        if self.ok is None:
            self.ok = False

    def __repr__(self):
        if not self.ok:
            return f"MatmulAccess(REJECT: {self.reason})"
        return (f"MatmulAccess(slot={self.base_slot} + base(const={self.const_off},"
                f"symdiv={self.sym_div}) + IV*{self.coeff} eb={self.elem_bytes} "
                f"contig={self.contiguous} aligned={self.aligned})")


class MatmulForm:
    """The whole loop's decision, with every predicate result retained.

    `checks` is an ordered list of (predicate_name, ok, detail) so a report can
    show exactly which structural requirement failed, rather than one opaque
    reason string."""

    __slots__ = ('ok', 'reason', 'checks', 'accesses', 'iv_slot', 'trip',
                 'lanes', 'chunks', 'remainder', 'elem_bytes', 'vtype',
                 'signed', 'acc_slot', 'kind', 'dot_instr')

    def __init__(self):
        self.ok = False
        self.reason = None
        self.checks = []
        self.accesses = []

    def record(self, name, ok, detail=None):
        self.checks.append((name, bool(ok), detail))
        return ok

    def failed(self):
        return [(n, d) for n, o, d in self.checks if not o]

    def __repr__(self):
        if self.ok:
            return (f"MatmulForm(ACCEPT {self.vtype} lanes={self.lanes} "
                    f"trip={self.trip} chunks={self.chunks} rem={self.remainder})")
        return f"MatmulForm(REJECT: {self.reason})"


# ── the ten structural predicates ──────────────────────────────────────────────
#
# Each is an explicit function returning (ok, detail). None has a side effect on
# lowering; they are pure queries over the IR and the capability database.

def p1_counted_inner_iv(desc, kernel):
    """1. A counted inner-loop induction variable exists with a known trip."""
    if getattr(desc, 'primary_iv', None) is None:
        return False, 'no-primary-iv'
    if kernel.trip is None:
        return False, 'trip-unknown'
    if kernel.trip <= 0:
        return False, f'trip-{kernel.trip}-not-positive'
    return True, f'iv_slot={desc.primary_iv} trip={kernel.trip}'


def p2_same_k_iv(accesses, iv_slot):
    """2. Both multiplicand accesses are indexed by the SAME K induction
    variable. Two operands walking different IVs is not a dot product."""
    if len(accesses) < 2:
        return False, f'need-2-multiplicands-found-{len(accesses)}'
    bad = [a for a in accesses if a.iv_slot != iv_slot]
    if bad:
        return False, f'{len(bad)}-access(es)-not-on-iv-slot-{iv_slot}'
    return True, f'both-on-iv_slot={iv_slot}'


def p3_coeff_is_elem_bytes(accesses):
    """3. The K coefficient equals elem_bytes -- i.e. consecutive K touch
    consecutive elements. This is what makes a packed load legal, and it is the
    check that rejects an UN-transposed second operand (whose coeff is
    row_len*elem_bytes)."""
    for a in accesses:
        if a.coeff is None:
            return False, 'coeff-unresolved'
        if a.elem_bytes is None:
            return False, 'elem-bytes-unknown'
        if a.coeff != a.elem_bytes:
            return False, f'stride-{a.coeff}-not-elem-{a.elem_bytes}'
    return True, f'coeff==elem_bytes=={accesses[0].elem_bytes}'


def p4_invariant_base(accesses):
    """4. The row/base component is loop-invariant.

    The affine resolver only yields a coefficient when it has separated IV terms
    from non-IV terms; a base whose value moves with the loop makes the whole
    offset UNKNOWN rather than producing a wrong coefficient. This predicate
    states that requirement explicitly instead of relying on that side effect.

    HONEST STATUS: measured to be SUBSUMED by p6 in every case tested. A base
    that varies makes `resolve_offset` return UNKNOWN, so p6 fires first and p4
    never becomes the reported cause. It is retained as defence in depth and
    because the R13 specification requires the requirement to be an explicit
    predicate rather than an implicit side effect -- but it should not be
    described as load-bearing today. See `_r13_0_test.py`
    `test_negative_controls`, which asserts p6 for the varying-base control."""
    for a in accesses:
        if not a.invariant_base:
            return False, f'base-of-slot-{a.base_slot}-varies'
    return True, 'all-bases-loop-invariant'


def p5_base_stable_across_vector_loop(accesses, ctx):
    """5. The base stays stable across the WHOLE vector loop, not merely within
    one iteration: the loop must not STORE to the slot holding it (the M2
    question `LoopAffineContext.stored_slots` answers)."""
    for a in accesses:
        if a.base_slot in ctx.stored_slots:
            return False, f'slot-{a.base_slot}-written-inside-loop'
    return True, 'no-multiplicand-slot-written-in-loop'


def p6_no_runtime_varying_stride(accesses):
    """6. No hidden runtime-varying stride. A gather (`a[idx[k]]`) resolves to
    UNKNOWN, never to a constant coefficient; requiring a resolved, constant,
    non-negative coefficient rules it out."""
    for a in accesses:
        if a.kind == UNKNOWN:
            return False, f'unresolved-offset: {a.reason}'
        # NOTE: a resolved but WRONG constant stride (kind == STRIDED) is NOT
        # rejected here -- it is a compile-time constant, which is all this
        # predicate claims. p3 owns "is that constant equal to elem_bytes",
        # so the reported reason names the actual defect.
        if not isinstance(a.coeff, int):
            return False, 'coefficient-not-a-compile-time-integer'
    return True, 'all-strides-compile-time-constant'


def p7_packed_load_legal(form, legality):
    """7. Packed-load legality: the trip must contain at least one whole vector
    chunk. A trip smaller than one word cannot be lowered to a packed load at
    all -- this is the `4x4 vu8` case, and it is a legality answer, not a
    missing feature."""
    if not getattr(legality, "legal", False):
        return False, f'legality-{getattr(legality, "reason", "rejected")}'
    if form.lanes in (None, 0):
        return False, 'lanes-unknown'
    if form.chunks < 1:
        return False, (f'trip-{form.trip}-smaller-than-lanes-{form.lanes} '
                       f'(zero full chunks)')
    return True, (f'trip={form.trip} lanes={form.lanes} '
                  f'chunks={form.chunks} remainder={form.remainder}')


def p8_alignment(accesses):
    """8. Every packed access must be PROVABLY word-aligned. The IV term is
    aligned by construction once the lowering substitutes a multiple of the
    packed word, so this reduces to the invariant part -- exactly what
    `vector_affine.word_aligned` decides. Unproven alignment is a reject."""
    for a in accesses:
        if not a.aligned:
            return False, (f'slot-{a.base_slot}-base-not-provably-'
                           f'{WORD_BYTES}B-aligned '
                           f'(const_off={a.const_off}, sym_div={a.sym_div})')
    return True, f'all-bases-provably-{WORD_BYTES}B-aligned'


def p9_dot_capability(kernel):
    """9. The SOURCE datatype must be supported by the existing $dot capability.

    The element width is read from the source marker and is NEVER changed here
    to obtain more lanes -- an unsupported type is rejected cleanly. The
    capability database is the authority (it is what knows, for instance, that
    the ISA has no 32-bit $dot)."""
    vt = kernel.vtype
    if vt is None:
        return False, 'no-element-type'
    c = _CAPS.can('dot', vt, want_accumulate=True)
    if not c.ok:
        return False, f'{vt}:{c.reason}'
    e = _vdb.ELEMENT_TYPES.get(vt) or {}
    if not e.get('reliable', False):
        return False, f'{vt}:element-type-not-reliable'
    return True, f'{vt} -> {c.instr} lanes={c.lanes} accumulate={c.accumulate}'


def p10_reduction_semantics(kernel):
    """10. Accumulator/reduction semantics must match what the existing dot
    lowering implements: a single accumulator slot, summed, whose value is a
    product of two loads."""
    if kernel.reduction_slot is None:
        return False, 'no-reduction-slot'
    if kernel.reduction_value != 'dot':
        return False, f'reduction-value-{kernel.reduction_value}-not-dot'
    op = kernel.reduction_op
    if op not in ('+', None):
        return False, f'reduction-op-{op}-not-sum'
    return True, f'slot={kernel.reduction_slot} value=dot op={op or "+"}'


# ── driver ─────────────────────────────────────────────────────────────────────

def _multiplicand_loads(instrs, ctx, kernel):
    """The two loads whose product feeds the accumulator, or (None, reason).

    STRUCTURAL, not heuristic: the multiplicands are BY DEFINITION the operands
    of the multiply that updates the reduction slot. This mirrors
    `kernel_detector`'s own reduction walk (the code that decided
    `reduction_value == 'dot'`), so the pair found here is exactly the pair the
    detector classified -- rather than "every affine load in the body", which
    also sweeps up loop counters and other scalars.
    """
    slot = kernel.reduction_slot
    if slot is None:
        return None, 'no-reduction-slot'

    def loads_slot_zero(name):
        d = ctx.def_map.get(name)
        if d is None:
            return False
        ins = ctx.instrs[d]
        return (_cname(ins) == 'IRLoad' and isinstance(ins.base, _va.Temp)
                and ctx.addr_slot.get(ins.base.name) == slot
                and isinstance(ins.offset, _va.Const) and ins.offset.value == 0)

    for i in sorted(ctx.region):
        st = instrs[i]
        if _cname(st) != 'IRStore' or not isinstance(getattr(st, 'base', None), _va.Temp):
            continue
        if ctx.addr_slot.get(st.base.name) != slot:
            continue
        if not isinstance(st.src, _va.Temp):
            continue
        d = ctx.def_map.get(st.src.name)
        if d is None or _cname(instrs[d]) != 'IRBinOp' or instrs[d].op != '+':
            continue
        upd = instrs[d]
        L, R = upd.left, upd.right
        if isinstance(L, _va.Temp) and loads_slot_zero(L.name):
            V = R
        elif isinstance(R, _va.Temp) and loads_slot_zero(R.name):
            V = L
        else:
            continue
        if not isinstance(V, _va.Temp):
            return None, 'reduction-value-not-a-temp'
        dv = ctx.def_map.get(V.name)
        if dv is None or _cname(instrs[dv]) != 'IRBinOp' or instrs[dv].op != '*':
            return None, 'reduction-value-not-a-multiply'
        mul = instrs[dv]
        pair = []
        for operand in (mul.left, mul.right):
            if not isinstance(operand, _va.Temp):
                return None, 'multiplicand-not-a-temp'
            do = ctx.def_map.get(operand.name)
            if do is None or _cname(instrs[do]) != 'IRLoad':
                return None, 'multiplicand-not-a-load'
            pair.append(do)
        return pair, None
    return None, 'no-accumulator-store-found'


def _resolve_accesses(desc, instrs, kernel, ctx):
    """Resolve the two multiplicand loads into MatmulAccess records."""
    idxs, why = _multiplicand_loads(instrs, ctx, kernel)
    if idxs is None:
        return [], why
    out = []
    for i in idxs:
        ins = instrs[i]
        base = getattr(ins, 'base', None)
        slot = (ctx.addr_slot.get(base.name)
                if isinstance(base, _va.Temp) else None)
        acc = classify_access(ins, ctx)
        off = getattr(ins, 'offset', None)
        base_varies = (isinstance(off, _va.Temp)
                       and _base_component_varies(off, ctx))
        out.append(MatmulAccess(
            ok=acc.ok and acc.kind == CONTIGUOUS,
            reason=acc.reason or (None if acc.kind == CONTIGUOUS
                                  else f'kind-{acc.kind}'),
            index=i,
            base_slot=slot,
            iv_slot=ctx.iv_slot,
            coeff=acc.coeff,
            elem_bytes=acc.elem_bytes,
            const_off=acc.const_off,
            sym_div=acc.sym_div,
            kind=acc.kind,
            invariant_base=not base_varies,
            contiguous=(acc.kind == CONTIGUOUS),
            aligned=word_aligned(acc, WORD_BYTES),
            signed=not bool(getattr(ins, 'unsigned', False)),
        ))
    return out, None


def _base_component_varies(off, ctx):
    """Does the NON-IV part of this offset expression vary across iterations?

    `ctx.varies` reports True for anything reaching the IV, which every matmul
    offset does by construction. The question here is narrower: ignoring the IV
    itself, does anything else move? Walk the expression's sources and ask
    `varies` about each one that is not the IV."""
    d = ctx.def_map.get(off.name)
    if d is None:
        return False
    seen = set()
    stack = [off.name]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        if ctx.is_the_iv(n):
            continue                       # the IV term is expected to vary
        di = ctx.def_map.get(n)
        if di is None or di not in ctx.region:
            continue                       # computed outside the loop = invariant
        ins = ctx.instrs[di]
        if _cname(ins) == 'IRLoad':
            slot = ctx._slot_of_load(ins)
            if slot is None:
                return True                # unknown address: conservative
            if slot != ctx.iv_slot and slot in ctx.stored_slots:
                return True                # written by this loop
            continue
        for s in (_va.src_names(ins) or []):
            stack.append(s)
    return False


def analyze(desc, instrs, kernel, legality):
    """Decide whether one loop is expressible as `invariant_base + IV*eb` for
    both multiplicands. Returns a MatmulForm; NOTHING is mutated.

    The ten predicates run in a fixed order and every result is recorded, so a
    rejection names the specific structural requirement that failed."""
    form = MatmulForm()
    form.kind = kernel.kind
    form.vtype = kernel.vtype
    form.elem_bytes = kernel.elem_bytes
    form.signed = kernel.signed
    form.acc_slot = kernel.reduction_slot
    form.trip = kernel.trip
    form.lanes = getattr(legality, 'lanes', 0) or 0
    if form.lanes:
        form.chunks = (kernel.trip or 0) // form.lanes
        form.remainder = (kernel.trip or 0) % form.lanes
    else:
        form.chunks = form.remainder = 0

    ok, detail = p1_counted_inner_iv(desc, kernel)
    if not form.record('p1_counted_inner_iv', ok, detail):
        form.reason = f'p1:{detail}'
        return form

    ctx = LoopAffineContext(instrs, desc)
    form.iv_slot = ctx.iv_slot
    accesses, why = _resolve_accesses(desc, instrs, kernel, ctx)
    form.accesses = accesses
    if accesses == [] and why:
        form.record('p2_same_k_iv', False, why)
        form.reason = f'p2:{why}'
        return form

    ok, detail = p10_reduction_semantics(kernel)
    if not form.record('p10_reduction_semantics', ok, detail):
        form.reason = f'p10:{detail}'
        return form

    ok, detail = p2_same_k_iv(accesses, ctx.iv_slot)
    if not form.record('p2_same_k_iv', ok, detail):
        form.reason = f'p2:{detail}'
        return form

    ok, detail = p6_no_runtime_varying_stride(accesses)
    if not form.record('p6_no_runtime_varying_stride', ok, detail):
        form.reason = f'p6:{detail}'
        return form

    ok, detail = p3_coeff_is_elem_bytes(accesses)
    if not form.record('p3_coeff_is_elem_bytes', ok, detail):
        form.reason = f'p3:{detail}'
        return form

    ok, detail = p4_invariant_base(accesses)
    if not form.record('p4_invariant_base', ok, detail):
        form.reason = f'p4:{detail}'
        return form

    ok, detail = p5_base_stable_across_vector_loop(accesses, ctx)
    if not form.record('p5_base_stable_across_vector_loop', ok, detail):
        form.reason = f'p5:{detail}'
        return form

    ok, detail = p9_dot_capability(kernel)
    if not form.record('p9_dot_capability', ok, detail):
        form.reason = f'p9:{detail}'
        return form

    ok, detail = p7_packed_load_legal(form, legality)
    if not form.record('p7_packed_load_legal', ok, detail):
        form.reason = f'p7:{detail}'
        return form

    ok, detail = p8_alignment(accesses)
    if not form.record('p8_alignment', ok, detail):
        form.reason = f'p8:{detail}'
        return form

    form.dot_instr = _CAPS.instruction_for('dot', kernel.vtype,
                                          want_accumulate=True)
    form.ok = True
    return form


PREDICATES = ('p1_counted_inner_iv', 'p2_same_k_iv', 'p3_coeff_is_elem_bytes',
              'p4_invariant_base', 'p5_base_stable_across_vector_loop',
              'p6_no_runtime_varying_stride', 'p7_packed_load_legal',
              'p8_alignment', 'p9_dot_capability', 'p10_reduction_semantics')


# ── Phase 3: convertibility to the existing dot planner's internal form ────────
#
# ANALYSIS ONLY. Nothing here emits IR or calls a lowering. It answers: if the
# ten predicates accept, can the result be handed to the EXISTING dot planner
# and emitter, and what exactly is missing today?

#: Fields `vector_lowering.LoweringPlan` needs, and where R13's form supplies
#: them. 'form' = already available, 'shared' = computed by the existing planner
#: from `desc`/`kernel` with no matmul-specific input, 'EXTENSION' = the genuine
#: delta this milestone must add.
DOT_PLAN_FIELDS = {
    'kind':          'form',
    'vtype':         'form',
    'eb':            'form',
    'lanes':         'form',
    'signed':        'form',
    'trip':          'form',
    'chunks':        'form',
    'remainder':     'form',
    'acc_slot':      'form',
    'iv_slot':       'form',
    'array_slots':   'EXTENSION',
    'iv_bytes':      'shared',
    'acc_bytes':     'shared',
    'iv_init_site':  'shared',
    'region_lo':     'shared',
    'region_hi':     'shared',
    'peel':          'EXTENSION',
}


class PlanParity:
    """What converting `form` into a LoweringPlan would require."""

    __slots__ = ('ok', 'supplied', 'shared', 'extensions', 'notes')

    def __init__(self):
        self.supplied, self.shared, self.extensions, self.notes = [], [], [], []
        self.ok = False

    def __repr__(self):
        return (f"PlanParity(ok={self.ok} supplied={len(self.supplied)} "
                f"shared={len(self.shared)} extensions={len(self.extensions)})")


def dot_plan_parity(form, kernel=None):
    """Field-by-field parity between an accepted MatmulForm and LoweringPlan.

    The point of this function is to PROVE the delta is small and specific
    rather than to assert it: everything the existing dot lowering consumes is
    either already in the form, or already derived by the shared planner from
    `desc`/`kernel`, EXCEPT the array addressing -- which is the one thing this
    milestone exists to generalise."""
    par = PlanParity()
    if not form.ok:
        par.notes.append(f'form rejected: {form.reason}')
        return par

    for field, origin in sorted(DOT_PLAN_FIELDS.items()):
        if origin == 'form':
            par.supplied.append(field)
        elif origin == 'shared':
            par.shared.append(field)
        else:
            par.extensions.append(field)

    # The delta, stated concretely.
    for a in form.accesses:
        par.notes.append(
            f'array_slots: existing plan carries a bare slot ({a.base_slot}); '
            f'matmul additionally needs invariant base '
            f'(const_off={a.const_off}, sym_div={a.sym_div}) -- the '
            f'`invariant + IV*eb` form `gemm_lowering.clone_offset` already builds')

    # `need` is keyed on the kind and currently admits only dot-product as a
    # two-operand kernel (vector_lowering.py:215). matmul is also two-operand,
    # so that predicate must widen or the second multiplicand is silently
    # dropped -- a correctness trap, not a missed optimisation.
    par.notes.append(
        "vector_lowering.plan_lowering `need = 2 if kernel.kind in "
        "('dot-product',) else 1` must include 'matmul': a matmul has TWO "
        "multiplicands, and taking array_slots[:1] would silently drop one.")

    # The remainder peel replays the original loads at a constant index, so its
    # operand descriptors need the same base extension.
    par.notes.append(
        'vector_remainder_peel.PeelArray(slot, eb, unsigned) needs the same '
        'invariant-base extension, or a peeled tail would address row 0.')

    par.ok = True
    return par

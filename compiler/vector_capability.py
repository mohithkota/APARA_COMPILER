"""
vector_capability.py -- Vector Capability Layer (Milestone R4.0).

The reusable query API over the capability database (`vector_capability_db.py`).
Future vector passes ask THIS layer -- never hardcode ISA facts:

    cap = VectorCapability()
    cap.vector_type('int', signed=True)          -> 'vi32'
    cap.lanes('vi8')                              -> 8
    cap.can('reduce_sum', 'vi32')                 -> Capability(ok=True, instr='vreduce_sum', ...)
    cap.can('reduce_sum', 'vu8')                  -> Capability(ok=False, reason='unsigned-vreduce-buggy')
    cap.can('dot', 'vi8')                         -> Capability(ok=True, instr='dot', lanes=8, ...)
    cap.register_layout('load_wide', width=128)   -> ('aligned-register-group', 2)

Everything is a lookup into the database; no analysis, no IR.
"""

import vector_capability_db as _db


class Capability:
    """The answer to 'can this operation be vectorized on this element type?'."""
    __slots__ = ('ok', 'operation', 'vtype', 'instr', 'mnemonic', 'lanes',
                 'group', 'accumulate', 'reason')

    def __init__(self, ok, operation, vtype, instr=None, mnemonic=None, lanes=0,
                 group=None, accumulate=False, reason=None):
        self.ok = ok
        self.operation = operation
        self.vtype = vtype
        self.instr = instr
        self.mnemonic = mnemonic
        self.lanes = lanes
        self.group = group
        self.accumulate = accumulate
        self.reason = reason

    def __repr__(self):
        if self.ok:
            return (f"Capability(OK {self.operation}/{self.vtype} -> {self.mnemonic} "
                    f"x{self.lanes} lanes)")
        return f"Capability(NO {self.operation}/{self.vtype}: {self.reason})"


# operation name -> the database instruction key(s) that implement it
_OP_TO_INSTR = {
    'reduce_sum': ('vreduce_sum',),
    'reduce_max': ('vreduce_max',),
    'dot':        ('dot', 'dot128'),
    'add':        ('valu',),
    'sub':        ('valu',),
    'mul':        ('valu',),
    'load':       ('load_wide',),
    'store':      ('store_wide',),
}
_VALU_OP = {'add': '+', 'sub': '-', 'mul': '*'}


class VectorCapability:
    """Reusable capability queries. Stateless; cheap to construct."""

    # ── element types ─────────────────────────────────────────────────────────
    def vector_type(self, c_type, signed=None):
        """The vector tag for a C scalar element type, or None if it does not
        vectorize (unknown type, or a 64-bit element = single lane)."""
        c = c_type.replace('$', '').strip()
        for (name, sgn), tag in _db.C_TYPE_TO_VECTOR.items():
            if name == c and (signed is None or sgn == signed):
                return tag
        return None

    def is_element_type(self, vtype):
        return vtype in _db.ELEMENT_TYPES

    def lanes(self, vtype):
        """Packed elements per 64-bit register."""
        return _db.lane_count(vtype)

    def element_bits(self, vtype):
        e = _db.ELEMENT_TYPES.get(vtype)
        return e['bits'] if e else 0

    def is_reliable_type(self, vtype):
        e = _db.ELEMENT_TYPES.get(vtype)
        return bool(e and e['reliable'])

    # ── operation capability ──────────────────────────────────────────────────
    def can(self, operation, vtype, want_accumulate=False):
        """Can `operation` on element type `vtype` be implemented reliably?
        Returns a Capability (ok / reason)."""
        if operation not in _OP_TO_INSTR:
            return Capability(False, operation, vtype, reason='unknown-operation')
        if not self.is_element_type(vtype):
            return Capability(False, operation, vtype, reason='unknown-element-type')
        if not self.is_reliable_type(vtype):
            return Capability(False, operation, vtype, reason='element-type-toolchain-broken')

        for key in _OP_TO_INSTR[operation]:
            spec = _db.INSTRUCTIONS[key]
            if vtype not in spec['types']:
                continue
            # VALU: the specific arithmetic op must be supported
            if key == 'valu' and _VALU_OP.get(operation) not in spec['ops']:
                continue
            if want_accumulate and not spec.get('accumulate'):
                continue
            lanes = spec.get('lanes', self.lanes(vtype))
            return Capability(True, operation, vtype, instr=key,
                              mnemonic=spec['mnemonic'], lanes=lanes,
                              group=spec['group'],
                              accumulate=spec.get('accumulate', False))
        # present but not for this type -> pinpoint the reason
        if operation == 'reduce_sum' and vtype in ('vu8', 'vu16', 'vu32'):
            return Capability(False, operation, vtype, reason='unsigned-vreduce-buggy')
        if operation == 'dot' and vtype in ('vi32', 'vu32'):
            return Capability(False, operation, vtype, reason='no-32bit-dot')
        return Capability(False, operation, vtype, reason='type-not-supported')

    def instruction_for(self, operation, vtype, want_accumulate=False):
        """The database instruction key implementing `operation` on `vtype`, or
        None. (Convenience over `can`.)"""
        c = self.can(operation, vtype, want_accumulate)
        return c.instr if c.ok else None

    # ── layout / grouping ─────────────────────────────────────────────────────
    def register_layout(self, instr_key, width=None):
        """(grouping-kind, group-size) for an instruction. For wide load/store,
        `width` (128/256) selects the pair/quad group size."""
        spec = _db.INSTRUCTIONS.get(instr_key)
        if spec is None:
            return (None, 0)
        if 'widths' in spec and width is not None:
            return (spec['group'], spec['widths'].get(width, 0))
        return (spec['group'], 1)

    def max_lanes(self, vtype):
        """The widest reliable vector for `vtype`: a wide u128 dot reaches 16 u8
        lanes; otherwise one register's worth."""
        base = self.lanes(vtype)
        if vtype == 'vu8':                        # dot128 doubles it
            return max(base, _db.INSTRUCTIONS['dot128'].get('lanes', base))
        return base

    # ── introspection (for reports) ───────────────────────────────────────────
    def all_instructions(self):
        return dict(_db.INSTRUCTIONS)

    def known_broken(self):
        return dict(_db.KNOWN_BROKEN)

    def lane_caps(self):
        return dict(_db.LANE_CAPS)

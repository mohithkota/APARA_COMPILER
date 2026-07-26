"""
vector_capability_db.py -- APARA Vector ISA Capability Database (Milestone R4.0).

The SINGLE SOURCE OF TRUTH for what the APARA vector ISA can do, determined
DIRECTLY from the production implementation (never assumed):

  * codegen.py's vector emitters (`_gen_IRVecArith/Dot/Dot128/Reduce/LoadWide/
    StoreWide/Slice/Pack/Fsqrt`) -- what the backend actually emits;
  * ir_gen.py's intrinsic lowering -- the type tags and operand shapes actually
    produced;
  * golden_stubs.h -- the "no-bias" reference semantics (derived from isa.txt and
    hardware-confirmed in STATUS.md, explicitly NOT by mirroring the compiler);
  * STATUS.md -- the confirmed hardware bugs and the isa_coverage/fuzz1000
    validation record.

This module holds ONLY data (facts). The query layer (`vector_capability.py`) and
every future vector pass consult it; nothing hardcodes ISA knowledge elsewhere.

--------------------------------------------------------------------------------
LANE MODEL (golden_stubs.h __v_generic / __dot_generic / __vreduce_generic):
a 64-bit register holds  64 / element_bits  packed elements. A vector operation is
element-wise across those lanes of one (or a pair/quad of) 64-bit register(s).
"""

# ── element types ───────────────────────────────────────────────────────────────
# (tag -> element_bits, signed, reliable). vi4/vu4 are grammar-legal but
# toolchain-broken (STATUS.md E5), so reliable=False.
ELEMENT_TYPES = {
    'vi8':  {'bits': 8,  'signed': True,  'reliable': True},
    'vu8':  {'bits': 8,  'signed': False, 'reliable': True},
    'vi16': {'bits': 16, 'signed': True,  'reliable': True},
    'vu16': {'bits': 16, 'signed': False, 'reliable': True},
    'vi32': {'bits': 32, 'signed': True,  'reliable': True},
    'vu32': {'bits': 32, 'signed': False, 'reliable': True},
    'vi4':  {'bits': 4,  'signed': True,  'reliable': False},   # E5: toolchain-broken
    'vu4':  {'bits': 4,  'signed': False, 'reliable': False},
}

# C scalar type -> (vector tag) for the natural element type. Only integer element
# types <= 32 bits vectorize (a 64-bit element gives a 1-lane "vector" = no win).
C_TYPE_TO_VECTOR = {
    ('int8_t', True):  'vi8',  ('char', True):  'vi8',   ('signed char', True): 'vi8',
    ('uint8_t', False): 'vu8', ('unsigned char', False): 'vu8', ('vu8_t', False): 'vu8',
    ('int16_t', True):  'vi16', ('short', True): 'vi16',
    ('uint16_t', False): 'vu16', ('unsigned short', False): 'vu16',
    ('int32_t', True):  'vi32', ('int', True):   'vi32',
    ('uint32_t', False): 'vu32', ('unsigned int', False): 'vu32', ('unsigned', False): 'vu32',
}

# ── instructions ────────────────────────────────────────────────────────────────
# Each entry records: the mcode mnemonic, the IR node, the operations it supports,
# the element types it is RELIABLE on (hardware-confirmed, bug-free), any
# register-grouping requirement, and a short semantics note + provenance.

_ALL_INT = ('vi8', 'vu8', 'vi16', 'vu16', 'vi32', 'vu32')

INSTRUCTIONS = {
    'valu': {
        'mnemonic': '$v',
        'ir': 'IRVecArith',
        'ops': ('+', '-', '*'),                 # add / sub / mul (element-wise)
        'types': _ALL_INT,                      # signed/unsigned identical at bit level
        'replicate': True,                      # rs2 low element broadcast to all lanes
        'group': 'single',                      # one 64-bit register per operand
        'semantics': 'element-wise op across 64/nbits lanes',
        'validated': 'test_valu_full (STATUS.md 2026-06-20)',
    },
    'dot': {
        'mnemonic': '$dot',
        'ir': 'IRVecDot',
        'ops': ('dot',),                        # sum of element-wise products
        'types': ('vi8', 'vu8', 'vi16', 'vu16'),  # 8/16-bit confirmed; no 32-bit dot
        'accumulate': True,                     # $dot $accumulate chains into rd
        'group': 'single',
        'semantics': 'rd := sum_i(a[i]*b[i]) [+ rd if accumulate]; per-elem sign/zero-extend',
        'validated': 'test_dot_full (STATUS.md 2026-06-20, hardware-confirmed, no bug)',
    },
    'dot128': {
        'mnemonic': '$dot / $dot $accumulate (pair)',
        'ir': 'IRVecDot128',
        'ops': ('dot',),
        'types': ('vu8',),                      # 16xu8 confirmed from the 16x16 reference
        'accumulate': True,
        'group': 'pair-values',                 # lo=elems 0-7, hi=elems 8-15 (2 regs, no align)
        'lanes': 16,
        'semantics': '16-element dot across a value pair (lo/hi); two $dot instrs',
        'validated': 'test_dot128 / 16x16 reference (STATUS.md 2026-06-20)',
    },
    'vreduce_sum': {
        'mnemonic': '$vreduce +',
        'ir': 'IRVecReduce',
        'ops': ('+',),
        'types': ('vi8', 'vi16', 'vi32'),       # SIGNED only: unsigned vreduce is BUGGY
        'group': 'single',
        'semantics': 'horizontal sum of all lanes (sign-extended)',
        'validated': 'test_vreduce_full (signed); UNSIGNED sign-extends -> UNRELIABLE',
    },
    'vreduce_max': {
        'mnemonic': '$vreduce $max',
        'ir': 'IRVecReduce',
        'ops': ('$max',),
        'types': _ALL_INT,                      # MAX supported for all types
        'group': 'single',
        'semantics': 'horizontal max of all lanes',
        'validated': 'test_vreduce_full (fixed toolchain)',
    },
    'load_wide': {
        'mnemonic': '$ld ($u128) / ($u256)',
        'ir': 'IRLoadWide',
        'ops': ('load',),
        'widths': {128: 2, 256: 4},             # bits -> register-group size (pair/quad)
        'group': 'aligned-register-group',      # pair 2-aligned, quad 4-aligned (hardware)
        'semantics': 'contiguous 2/4-word load into an aligned register group',
        'validated': 'test wide ld/st (STATUS.md)',
    },
    'store_wide': {
        'mnemonic': '$st ($u128) / ($u256)',
        'ir': 'IRStoreWide',
        'ops': ('store',),
        'widths': {128: 2, 256: 4},
        'group': 'aligned-register-group',
        'semantics': 'contiguous 2/4-word store from an aligned register group',
        'validated': 'test wide ld/st (STATUS.md)',
    },
    'slice': {
        'mnemonic': '$slice',
        'ir': 'IRSlice',
        'ops': ('slice',),
        'group': 'single',
        'semantics': 'rd := rs[hi:lo], zero-extended',
        'validated': 'test_slice_full',
    },
    'pack': {
        'mnemonic': '$pack',
        'ir': 'IRPack',
        'ops': ('pack',),
        'group': 'src-consecutive-pair',        # rs2, rs2+1 consecutive
        'semantics': 'arg1 -> high word_nbits, arg2 -> low word_nbits',
        'validated': 'test_pack_full',
    },
    'fsqrt': {
        'mnemonic': '$fsqrt',
        'ir': 'IRFsqrt',
        'ops': ('fsqrt',),
        'ftypes': ('$f64', '$f32', '$f16', '$f8', '$f4'),
        'group': 'single',
        'semantics': 'floating-point square root',
        'validated': 'test_fsqrt',
    },
}

# ── confirmed unsupported / broken (must never be emitted by a vector pass) ──────
KNOWN_BROKEN = {
    'vi4/vu4 (4-bit lanes)':      'grammar-legal but toolchain-broken (STATUS.md E5)',
    'unsigned $vreduce sum':      'sign-extends lanes instead of zero-extending '
                                  '(McodeOperations.cpp __vreduce_operation__)',
    '$vreduce min/mul/or/xor/and': 'return 0 in the simulator (only + and $max wired)',
    'native $abs/$max/$min':      'scalar ALU has no MAX/MIN case; encoder rejects; '
                                  'lowered to $cmov instead',
    '32-bit $dot':                'not exposed / unconfirmed (golden_stubs defines dot '
                                  'only for 8/16-bit element types)',
}

# ── hardware resource facts (mirror bundler / codegen; not re-derived) ───────────
# Bundle lane caps that bound vector throughput, from the bundler resource model.
LANE_CAPS = {'total': 8, 'mem': 4, 'div_sqrt': 1, 'ctl': 1}
REGISTER_POOL = 28                              # allocatable registers (r0/r26-28 fixed)
WORD_BITS = 64


def lane_count(vtype):
    """Number of packed elements in one 64-bit register for `vtype`."""
    e = ELEMENT_TYPES.get(vtype)
    return (WORD_BITS // e['bits']) if e else 0

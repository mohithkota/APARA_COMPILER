"""
APARA Compiler — pycparser AST → Three-Address IR
"""

import pycparser
import pycparser.c_ast as A
from ir import *

# ── C type → APARA type string ────────────────────────────────────────────────

_CTYPE_TO_APARA = {
    'char': '$i8',          'signed char': '$i8',      'int8_t':  '$i8',
    'unsigned char': '$u8', 'uint8_t': '$u8',
    'short': '$i16',        'short int': '$i16',        'int16_t': '$i16',
    'signed short': '$i16', 'signed short int': '$i16',
    'unsigned short': '$u16', 'unsigned short int': '$u16', 'uint16_t': '$u16',
    'int':   '$i32',        'signed int': '$i32',       'int32_t': '$i32',
    'signed': '$i32',
    'unsigned int': '$u32', 'unsigned': '$u32',         'uint32_t': '$u32',
    'long':  '$i32',        'long int': '$i32',         'signed long': '$i32',
    'unsigned long': '$u32', 'unsigned long int': '$u32',
    'long long': '$i64',    'long long int': '$i64',    'int64_t': '$i64',
    'signed long long': '$i64', 'signed long long int': '$i64',
    'unsigned long long': '$u64', 'unsigned long long int': '$u64', 'uint64_t': '$u64',
    'float':  '$f32',       'float32_t': '$f32',
    'double': '$f64',       'float64_t': '$f64',
}

def _is_unsigned_decl(node):
    """True if this type AST node (or an array/pointer's element type) maps
    to an unsigned APARA type ($u8/$u16/$u32/$u64). Drives sign- vs
    zero-extension on load -- see IRLoad/IRGlobalLoad's `unsigned` flag.
    PtrDecl must recurse into the POINTEE type, not return early: a bare
    PtrDecl maps to '$u64' in _c_decl_to_apara_type regardless of what it
    points to (pointers are always unsigned 64-bit addresses), which would
    otherwise make every pointer name look "unsigned" and leak into p[i]'s
    element-access signedness, which actually depends on the pointee."""
    if isinstance(node, (A.ArrayDecl, A.PtrDecl)):
        return _is_unsigned_decl(node.type)
    return _c_decl_to_apara_type(node).startswith('$u')

def _c_decl_to_apara_type(node):
    """Return the APARA type string ($i8, $f32, …) for a C type AST node."""
    if node is None:
        return '$i64'
    if isinstance(node, A.TypeDecl):
        return _c_decl_to_apara_type(node.type)
    if isinstance(node, A.IdentifierType):
        name = ' '.join(node.names)
        return _CTYPE_TO_APARA.get(name, '$i64')
    if isinstance(node, A.PtrDecl):
        return '$u64'
    return '$i64'

# ── CMOV condition table ──────────────────────────────────────────────────────

_CMOV_INTRINSICS = {
    '__cmov_eq': '==', '__cmov_ne': '!=',
    '__cmov_gt': '>',  '__cmov_lt': '<',
    '__cmov_ge': '>=', '__cmov_le': '<=',
}

# ── Vector op table ───────────────────────────────────────────────────────────
_VOPS = {'add': '+', 'sub': '-', 'mul': '*'}

# Populated by IRGenerator._register_struct; allows _type_size to return correct struct sizes.
_STRUCT_TOTAL: dict = {}

# Populated by IRGenerator.visit_Typedef; allows _type_size to resolve scalar typedef
# names (e.g. "int64_t") to their real underlying size instead of silently defaulting
# to 4. pycparser does NOT expand a typedef's structure at its use site — a variable
# declared "int64_t x;" gets IdentifierType(names=['int64_t']), not the expanded
# "long long" — so without this table _type_size has no way to know int64_t is 8 bytes.
# Found 2026-06-17 via a real test failure (test_cast.c): see
# memory/feedback_elem_size_scalar_bug.md.
_TYPEDEF_SIZE: dict = {}

_BASE_TYPE_SIZES = {'char':1,'short':2,'int':4,'long':4,'long long':8,'float':4,'double':8}

def _type_size(node):
    if node is None: return 4
    if isinstance(node, A.TypeDecl):  return _type_size(node.type)
    if isinstance(node, A.PtrDecl):   return 4
    if isinstance(node, A.ArrayDecl):
        esz = _type_size(node.type)
        dim = int(node.dim.value) if node.dim else 0
        return esz * dim
    if isinstance(node, A.IdentifierType):
        name = ' '.join(node.names)
        base = name.replace('unsigned ', '').replace('signed ', '')
        if base in _BASE_TYPE_SIZES: return _BASE_TYPE_SIZES[base]
        if name in _TYPEDEF_SIZE:    return _TYPEDEF_SIZE[name]
        return 4
    if isinstance(node, A.Struct):    return _STRUCT_TOTAL.get(node.name or '', 8)
    if isinstance(node, A.Enum):      return 4
    return 4

def _elem_size(node):
    if isinstance(node, A.ArrayDecl): return _type_size(node.type)
    if isinstance(node, A.PtrDecl):   return 8   # pointer itself is always 8 bytes
    return _type_size(node)

# Opt-in marker typedefs (see compiler.py _FAKE_TYPEDEFS) requesting natural
# (packed, no 8-byte-per-element padding) array stride. Plain char/short/int
# arrays are NOT affected -- only arrays whose element is declared with one of
# these exact type names. long long/pointer/struct never go through this path.
_PACKED_ARRAY_TYPEDEFS = {'vu8_t', 'vi8_t', 'vu16_t', 'vi16_t', 'vu32_t', 'vi32_t'}

def _is_packed_array_decl(array_decl_node):
    """True if array_decl_node (an A.ArrayDecl) declares its element with one
    of the opt-in packed-array typedef markers."""
    if not isinstance(array_decl_node, A.ArrayDecl):
        return False
    t = array_decl_node.type
    if isinstance(t, A.TypeDecl):
        t = t.type
    return isinstance(t, A.IdentifierType) and any(
        n in _PACKED_ARRAY_TYPEDEFS for n in t.names)

class IRGenerator(pycparser.c_ast.NodeVisitor):
    DEFAULT_GLOBAL_BASE = 0x400

    def __init__(self, global_base=None):
        self.instructions  = []
        self._lbl_n        = 0
        self._func_name    = None
        self._global_base  = global_base or self.DEFAULT_GLOBAL_BASE
        self._next_global  = self._global_base
        self._globals      = {}
        self._frame_off    = 0
        self._var_offsets  = {}
        self._array_elem   = {}
        # name -> stride for global 1D arrays. Separate from _array_elem because
        # that dict is reset per-function (visit_FuncDef) for local scoping, which
        # would otherwise wipe out global registrations the moment any function is
        # visited. Mirrors _array_row_stride, which already persists this way for
        # global 2D arrays.
        self._global_array_elem = {}
        # name -> True if declared unsigned (char/short/int, or an array of
        # those). NOT reset per-function like _array_elem -- instead every
        # visit_Decl explicitly add()s or discard()s its own name, so a
        # same-named local in a later function always overwrites any stale
        # entry rather than relying on a reset that would also wipe globals
        # (the exact bug _global_array_elem above was already split out to
        # avoid).
        self._unsigned_vars = set()
        # name → elem_bytes for the variable's OWN scalar load/store width
        # (set by _alloc_local; used by _load_var/_store_var for the local-scalar path).
        self._local_elem_bytes = {}
        self._scopes       = [{}]
        self._break_to     = None
        self._cont_to      = None
        self._str_n        = 0
        self._func_names   = set()   # all C function definition names (for function pointers)
        # Maps variable name → DMEM stride per element for pointer arithmetic.
        # All pointer types currently use stride=8 (one 8-byte DMEM slot per element).
        self._ptr_stride      = {}
        # 2D array info: name → (outer_dim, inner_dim)
        self._array_dims      = {}
        # 2D array info: name → row stride in bytes (inner_dim * col_stride)
        self._array_row_stride = {}
        # Struct support
        # name → {field: (byte_offset, dmem_size, sub_struct_name_or_None)}
        self._struct_layouts      = {}
        # name → total DMEM bytes (mirrors _STRUCT_TOTAL but instance-local for clarity)
        self._struct_total_dmem   = {}
        # variable name → struct type name (for '.' access on struct vars)
        self._var_struct_type     = {}
        # variable name → struct type name (for '->' access via pointer-to-struct)
        self._var_struct_ptr_type = {}

    def _emit(self, i):    self.instructions.append(i)
    def _tmp(self):        return Temp()
    def _lbl(self, p='L'):
        self._lbl_n += 1; return f"{p}_{self._lbl_n}"

    def _push_scope(self): self._scopes.append({})
    def _pop_scope(self):  self._scopes.pop()

    def _define(self, name, kind, info):
        self._scopes[-1][name] = (kind, info)

    def _lookup(self, name):
        for scope in reversed(self._scopes):
            if name in scope: return scope[name]
        return None

    def _alloc_global(self, name, total_bytes, elem_bytes, init_vals, packed=False):
        # APARA: $ld ($i32) always reads bits[63:32] of the 8-byte DMEM word.
        # Each element must be at byte_off=0 of its own word → stride = 8.
        # EXCEPT for opt-in `packed` arrays (see _is_packed_array_decl): these use
        # the natural element size, no padding, so $ld ($u128)/($u256) can see N
        # tightly-packed bytes as N real vector elements. Default unchanged.
        #
        # Use len(init_vals) as the ground truth for n_elems when available,
        # because _elem_size() returns 4 for ALL scalar TypeDecl nodes (including
        # long long), so total_bytes // elem_bytes would give 2 for a long long scalar.
        if init_vals:
            n_elems = len(init_vals)
        else:
            n_elems = max(1, total_bytes // max(elem_bytes, 1))
        c_elem = total_bytes // max(n_elems, 1)  # actual C element size
        dmem_stride = c_elem if packed else max(c_elem, 8)
        total_dmem = n_elems * dmem_stride
        addr = self._next_global
        self._next_global += total_dmem
        # elem_bytes is the C type size (drives instruction type $i32 vs $i64)
        # stride is the DMEM allocation stride (always 8 for APARA alignment)
        gd = IRGlobalDecl(name, addr, total_dmem, elem_bytes, init_vals, stride=dmem_stride)
        self._globals[name] = gd
        self._emit(gd)
        self._define(name, 'global', gd)
        if n_elems > 1:
            self._global_array_elem[name] = dmem_stride  # bare array name in a call
            # now decays to its address instead of loading element 0 (see __init__)
        return gd

    def _alloc_local(self, name, total_bytes, elem_bytes, packed=False):
        # Each element gets its own 8-byte-aligned DMEM word so byte_off=0 always,
        # except for opt-in `packed` arrays -- see _alloc_global for why.
        n_elems = max(1, total_bytes // max(elem_bytes, 1))
        dmem_stride = elem_bytes if packed else max(elem_bytes, 8)
        total_dmem = n_elems * dmem_stride
        self._frame_off += total_dmem
        fp_off = -self._frame_off
        self._var_offsets[name] = fp_off
        self._local_elem_bytes[name] = elem_bytes
        if elem_bytes != total_bytes:
            self._array_elem[name] = dmem_stride  # DMEM stride for index computation
        self._define(name, 'local', fp_off)
        return fp_off

    def _load_var(self, name):
        info = self._lookup(name)
        if info is None:
            if name in self._func_names:
                res = self._tmp()
                self._emit(IRFuncAddr(res, name))
                return res
            return Const(0)
        kind, loc = info
        unsigned = name in self._unsigned_vars
        if kind == 'global':
            gd = loc
            res = self._tmp()
            self._emit(IRGlobalLoad(res, gd.dmem_addr, Const(0), elem_bytes=gd.elem_bytes,
                                     unsigned=unsigned))
            return res
        else:
            addr = self._tmp(); val = self._tmp()
            eb = self._local_elem_bytes.get(name, 8)
            self._emit(IRLoadAddr(addr, loc))
            self._emit(IRLoad(val, addr, Const(0), eb, unsigned=unsigned))
            return val

    def _store_var(self, name, val):
        info = self._lookup(name)
        if info is None: return
        kind, loc = info
        if kind == 'global':
            gd = loc
            self._emit(IRGlobalStore(gd.dmem_addr, Const(0), val, gd.elem_bytes))
        else:
            addr = self._tmp()
            eb = self._local_elem_bytes.get(name, 8)
            self._emit(IRLoadAddr(addr, loc))
            self._emit(IRStore(addr, Const(0), val, eb))

    def _addr_of_var(self, name):
        info = self._lookup(name)
        if info is None: return Const(0)
        kind, loc = info
        if kind == 'global':
            gd = loc
            res = self._tmp()
            self._emit(IRGlobalAddrOf(res, gd.dmem_addr))
            return res
        else:
            addr = self._tmp()
            self._emit(IRLoadAddr(addr, loc))
            return addr

    # ── pointer helpers ───────────────────────────────────────────────────────

    def _ptr_stride_of_node(self, ast_node):
        """
        Return DMEM element stride if ast_node is a pointer-typed expression, else 0.
        Currently all pointer types share stride=8 (APARA alignment constraint).
        """
        if isinstance(ast_node, A.ID) and ast_node.name in self._ptr_stride:
            return self._ptr_stride[ast_node.name]
        if isinstance(ast_node, A.UnaryOp) and ast_node.op == '&':
            return 8  # address-of always produces a pointer
        return 0

    def _scale_by_stride(self, val, stride):
        """Multiply val (Const or Temp) by stride for pointer arithmetic."""
        if stride <= 1:
            return val
        if isinstance(val, Const):
            return Const(val.value * stride)
        scaled = self._tmp()
        self._emit(IRBinOp(scaled, '*', val, Const(stride)))
        return scaled

    def _record_ptr(self, name, ctype_node):
        """If ctype_node is a PtrDecl, record name as a pointer in _ptr_stride."""
        if isinstance(ctype_node, A.PtrDecl):
            pointed = ctype_node.type  # the pointed-to type node
            self._ptr_stride[name] = 8  # DMEM stride is always 8 for now

    # ── struct helpers ────────────────────────────────────────────────────────

    def _register_struct(self, struct_node):
        """Build DMEM layout for a struct; idempotent."""
        name = struct_node.name or ''
        if name and name in self._struct_layouts:
            return
        layout = {}
        offset = 0
        for decl in (struct_node.decls or []):
            if not decl.name:
                continue
            fname = decl.name
            ftype = decl.type
            sub_struct = None
            # Check for embedded struct field
            inner = ftype
            while isinstance(inner, (A.PtrDecl, A.ArrayDecl)):
                inner = inner.type
            if isinstance(inner, A.TypeDecl) and isinstance(inner.type, A.Struct):
                sn = inner.type
                if sn.decls:
                    self._register_struct(sn)
                sub_struct = sn.name or ''
                fdmem = self._struct_total_dmem.get(sub_struct, 8)
            elif isinstance(ftype, A.ArrayDecl):
                # Array field: each element gets 8-byte slot
                n_f = int(ftype.dim.value) if ftype.dim else 1
                esz_f = max(_elem_size(ftype), 8)
                fdmem = n_f * esz_f
            elif isinstance(ftype, A.PtrDecl):
                fdmem = 8  # pointer = one 8-byte slot
            else:
                fdmem = 8  # scalar: one 8-byte slot regardless of C type
            layout[fname] = (offset, fdmem, sub_struct)
            offset += fdmem
        if not name:
            return
        self._struct_layouts[name]    = layout
        self._struct_total_dmem[name] = offset if offset > 0 else 8
        _STRUCT_TOTAL[name]           = self._struct_total_dmem[name]

    def _record_struct_var(self, var_name, type_node):
        """Record struct-type and ptr-to-struct-type info for a variable."""
        if isinstance(type_node, A.TypeDecl) and isinstance(type_node.type, A.Struct):
            sn = type_node.type
            if sn.decls:
                self._register_struct(sn)
            if sn.name:
                self._var_struct_type[var_name] = sn.name
        elif isinstance(type_node, A.PtrDecl):
            inner = type_node.type
            if isinstance(inner, A.TypeDecl) and isinstance(inner.type, A.Struct):
                sn = inner.type
                if sn.decls:
                    self._register_struct(sn)
                if sn.name:
                    self._var_struct_ptr_type[var_name] = sn.name

    def _ptr_struct_type_of(self, expr_node):
        """Return the struct name that expr_node points to, or ''."""
        if isinstance(expr_node, A.ID):
            return self._var_struct_ptr_type.get(expr_node.name, '')
        return ''

    def _structref_base_and_total_off(self, node):
        """
        For a StructRef node, compute (base_addr_temp, total_byte_offset_int,
        field_dmem_size, sub_struct_name_or_None).

        Works for '.' (direct) and '->' (pointer), and chains recursively.
        """
        field_name = node.field.name

        if node.type == '->':
            struct_name = self._ptr_struct_type_of(node.name)
            base = self._visit_expr(node.name)
            parent_off = 0
        else:  # '.'
            if isinstance(node.name, A.ID):
                var_name = node.name.name
                struct_name = self._var_struct_type.get(var_name, '')
                base = self._addr_of_var(var_name)
                parent_off = 0
            elif isinstance(node.name, A.StructRef):
                # Chained: accumulate offset from parent
                base, parent_off, _, sub = self._structref_base_and_total_off(node.name)
                struct_name = sub or ''
            else:
                return Const(0), 0, 8, None

        layout = self._struct_layouts.get(struct_name, {})
        if field_name in layout:
            rel_off, fdmem, sub_struct = layout[field_name]
        else:
            rel_off, fdmem, sub_struct = 0, 8, None

        total_off = parent_off + rel_off
        return base, total_off, fdmem, sub_struct

    def _structref_read(self, node):
        base, off, fdmem, _ = self._structref_base_and_total_off(node)
        res = self._tmp()
        self._emit(IRLoad(res, base, Const(off), fdmem))
        return res

    def _structref_write(self, node, val):
        base, off, fdmem, _ = self._structref_base_and_total_off(node)
        self._emit(IRStore(base, Const(off), val, fdmem))

    # ── 2D array helpers ──────────────────────────────────────────────────────

    def _2d_base_and_offset(self, node):
        """
        Compute (base_addr_temp, byte_offset_temp) for A[i][j].
        node: ArrayRef(name=ArrayRef(name=ID('A'), subscript=i), subscript=j)
        """
        row_node = node.name                    # A[i]
        arr_name = row_node.name.name           # 'A'
        row_idx  = self._visit_expr(row_node.subscript)   # i
        col_idx  = self._visit_expr(node.subscript)       # j

        row_stride = self._array_row_stride.get(arr_name, 8)
        col_stride = self._array_elem.get(arr_name, 8)

        # row_off = i * row_stride
        if isinstance(row_idx, Const):
            row_off = Const(row_idx.value * row_stride)
        else:
            row_off = self._tmp()
            self._emit(IRBinOp(row_off, '*', row_idx, Const(row_stride)))

        # col_off = j * col_stride
        if isinstance(col_idx, Const):
            col_off = Const(col_idx.value * col_stride)
        else:
            col_off = self._tmp()
            self._emit(IRBinOp(col_off, '*', col_idx, Const(col_stride)))

        # total_off = row_off + col_off
        if isinstance(row_off, Const) and isinstance(col_off, Const):
            total_off = Const(row_off.value + col_off.value)
        elif isinstance(row_off, Const) and row_off.value == 0:
            total_off = col_off
        elif isinstance(col_off, Const) and col_off.value == 0:
            total_off = row_off
        else:
            total_off = self._tmp()
            self._emit(IRBinOp(total_off, '+', row_off, col_off))

        # Base address of the 2D array
        info = self._lookup(arr_name)
        if info:
            kind, loc = info
            if kind == 'global':
                base = self._tmp()
                if arr_name in self._ptr_stride:
                    # Global pointer holding array address: load the pointer value
                    self._emit(IRGlobalLoad(base, loc.dmem_addr, Const(0), elem_bytes=loc.elem_bytes))
                else:
                    # Global 2D array: address of first element
                    self._emit(IRGlobalAddrOf(base, loc.dmem_addr))
            else:
                base = self._tmp()
                if arr_name in self._ptr_stride:
                    # Local 2D array param: value on stack IS the base pointer
                    addr = self._tmp()
                    self._emit(IRLoadAddr(addr, loc))
                    self._emit(IRLoad(base, addr, Const(0), 8))
                else:
                    # Local 2D array (inline on frame): use frame address directly
                    self._emit(IRLoadAddr(base, loc))
        else:
            base = self._tmp()
            self._emit(IRAssign(base, Const(0)))

        return base, total_off, col_stride

    def _2d_arrayref_read(self, node):
        base, off, col_stride = self._2d_base_and_offset(node)
        res = self._tmp()
        self._emit(IRLoad(res, base, off, col_stride))
        return res

    def _2d_arrayref_write(self, node, val):
        base, off, col_stride = self._2d_base_and_offset(node)
        self._emit(IRStore(base, off, val, col_stride))

    def visit_FileAST(self, node):
        Temp.reset()
        # Pre-collect all function definition names so function-pointer assignments like
        # "fp = add" correctly resolve "add" to an IRFuncAddr even before add is defined.
        for ext in node.ext:
            if isinstance(ext, A.FuncDef):
                self._func_names.add(ext.decl.name)
        for ext in node.ext: self.visit(ext)

    def visit_Typedef(self, node):
        """Register typedef'd struct types so _STRUCT_TOTAL is populated, and
        register every typedef's byte size in _TYPEDEF_SIZE so _type_size can
        resolve scalar typedef names (e.g. int64_t) instead of defaulting to 4."""
        if isinstance(node.type, A.TypeDecl) and isinstance(node.type.type, A.Struct):
            sn = node.type.type
            # Anonymous struct in typedef: give it the typedef name
            if sn.decls and not sn.name:
                sn.name = node.name
            if sn.decls:
                self._register_struct(sn)
                self._var_struct_type[node.name] = sn.name
        _TYPEDEF_SIZE[node.name] = _type_size(node.type)

    def visit_Decl(self, node):
        if node.name is None:
            # Standalone struct definition.
            # pycparser gives Decl(name=None, type=Struct(...)) — NOT TypeDecl(Struct(...)).
            tn = node.type
            if isinstance(tn, A.Struct) and tn.decls:
                self._register_struct(tn)
            elif isinstance(tn, A.TypeDecl) and isinstance(tn.type, A.Struct) and tn.type.decls:
                self._register_struct(tn.type)
            return
        # Skip function declarations (no body — just a prototype/forward declaration)
        if isinstance(node.type, A.FuncDecl): return
        init_vals = self._flatten_init(node.init) if node.init else []

        # Detect struct variable BEFORE computing total/esz so _STRUCT_TOTAL is populated.
        self._record_struct_var(node.name, node.type)
        is_struct_var = node.name in self._var_struct_type

        if _is_unsigned_decl(node.type):
            self._unsigned_vars.add(node.name)
        else:
            self._unsigned_vars.discard(node.name)

        total = _type_size(node.type)
        esz   = _elem_size(node.type)

        # Detect 2D array: type = ArrayDecl(dim=rows, type=ArrayDecl(dim=cols, type=T))
        if isinstance(node.type, A.ArrayDecl) and isinstance(node.type.type, A.ArrayDecl):
            outer_dim  = int(node.type.dim.value)      if node.type.dim      else 0
            inner_dim  = int(node.type.type.dim.value) if node.type.type.dim else 0
            col_stride = max(_type_size(node.type.type.type), 8)
            self._array_row_stride[node.name] = inner_dim * col_stride
            self._array_dims[node.name]       = (outer_dim, inner_dim)
            esz = col_stride   # per-scalar stride; alloc uses total//esz = rows*cols elems

        # For struct vars use esz=8 (each slot 8-byte); total already = struct_dmem from registry.
        if is_struct_var:
            esz = 8

        self._record_ptr(node.name, node.type)   # track pointer variables
        is_packed = _is_packed_array_decl(node.type)  # opt-in only; False for 2D/struct/ptr
        if self._func_name is None:
            self._alloc_global(node.name, total, esz, init_vals, packed=is_packed)
        else:
            fp_off = self._alloc_local(node.name, total, esz, packed=is_packed)
            if is_struct_var:
                self._array_elem.pop(node.name, None)  # struct is not an array
            if node.init:
                is_arr = isinstance(node.type, A.ArrayDecl)
                if is_struct_var and isinstance(node.init, A.InitList):
                    # Struct initializer: store each field at offset i*8
                    base = self._tmp()
                    self._emit(IRLoadAddr(base, fp_off))
                    for i, v in enumerate(init_vals):
                        self._emit(IRStore(base, Const(i * 8), Const(v), 8))
                elif is_arr and isinstance(node.init, A.InitList):
                    base = self._tmp()
                    self._emit(IRLoadAddr(base, fp_off))
                    for i, v in enumerate(init_vals):
                        self._emit(IRStore(base, Const(i * esz), Const(v), esz))
                elif not is_arr and not is_struct_var:
                    val = self._visit_expr(node.init)
                    addr = self._tmp()
                    self._emit(IRLoadAddr(addr, fp_off))
                    self._emit(IRStore(addr, Const(0), val, esz))

    def _flatten_init(self, init_node):
        if isinstance(init_node, A.Constant):
            try: return [int(init_node.value, 0)]
            except: return [0]
        if isinstance(init_node, A.InitList):
            vals = []
            for expr in init_node.exprs: vals.extend(self._flatten_init(expr))
            return vals
        if isinstance(init_node, A.UnaryOp) and init_node.op == '-':
            sub = self._flatten_init(init_node.expr)
            return [-v for v in sub]
        return [0]

    def visit_FuncDef(self, node):
        name = node.decl.name
        self._func_name = name
        self._frame_off = 0
        self._var_offsets = {}
        self._array_elem  = {}
        self._local_elem_bytes = {}
        Temp.reset()

        params_raw = []
        if (node.decl.type.args and node.decl.type.args.params):
            for p in node.decl.type.args.params:
                if isinstance(p, A.Decl) and p.name:
                    params_raw.append(p)

        self._push_scope()
        param_list = []
        for p in params_raw:
            # 2D array param: long long A[rows][cols] → decays to pointer at ABI level.
            # Allocate only 8 bytes (holds the base address passed by caller).
            if isinstance(p.type, A.ArrayDecl) and isinstance(p.type.type, A.ArrayDecl):
                inner_dim  = int(p.type.type.dim.value) if p.type.type.dim else 0
                col_stride = max(_type_size(p.type.type.type), 8)
                self._array_row_stride[p.name] = inner_dim * col_stride
                self._array_dims[p.name]       = (0, inner_dim)
                self._array_elem[p.name]       = col_stride
                fp_off = self._alloc_local(p.name, 8, 8)
                self._ptr_stride[p.name] = 8   # holds a pointer value
                param_list.append((p.name, fp_off))
                continue
            esz = _elem_size(p.type)
            fp_off = self._alloc_local(p.name, max(_type_size(p.type), 4), esz)
            param_list.append((p.name, fp_off))
            if isinstance(p.type, A.ArrayDecl):
                self._array_elem[p.name] = esz
            self._record_ptr(p.name, p.type)        # track pointer variables
            self._record_struct_var(p.name, p.type) # track struct / ptr-to-struct params

        begin = IRFuncBegin(name, param_list, {}, 0)
        self._emit(begin)

        if node.body: self.visit(node.body)

        # FIX applied here: Only emit implicit return if last instr is NOT already a return
        if not self.instructions or not isinstance(self.instructions[-1], IRReturn):
            self._emit(IRReturn(None))

        self._emit(IRFuncEnd(name))
        self._pop_scope()

        fs = (self._frame_off + 4 + 71) & ~7
        begin.var_offsets = dict(self._var_offsets)
        begin.frame_size  = fs
        begin.params      = param_list
        self._func_name = None

    def visit_Compound(self, node):
        self._push_scope()
        if node.block_items:
            for item in node.block_items: self.visit(item)
        self._pop_scope()

    def visit_Assignment(self, node):
        op = node.op
        rval = self._visit_expr(node.rvalue)
        if op == '=':
            self._assign_lval(node.lvalue, rval)
            return rval
        old  = self._visit_expr(node.lvalue)
        res  = self._tmp()
        base_op = op[:-1]   # '+=' → '+', '-=' → '-', etc.
        # Scale rval by pointer stride for pointer += / pointer -=
        actual_rval = rval
        if base_op in ('+', '-') and isinstance(node.lvalue, A.ID):
            stride = self._ptr_stride.get(node.lvalue.name, 0)
            if stride > 1:
                actual_rval = self._scale_by_stride(rval, stride)
        self._emit(IRBinOp(res, base_op, old, actual_rval))
        self._assign_lval(node.lvalue, res)
        return res

    def _assign_lval(self, lval, val):
        if isinstance(lval, A.ID):
            self._store_var(lval.name, val)
        elif isinstance(lval, A.StructRef):
            self._structref_write(lval, val)
        elif isinstance(lval, A.ArrayRef):
            if isinstance(lval.name, A.ArrayRef) and isinstance(lval.name.name, A.ID):
                self._2d_arrayref_write(lval, val)
                return
            base, off = self._array_base_off(lval)
            if isinstance(lval.name, A.ID):
                name = lval.name.name
                info = self._lookup(name)
                if info and info[0] == 'global' and name not in self._ptr_stride:
                    # Global array (not pointer): use elem_bytes from global decl
                    gd = info[1]
                    self._emit(IRStore(base, off, val, gd.elem_bytes))
                    return
            # Pointer variable or local array: store to computed address
            eb = self._get_esz(lval.name) if isinstance(lval.name, A.ID) else 8
            self._emit(IRStore(base, off, val, eb))
        elif isinstance(lval, A.UnaryOp) and lval.op == '*':
            # All pointer types currently use stride=8 (see _record_ptr) — *p is
            # always an 8-byte dereference until pointer-to-narrow-type is tracked.
            ptr = self._visit_expr(lval.expr)
            self._emit(IRStore(ptr, Const(0), val, 8))

    def visit_If(self, node):
        t_lbl = self._lbl('if_t'); e_lbl = self._lbl('if_e')
        if node.iffalse:
            f_lbl = self._lbl('if_f')
            self._emit_cond(node.cond, t_lbl, f_lbl)
            self._emit(IRLabel(t_lbl)); self.visit(node.iftrue)
            self._emit(IRJump(e_lbl))
            self._emit(IRLabel(f_lbl)); self.visit(node.iffalse)
        else:
            self._emit_cond(node.cond, t_lbl, e_lbl)
            self._emit(IRLabel(t_lbl)); self.visit(node.iftrue)
        self._emit(IRLabel(e_lbl))

    def visit_While(self, node):
        cond_lbl = self._lbl('wc'); body_lbl = self._lbl('wb'); end_lbl = self._lbl('we')
        ob, oc = self._break_to, self._cont_to
        self._break_to, self._cont_to = end_lbl, cond_lbl
        self._emit(IRLabel(cond_lbl))
        self._emit_cond(node.cond, body_lbl, end_lbl)
        self._emit(IRLabel(body_lbl)); self.visit(node.stmt)
        self._emit(IRJump(cond_lbl))
        self._emit(IRLabel(end_lbl))
        self._break_to, self._cont_to = ob, oc

    def visit_DoWhile(self, node):
        body_lbl = self._lbl('dw_b'); cond_lbl = self._lbl('dw_c'); end_lbl = self._lbl('dw_e')
        ob, oc = self._break_to, self._cont_to
        self._break_to, self._cont_to = end_lbl, cond_lbl
        self._emit(IRLabel(body_lbl)); self.visit(node.stmt)
        self._emit(IRLabel(cond_lbl))
        self._emit_cond(node.cond, body_lbl, end_lbl)
        self._emit(IRLabel(end_lbl))
        self._break_to, self._cont_to = ob, oc

    def visit_For(self, node):
        cond_lbl=self._lbl('fc'); body_lbl=self._lbl('fb'); incr_lbl=self._lbl('fi'); end_lbl=self._lbl('fe')
        ob, oc = self._break_to, self._cont_to
        self._break_to, self._cont_to = end_lbl, incr_lbl
        if node.init:
            if isinstance(node.init, A.DeclList):
                for d in node.init.decls: self.visit(d)
            else: self._visit_expr(node.init)
        self._emit(IRLabel(cond_lbl))
        if node.cond: self._emit_cond(node.cond, body_lbl, end_lbl)
        else: self._emit(IRJump(body_lbl))
        self._emit(IRLabel(body_lbl)); self.visit(node.stmt)
        self._emit(IRLabel(incr_lbl))
        if node.next: self._visit_expr(node.next)
        self._emit(IRJump(cond_lbl))
        self._emit(IRLabel(end_lbl))
        self._break_to, self._cont_to = ob, oc

    def visit_Switch(self, node):
        sw_val  = self._visit_expr(node.cond)
        end_lbl = self._lbl('sw_end')
        ob = self._break_to; self._break_to = end_lbl
        case_items   = []
        default_item = None
        compound = node.stmt
        items = compound.block_items if isinstance(compound, A.Compound) and compound.block_items else []
        for item in items:
            if isinstance(item, A.Case):
                lbl = self._lbl('case')
                case_items.append((lbl, item.expr, item.stmts or []))
            elif isinstance(item, A.Default):
                lbl = self._lbl('dflt')
                default_item = (lbl, item.stmts or [])

        for lbl, expr, stmts in case_items:
            cv = Const(int(expr.value, 0)) if isinstance(expr, A.Constant) else self._visit_expr(expr)
            diff = self._tmp()
            self._emit(IRBinOp(diff, '-', sw_val, cv))
            self._emit(IRCondJump(diff, '==', Const(0), lbl))

        if default_item: self._emit(IRJump(default_item[0]))
        else: self._emit(IRJump(end_lbl))

        for lbl, expr, stmts in case_items:
            self._emit(IRLabel(lbl))
            for stmt in stmts: self.visit(stmt)

        if default_item:
            self._emit(IRLabel(default_item[0]))
            for stmt in default_item[1]: self.visit(stmt)

        self._emit(IRLabel(end_lbl))
        self._break_to = ob

    def visit_FuncCall(self, node):
        """Handle standalone function-call statements like __nop(); f(x);"""
        self._visit_expr(node)

    def visit_UnaryOp(self, node):
        """Handle standalone unary-op statements like i++; i--;"""
        self._visit_expr(node)

    def visit_Break(self, node):
        if self._break_to: self._emit(IRJump(self._break_to))

    def visit_Continue(self, node):
        if self._cont_to: self._emit(IRJump(self._cont_to))

    def visit_Return(self, node):
        val = self._visit_expr(node.expr) if node.expr else None
        self._emit(IRReturn(val))

    def generic_visit(self, node):
        for _, c in node.children(): self.visit(c)

    def _visit_expr(self, node):
        if node is None: return Const(0)
        if isinstance(node, A.Constant):
            raw = node.value
            # Strip C literal suffixes (u,U,l,L,f,F) — but only on non-hex literals
            # because hex digits include 'f'/'F' (e.g. 0x0F must not become '0x0')
            if raw.startswith(('0x', '0X')):
                raw = raw.rstrip('uUlL')   # hex: strip only integer suffixes
            else:
                raw = raw.rstrip('uUlLfF') # decimal/float: strip all suffixes
            try: return Const(int(raw, 0))
            except: return Const(ord(raw.strip("'"))) if raw.startswith("'") else Const(0)
        if isinstance(node, A.ID): return self._load_var(node.name)
        if isinstance(node, A.BinaryOp): return self._binop(node)
        if isinstance(node, A.UnaryOp): return self._unary(node)
        if isinstance(node, A.Assignment): return self.visit_Assignment(node)
        if isinstance(node, A.FuncCall): return self._call(node)
        if isinstance(node, A.StructRef): return self._structref_read(node)
        if isinstance(node, A.ArrayRef):
            if isinstance(node.name, A.ArrayRef) and isinstance(node.name.name, A.ID):
                return self._2d_arrayref_read(node)
            return self._arrayref(node)
        if isinstance(node, A.Cast):
            expr_val  = self._visit_expr(node.expr)
            dest_type = _c_decl_to_apara_type(node.to_type.type if node.to_type else None)
            if dest_type == '$i64':
                return expr_val   # no narrowing needed
            res = self._tmp()
            # IRCast(dest, src, dest_type, src_type) maps straight to mcode
            # text "$cast (dest_type) rd (src_type) rs". The simulator's
            # scalar-cast execution (___cast_operation___, McodeOperations.cpp)
            # masks/sign-or-zero-extends using the SECOND type tag's width and
            # unsigned flag, not the first -- confirmed by tracing
            # Break_Vector(src_type.nbits, ...): with src_type=$i64 (64 bits)
            # this is always a no-op regardless of the narrow C-level dest
            # type, which is why (unsigned char)(-1) silently passed through
            # as -1 before this fix. So the narrow C type must go in the
            # SECOND mcode position (src_type here) with a literal $i64 in
            # the first -- backwards from the natural "cast FROM i64 TO u8"
            # reading, but it's what actually makes the hardware narrow and
            # sign/zero-extend correctly.
            self._emit(IRCast(res, expr_val, '$i64', dest_type))
            return res
        if isinstance(node, A.TernaryOp): return self._ternary(node)
        if isinstance(node, A.ExprList):
            r = Const(0)
            for e in node.exprs: r = self._visit_expr(e)
            return r
        if isinstance(node, A.Constant) and node.type == 'string':
            return self._string_literal(node.value)
        return Const(0)

    def _binop(self, node):
        op = node.op
        if op == '&&':
            res = self._tmp(); fl = self._lbl('andF'); el = self._lbl('andE')
            l = self._visit_expr(node.left)
            self._emit(IRCondJump(l, '==', Const(0), fl))
            r = self._visit_expr(node.right)
            self._emit(IRCondJump(r, '==', Const(0), fl))
            self._emit(IRAssign(res, Const(1))); self._emit(IRJump(el))
            self._emit(IRLabel(fl)); self._emit(IRAssign(res, Const(0)))
            self._emit(IRLabel(el)); return res
        if op == '||':
            res = self._tmp(); tl = self._lbl('orT'); el = self._lbl('orE')
            l = self._visit_expr(node.left)
            self._emit(IRCondJump(l, '!=', Const(0), tl))
            r = self._visit_expr(node.right)
            self._emit(IRCondJump(r, '!=', Const(0), tl))
            self._emit(IRAssign(res, Const(0))); self._emit(IRJump(el))
            self._emit(IRLabel(tl)); self._emit(IRAssign(res, Const(1)))
            self._emit(IRLabel(el)); return res

        l = self._visit_expr(node.left); r = self._visit_expr(node.right); res = self._tmp()
        if op in ('+', '-'):
            # Pointer arithmetic: scale the integer side by the element stride
            l_stride = self._ptr_stride_of_node(node.left)
            r_stride = self._ptr_stride_of_node(node.right) if op == '+' else 0
            if l_stride > 1:
                r = self._scale_by_stride(r, l_stride)
            elif r_stride > 1:
                l = self._scale_by_stride(l, r_stride)
        if op in ('+','-','*','/','%','&','|','^','<<','>>'):
            self._emit(IRBinOp(res, op, l, r))
        elif op in ('>','<','>=','<=','==','!='):
            tl = self._lbl('cT'); el = self._lbl('cE')
            self._emit(IRCondJump(l, op, r, tl))
            self._emit(IRAssign(res, Const(0))); self._emit(IRJump(el))
            self._emit(IRLabel(tl)); self._emit(IRAssign(res, Const(1)))
            self._emit(IRLabel(el))
        return res

    def _unary(self, node):
        op = node.op
        if op in ('p++','p--'):
            old = self._visit_expr(node.expr); res = self._tmp(); new = self._tmp()
            self._emit(IRAssign(res, old))
            stride = self._ptr_stride_of_node(node.expr)
            delta  = Const(stride if stride > 1 else 1)
            self._emit(IRBinOp(new, '+' if op=='p++' else '-', old, delta))
            self._assign_lval(node.expr, new)
            return res
        if op in ('++','--'):
            old = self._visit_expr(node.expr); res = self._tmp()
            stride = self._ptr_stride_of_node(node.expr)
            delta  = Const(stride if stride > 1 else 1)
            self._emit(IRBinOp(res, '+' if op=='++' else '-', old, delta))
            self._assign_lval(node.expr, res)
            return res
        if op == '-':
            v = self._visit_expr(node.expr); res = self._tmp()
            self._emit(IRBinOp(res, '-', Const(0), v)); return res
        if op == '~':
            v = self._visit_expr(node.expr); res = self._tmp()
            self._emit(IRBinOp(res, '^', v, Const(-1))); return res
        if op == '!':
            v = self._visit_expr(node.expr); res = self._tmp()
            tl = self._lbl('nT'); el = self._lbl('nE')
            self._emit(IRCondJump(v, '==', Const(0), tl))
            self._emit(IRAssign(res, Const(0))); self._emit(IRJump(el))
            self._emit(IRLabel(tl)); self._emit(IRAssign(res, Const(1)))
            self._emit(IRLabel(el)); return res
        if op == '&':
            if isinstance(node.expr, A.ID) and node.expr.name in self._func_names:
                res = self._tmp()
                self._emit(IRFuncAddr(res, node.expr.name))
                return res
            if isinstance(node.expr, A.ID):
                return self._addr_of_var(node.expr.name)
            if isinstance(node.expr, A.StructRef):
                # &s.field or &p->field → base_addr + field_offset
                base, off, _, _ = self._structref_base_and_total_off(node.expr)
                if off == 0:
                    return base
                res = self._tmp()
                self._emit(IRBinOp(res, '+', base, Const(off)))
                return res
            if isinstance(node.expr, A.ArrayRef):
                # &arr[i]  →  base_address + element_offset
                base, off = self._array_base_off(node.expr)
                res = self._tmp()
                if isinstance(off, Const) and off.value == 0:
                    self._emit(IRAssign(res, base))
                else:
                    self._emit(IRBinOp(res, '+', base, off))
                return res
            return Const(0)
        if op == '*':
            # All pointer types currently use stride=8 (see _record_ptr) — *p is
            # always an 8-byte dereference until pointer-to-narrow-type is tracked.
            ptr = self._visit_expr(node.expr); res = self._tmp()
            self._emit(IRLoad(res, ptr, Const(0), 8)); return res
        return self._visit_expr(node.expr)

    def _call(self, node):
        # Determine direct vs indirect call.
        # Indirect if: call through a variable (fp(args)) or explicit deref ((*fp)(args)).
        func_ptr_expr = None
        if isinstance(node.name, A.ID):
            fname = node.name.name
            # It's a variable (function pointer) if it's in scope as a local/global,
            # AND it's not a known function name (direct call takes priority).
            if fname not in self._func_names and self._lookup(fname) is not None:
                func_ptr_expr = node.name
                fname = None
        elif isinstance(node.name, A.UnaryOp) and node.name.op == '*':
            fname = None
            func_ptr_expr = node.name.expr   # (*fp)(args) — strip the dereference
        else:
            fname = None
            func_ptr_expr = node.name

        # Build arg list; array names decay to their base address (C semantics)
        args = []
        for a in (node.args.exprs if node.args else []):
            if isinstance(a, A.ID):
                name = a.name
                info = self._lookup(name)
                if info:
                    kind, loc = info
                    is_arr = (name in self._array_elem or name in self._array_row_stride
                              or name in self._global_array_elem)
                    is_ptr = name in self._ptr_stride
                    if is_arr and not is_ptr:  # raw array: pass address of first element
                        tmp = self._tmp()
                        if kind == 'global':
                            self._emit(IRGlobalAddrOf(tmp, loc.dmem_addr))
                        else:
                            self._emit(IRLoadAddr(tmp, loc))
                        args.append(tmp)
                        continue
            args.append(self._visit_expr(a))

        res   = self._tmp()

        # Indirect call — emit now before intrinsic checks (which use fname)
        if func_ptr_expr is not None:
            func_ptr = self._visit_expr(func_ptr_expr)
            self._emit(IRIndirectCall(res, func_ptr, args))
            return res

        # ── NOR / NAND / XNOR ────────────────────────────────────────────────
        if fname == '__nor'  and len(args) >= 2:
            self._emit(IRBinOp(res, '~|', args[0], args[1])); return res
        if fname == '__nand' and len(args) >= 2:
            self._emit(IRBinOp(res, '~&', args[0], args[1])); return res
        if fname == '__xnor' and len(args) >= 2:
            self._emit(IRBinOp(res, '~^', args[0], args[1])); return res

        # ── NOP ───────────────────────────────────────────────────────────────
        if fname == '__nop':
            self._emit(IRNop()); return Const(0)

        # ── Wide u128/u256 load round-trip (load mechanics only) ────────────────
        # __ld128(dst, src): one $ld ($u128) into a register pair, then two plain
        # 64-bit stores of the halves to dst[0]/dst[8].
        # __ld256(dst, src): same, $ld ($u256) into a register quad, four stores
        # to dst[0]/dst[8]/dst[16]/dst[24]. Proves the load+aligned-group
        # allocation mechanism; no vector op involved yet.
        if fname in ('__ld128', '__ld256') and len(args) >= 2:
            dst_addr, src_addr = args[0], args[1]
            n = 2 if fname == '__ld128' else 4
            dests = [self._tmp() for _ in range(n)]
            self._emit(IRLoadWide(dests, src_addr, Const(0)))
            for i, d in enumerate(dests):
                self._emit(IRStore(dst_addr, Const(i * 8), d, 8))
            return Const(0)

        # __st128(dst, src): mirrors __ld128 in reverse -- two plain 64-bit
        # loads of src[0]/src[8], then one $st ($u128) writing both halves to
        # dst in one instruction.
        # __st256(dst, src): same, four loads from src[0..24], one $st ($u256).
        # Proves the store+aligned-group mechanism; no vector op involved.
        if fname in ('__st128', '__st256') and len(args) >= 2:
            dst_addr, src_addr = args[0], args[1]
            n = 2 if fname == '__st128' else 4
            srcs = []
            for i in range(n):
                t = self._tmp()
                self._emit(IRLoad(t, src_addr, Const(i * 8), 8))
                srcs.append(t)
            self._emit(IRStoreWide(srcs, dst_addr, Const(0)))
            return Const(0)

        # ── Float sqrt ────────────────────────────────────────────────────────
        _FSQRT = {
            'sqrt': '$f64', '__fsqrt_f64': '$f64',
            'sqrtf': '$f32', '__fsqrt_f32': '$f32',
            '__fsqrt_f16': '$f16', '__fsqrt_f8': '$f8', '__fsqrt_f4': '$f4',
        }
        if fname in _FSQRT and len(args) >= 1:
            self._emit(IRFsqrt(res, args[0], _FSQRT[fname])); return res

        # ── CMOV ─────────────────────────────────────────────────────────────
        if fname in _CMOV_INTRINSICS and len(args) >= 3:
            self._emit(IRCmov(res, args[0], _CMOV_INTRINSICS[fname],
                              args[1], args[2])); return res

        # ── SLICE ─────────────────────────────────────────────────────────────
        if fname == '__slice' and len(args) >= 3:
            hi = args[1].value if isinstance(args[1], Const) else 63
            lo = args[2].value if isinstance(args[2], Const) else 0
            self._emit(IRSlice(res, args[0], hi, lo)); return res

        # ── PACK ──────────────────────────────────────────────────────────────
        if fname == '__pack' and len(args) >= 4:
            rb = args[2].value if isinstance(args[2], Const) else 64
            sb = args[3].value if isinstance(args[3], Const) else 64
            self._emit(IRPack(res, args[0], args[1], rb, sb)); return res

        # ── Vector arithmetic:  __v{add,sub,mul}_{type}[_rep] ─────────────────
        if fname and fname.startswith('__v') and len(args) >= 2:
            rest = fname[3:]                       # "add_vi32" or "add_vi32_rep"
            replicate = rest.endswith('_rep')
            if replicate: rest = rest[:-4]
            parts = rest.split('_', 1)             # ['add', 'vi32']
            if len(parts) == 2 and parts[0] in _VOPS:
                op   = _VOPS[parts[0]]
                tstr = '$' + parts[1]
                self._emit(IRVecArith(res, op, args[0], args[1], tstr, replicate))
                return res

        # ── Fused 128-bit-wide DOT, load straight into the dot (no memory
        # round-trip): __dot128_direct_{type}(a_ptr, b_ptr) ─────────────────────
        # Loads both 128-bit operands directly into anonymous IR temps and feeds
        # them straight into IRVecDot128 -- no IRStore, no named C variable
        # anywhere in this lowering. Reuses IRLoadWide/IRVecDot128 exactly as
        # they already exist; this is purely a different ir_gen.py dispatch,
        # not new IR/codegen. Stays inside the single-expression,
        # register-resident path that already works for plain sub-expressions
        # (e.g. f(a+b)) -- deliberately does not touch the named-variable
        # memory model (every named local always round-trips through its own
        # stack slot; that is out of scope here, see STATUS.md 2026-06-19).
        if fname and fname.startswith('__dot128_direct_') and len(args) >= 2:
            tstr = '$' + fname[16:]
            a_lo, a_hi, b_lo, b_hi = self._tmp(), self._tmp(), self._tmp(), self._tmp()
            self._emit(IRLoadWide([a_lo, a_hi], args[0], Const(0)))
            self._emit(IRLoadWide([b_lo, b_hi], args[1], Const(0)))
            self._emit(IRVecDot128(res, a_lo, a_hi, b_lo, b_hi, tstr))
            return res

        # ── ONE-OFF, hand-written for a single measurement (NOT a general
        # pass): __dot128_batch4_vu8(a_ptr, b0,b1,b2,b3, c0,c1,c2,c3). Loads A
        # once and all 4 B operands FIRST (matching the reference's load1/
        # load2 batching, hitting the ISA's 4-loads-per-bundle ceiling), THEN
        # issues all 4 dot products against the already-loaded, still
        # register-resident halves, THEN stores each result straight to its
        # own final C[] address (not an intermediate buffer, so no
        # store-then-immediate-reload hazard on the results either). See
        # STATUS.md 2026-06-20 for why this exists and that it is scoped to
        # this one experiment, not meant to be generalized.
        if fname == '__dot128_batch4_vu8' and len(args) >= 9:
            a_ptr  = args[0]
            b_ptrs = args[1:5]
            c_ptrs = args[5:9]
            a_lo, a_hi = self._tmp(), self._tmp()
            self._emit(IRLoadWide([a_lo, a_hi], a_ptr, Const(0)))
            b_halves = []
            for bp in b_ptrs:
                b_lo, b_hi = self._tmp(), self._tmp()
                self._emit(IRLoadWide([b_lo, b_hi], bp, Const(0)))
                b_halves.append((b_lo, b_hi))
            for (b_lo, b_hi), cp in zip(b_halves, c_ptrs):
                dot_res = self._tmp()
                self._emit(IRVecDot128(dot_res, a_lo, a_hi, b_lo, b_hi, '$vu8'))
                self._emit(IRStore(cp, Const(0), dot_res, 8))
            return Const(0)

        # ── 128-bit-wide DOT: __dot128_{type}(a_lo, a_hi, b_lo, b_hi) ───────────
        # Auto-split into the exact two-instruction pattern confirmed from the
        # 16x16 reference: plain $dot on the lo halves, $dot $accumulate on hi.
        if fname and fname.startswith('__dot128_') and len(args) >= 4:
            tstr = '$' + fname[9:]
            self._emit(IRVecDot128(res, args[0], args[1], args[2], args[3], tstr))
            return res

        # ── DOT product:  __dot_{type}  /  __dot_acc_{type} ──────────────────
        if fname and fname.startswith('__dot_'):
            rest = fname[6:]                       # "vi4" or "acc_vi4"
            acc  = rest.startswith('acc_')
            if acc: rest = rest[4:]
            tstr = '$' + rest
            if acc and len(args) >= 3:
                self._emit(IRVecDot(res, args[1], args[2], tstr,
                                    accumulate=True, accum=args[0]))
            elif not acc and len(args) >= 2:
                self._emit(IRVecDot(res, args[0], args[1], tstr))
            return res

        # ── Vector REDUCE:  __vreduce_{type} ──────────────────────────────────
        if fname and fname.startswith('__vreduce_') and len(args) >= 1:
            tstr = '$' + fname[10:]
            self._emit(IRVecReduce(res, args[0], tstr)); return res

        # ── Default: regular function call ────────────────────────────────────
        self._emit(IRCall(res, fname, args))
        return res

    def _arrayref(self, node):
        base, off = self._array_base_off(node)
        res = self._tmp()
        is_id = isinstance(node.name, A.ID)
        unsigned = is_id and node.name.name in self._unsigned_vars
        if is_id:
            name = node.name.name
            info = self._lookup(name)
            if info and info[0] == 'global' and name not in self._ptr_stride:
                # Global array (not pointer): use direct global-load shortcut
                gd = info[1]
                self._emit(IRGlobalLoad(res, gd.dmem_addr, off, elem_bytes=gd.elem_bytes,
                                         unsigned=unsigned))
                return res
        # Pointer variable or local array: load from computed address
        eb = self._get_esz(node.name) if is_id else 8
        self._emit(IRLoad(res, base, off, eb, unsigned=unsigned))
        return res

    def _ternary(self, node):
        res = self._tmp(); tl=self._lbl('tT'); fl=self._lbl('tF'); el=self._lbl('tE')
        self._emit_cond(node.cond, tl, fl)
        self._emit(IRLabel(tl)); self._emit(IRAssign(res, self._visit_expr(node.iftrue)))
        self._emit(IRJump(el))
        self._emit(IRLabel(fl)); self._emit(IRAssign(res, self._visit_expr(node.iffalse)))
        self._emit(IRLabel(el)); return res

    def _string_literal(self, raw):
        s = raw.strip('"').encode('raw_unicode_escape').decode('unicode_escape') + '\x00'
        self._str_n += 1
        name = f"__str{self._str_n}"
        inits = [ord(c) for c in s]
        gd = self._alloc_global(name, len(inits), 1, inits)
        res = self._tmp()
        self._emit(IRGlobalAddrOf(res, gd.dmem_addr))
        return res

    def _get_esz(self, name_node):
        if isinstance(name_node, A.ID):
            name = name_node.name
            if name in self._array_elem:   return self._array_elem[name]
            if name in self._ptr_stride:   return self._ptr_stride[name]
            info = self._lookup(name)
            if info:
                kind, loc = info
                if kind == 'global': return loc.stride
        return 8

    def _array_base_off(self, node):
        esz  = self._get_esz(node.name)
        idx  = self._visit_expr(node.subscript)
        if esz == 1: off = idx
        elif isinstance(idx, Const): off = Const(idx.value * esz)
        else:
            off = self._tmp()
            self._emit(IRBinOp(off, '*', idx, Const(esz)))

        if isinstance(node.name, A.ID):
            name = node.name.name
            info = self._lookup(name)
            if info:
                kind, loc = info
                if kind == 'global':
                    if name in self._ptr_stride:
                        # p[i] where p is a global pointer: load pointer VALUE
                        base = self._tmp()
                        self._emit(IRGlobalLoad(base, loc.dmem_addr, Const(0), elem_bytes=loc.elem_bytes))
                    else:
                        # arr[i] where arr is a global array: get base address
                        base = self._tmp()
                        self._emit(IRGlobalAddrOf(base, loc.dmem_addr))
                    return base, off
                else:
                    if name in self._ptr_stride:
                        # p[i] where p is a local pointer: load pointer VALUE from stack
                        addr = self._tmp(); base = self._tmp()
                        self._emit(IRLoadAddr(addr, loc))
                        self._emit(IRLoad(base, addr, Const(0), 8))
                    else:
                        # arr[i] where arr is a local array: get base address
                        base = self._tmp()
                        self._emit(IRLoadAddr(base, loc))
                    return base, off
        base = self._visit_expr(node.name)
        return base, off

    def _emit_cond(self, cond, true_lbl, false_lbl):
        if isinstance(cond, A.BinaryOp) and cond.op in ('>','<','>=','<=','==','!='):
            l = self._visit_expr(cond.left)
            r = self._visit_expr(cond.right)
            self._emit(IRCondJump(l, cond.op, r, true_lbl, false_lbl))
            return
        val = self._visit_expr(cond)
        self._emit(IRCondJump(val, '!=', Const(0), true_lbl, false_lbl))

"""
APARA Compiler — pycparser AST → Three-Address IR
"""

import pycparser
import pycparser.c_ast as A
from ir import *

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
        return {'char':1,'short':2,'int':4,'long':4,'long long':8,'float':4,'double':8}.get(name.replace('unsigned ','').replace('signed ',''), 4)
    if isinstance(node, A.Struct):    return 64
    if isinstance(node, A.Enum):      return 4
    return 4

def _elem_size(node):
    if isinstance(node, A.ArrayDecl): return _type_size(node.type)
    if isinstance(node, A.PtrDecl):   return _type_size(node.type)
    return 4

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
        self._scopes       = [{}]
        self._break_to     = None
        self._cont_to      = None
        self._str_n        = 0

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

    def _alloc_global(self, name, total_bytes, elem_bytes, init_vals):
        total_bytes = (max(total_bytes, 4) + 3) & ~3
        addr = self._next_global
        self._next_global += total_bytes
        gd = IRGlobalDecl(name, addr, total_bytes, elem_bytes, init_vals)
        self._globals[name] = gd
        self._emit(gd)
        self._define(name, 'global', gd)
        return gd

    def _alloc_local(self, name, total_bytes, elem_bytes):
        total_bytes = (max(total_bytes, 4) + 3) & ~3
        self._frame_off += total_bytes
        fp_off = -self._frame_off
        self._var_offsets[name] = fp_off
        if elem_bytes != total_bytes:
            self._array_elem[name] = elem_bytes
        self._define(name, 'local', fp_off)
        return fp_off

    def _load_var(self, name):
        info = self._lookup(name)
        if info is None: return Const(0)
        kind, loc = info
        if kind == 'global':
            gd = loc
            res = self._tmp()
            self._emit(IRGlobalLoad(res, gd.dmem_addr, Const(0), gd.elem_bytes))
            return res
        else:
            addr = self._tmp(); val = self._tmp()
            self._emit(IRLoadAddr(addr, loc))
            self._emit(IRLoad(val, addr, Const(0)))
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
            self._emit(IRLoadAddr(addr, loc))
            self._emit(IRStore(addr, Const(0), val))

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

    def visit_FileAST(self, node):
        Temp.reset()
        for ext in node.ext: self.visit(ext)

    def visit_Decl(self, node):
        if node.name is None: return
        init_vals = self._flatten_init(node.init) if node.init else []
        total = _type_size(node.type)
        esz   = _elem_size(node.type)

        if self._func_name is None:
            self._alloc_global(node.name, total, esz, init_vals)
        else:
            fp_off = self._alloc_local(node.name, total, esz)
            if node.init:
                is_arr = isinstance(node.type, A.ArrayDecl)
                if is_arr and isinstance(node.init, A.InitList):
                    base = self._tmp()
                    self._emit(IRLoadAddr(base, fp_off))
                    for i, v in enumerate(init_vals):
                        self._emit(IRStore(base, Const(i * esz), Const(v), esz))
                elif not is_arr:
                    val = self._visit_expr(node.init)
                    addr = self._tmp()
                    self._emit(IRLoadAddr(addr, fp_off))
                    self._emit(IRStore(addr, Const(0), val))

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
        Temp.reset()

        params_raw = []
        if (node.decl.type.args and node.decl.type.args.params):
            for p in node.decl.type.args.params:
                if isinstance(p, A.Decl) and p.name:
                    params_raw.append(p)

        self._push_scope()
        param_list = []
        for p in params_raw:
            esz = _elem_size(p.type)
            fp_off = self._alloc_local(p.name, max(_type_size(p.type), 4), esz)
            param_list.append((p.name, fp_off))
            if isinstance(p.type, A.ArrayDecl):
                self._array_elem[p.name] = esz

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
        old = self._visit_expr(node.lvalue)
        res = self._tmp()
        self._emit(IRBinOp(res, op[:-1], old, rval))
        self._assign_lval(node.lvalue, res)
        return res

    def _assign_lval(self, lval, val):
        if isinstance(lval, A.ID):
            self._store_var(lval.name, val)
        elif isinstance(lval, A.ArrayRef):
            base, off = self._array_base_off(lval)
            esz = self._get_esz(lval.name)
            self._emit(IRStore(base, off, val, esz))
        elif isinstance(lval, A.UnaryOp) and lval.op == '*':
            ptr = self._visit_expr(lval.expr)
            self._emit(IRStore(ptr, Const(0), val))

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
            raw = node.value.rstrip('uUlLfF')
            try: return Const(int(raw, 0))
            except: return Const(ord(raw.strip("'"))) if raw.startswith("'") else Const(0)
        if isinstance(node, A.ID): return self._load_var(node.name)
        if isinstance(node, A.BinaryOp): return self._binop(node)
        if isinstance(node, A.UnaryOp): return self._unary(node)
        if isinstance(node, A.Assignment): return self.visit_Assignment(node)
        if isinstance(node, A.FuncCall): return self._call(node)
        if isinstance(node, A.ArrayRef): return self._arrayref(node)
        if isinstance(node, A.Cast): return self._visit_expr(node.expr)
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
            self._emit(IRBinOp(new, '+' if op=='p++' else '-', old, Const(1)))
            self._assign_lval(node.expr, new)
            return res
        if op in ('++','--'):
            old = self._visit_expr(node.expr); res = self._tmp()
            self._emit(IRBinOp(res, '+' if op=='++' else '-', old, Const(1)))
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
            if isinstance(node.expr, A.ID): return self._addr_of_var(node.expr.name)
            return Const(0)
        if op == '*':
            ptr = self._visit_expr(node.expr); res = self._tmp()
            self._emit(IRLoad(res, ptr, Const(0))); return res
        return self._visit_expr(node.expr)

    def _call(self, node):
        fname = node.name.name if isinstance(node.name, A.ID) else None
        args  = [self._visit_expr(a) for a in (node.args.exprs if node.args else [])]
        res   = self._tmp()
        self._emit(IRCall(res, fname, args))
        return res

    def _arrayref(self, node):
        base, off = self._array_base_off(node)
        esz = self._get_esz(node.name)
        res = self._tmp()
        if isinstance(node.name, A.ID):
            info = self._lookup(node.name.name)
            if info and info[0] == 'global':
                self._emit(IRGlobalLoad(res, info[1].dmem_addr, off, esz))
                return res
        self._emit(IRLoad(res, base, off, esz))
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
            info = self._lookup(name_node.name)
            if info:
                kind, loc = info
                if name_node.name in self._array_elem: return self._array_elem[name_node.name]
                if kind == 'global': return loc.elem_bytes
        return 4

    def _array_base_off(self, node):
        esz  = self._get_esz(node.name)
        idx  = self._visit_expr(node.subscript)
        if esz == 1: off = idx
        elif isinstance(idx, Const): off = Const(idx.value * esz)
        else:
            off = self._tmp()
            self._emit(IRBinOp(off, '*', idx, Const(esz)))

        if isinstance(node.name, A.ID):
            info = self._lookup(node.name.name)
            if info:
                kind, loc = info
                if kind == 'global':
                    base = self._tmp()
                    self._emit(IRGlobalAddrOf(base, loc.dmem_addr))
                    return base, off
                else:
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

"""Kiln type checker with local type inference.

Annotates the AST in place:
  expr.ty            semantic type of every expression
  expr.used          whether the value of the expression is consumed
  Ident.sym / LetStmt.sym / ForStmt.sym / Pattern.sym   resolved Symbol
  Call.target        ("user", FnInfo) | ("builtin", name)
  Call.cargs         final argument list (UFCS receiver first)
  FieldAccess.as_call  synthesized Call when `x.len` means `len(x)`
"""

from __future__ import annotations
import difflib
import re
from . import ast_nodes as A
from .errors import CompileError, SourceLoc
from .ktypes import (Ty, Prim, Arr, StructT, ModuleT, TVar, INT, FLOAT, BOOL, STR, UNIT, NEVER,
                    PTR, CSTR, I8, U8, I16, U16, I32, U32, I64, U64, SIZED_INTS,
                    prune, unify, same, is_numeric, is_integer, int_lit_fits)

TYPE_NAMES = {
    "Int": INT, "int": INT, "i64": INT, "I64": I64,
    "Float": FLOAT, "float": FLOAT, "f64": FLOAT,
    "Bool": BOOL, "bool": BOOL,
    "Str": STR, "str": STR, "String": STR,
    "Unit": UNIT, "unit": UNIT,
    "Ptr": PTR, "ptr": PTR,
    "CStr": CSTR, "cstr": CSTR,
    "I8": I8, "i8": I8, "U8": U8, "u8": U8,
    "I16": I16, "i16": I16, "U16": U16, "u16": U16,
    "I32": I32, "i32": I32, "U32": U32, "u32": U32,
    "U64": U64, "u64": U64,
}
UNSUPPORTED_TYPES = {
    "i128", "u128", "uint", "f32", "char", "byte", "any", "never",
}

BUILTINS = {
    "print", "println", "eprint", "eprintln", "str", "to_str", "len", "push", "pop",
    "clear", "contains", "sort", "reverse", "join", "sum", "split", "split_whitespace",
    "lines", "trim", "to_upper", "to_lower", "starts_with", "ends_with", "replace",
    "repeat", "find", "parse_int", "parse_float", "fill", "abs", "min", "max", "sqrt",
    "floor", "ceil", "sin", "cos", "pow", "exit", "panic", "assert", "ask", "read_line",
    "now_ms", "env", "fs.read_text", "fs.write_text", "fs.exists",
}

FMT_RE = re.compile(r"^(?:(.)?([<>^]))?(0)?(\d+)?(?:\.(\d+))?([dfxXbeo])?$")


class Symbol:
    def __init__(self, name: str, ty: Ty, mutable: bool, cname: str, kind: str, loc=None):
        self.name = name
        self.ty = ty
        self.mutable = mutable
        self.cname = cname
        self.kind = kind  # local | param | global
        self.loc = loc

    def __repr__(self):
        return f"Symbol({self.name}:{self.ty})"


class FnInfo:
    def __init__(self, name: str, params: list[tuple[str, Ty]], ret: Ty, decl: A.FnDecl,
                 cname: str | None = None, is_extern: bool = False, link_lib: str | None = None):
        self.name = name
        self.params = params
        self.ret = ret
        self.decl = decl
        self.is_extern = is_extern
        self.link_lib = link_lib
        if cname is not None:
            self.cname = cname
        elif is_extern:
            self.cname = decl.name  # C symbol as declared
        else:
            # sanitize qualified names for C: gfx.open -> f_gfx_open
            self.cname = "f_" + name.replace(".", "_")

    def sig(self) -> str:
        ps = ", ".join(repr(prune(t)) for _, t in self.params)
        return f"fn({ps}) -> {prune(self.ret)!r}"


def parse_fmt_spec(spec: str | None, loc) -> tuple | None:
    if spec is None:
        return None
    m = FMT_RE.match(spec)
    if not m:
        raise CompileError(f"invalid format spec `:{spec}` (expected e.g. `:>8`, `:<20`, `:.3f`, `:08`, `:x`)", loc)
    fill, align, zero, width, prec, typ = m.groups()
    return (fill or (" " if not zero else "0"), align or "", bool(zero),
            int(width) if width else 0, int(prec) if prec is not None else -1, typ or "")


class Checker:
    def __init__(self, filename: str = "<input>"):
        self.file = filename
        self.structs: dict[str, StructT] = {}
        self.struct_decls: dict[str, A.StructDecl] = {}
        self.fns: dict[str, FnInfo] = {}
        self.globals: dict[str, Symbol] = {}
        self.scopes: list[dict[str, Symbol]] = []
        self.cur_fn: FnInfo | None = None
        self.loops: list[dict] = []
        self.name_counts: dict[str, int] = {}
        self.global_counts: dict[str, int] = {}
        self.all_types: list[Ty] = []
        self.global_inits: list[A.LetStmt] = []
        self.tests: list[A.TestDecl] = []
        self.pending_tvars: list[tuple[TVar, SourceLoc]] = []
        self.link_libs: set[str] = set()
        self.module_binds: dict[str, str] = {}  # local alias -> module path
        self.module_exports: dict[str, dict] = {}  # module path -> {name: decl}
        self.extern_fns: list[FnInfo] = []
        self.module_fns: dict[str, dict] = {}
        self.module_globals: dict[str, dict] = {}
        self._body_module = None

    # ── helpers ────────────────────────────────────────────────────
    def err(self, msg: str, node_or_loc):
        loc = node_or_loc if isinstance(node_or_loc, SourceLoc) or node_or_loc is None else node_or_loc.loc
        raise CompileError(msg, loc)

    def record(self, t: Ty) -> Ty:
        self.all_types.append(t)
        return t

    def resolve_type(self, te: A.TypeExpr) -> Ty:
        if isinstance(te, A.ArrayType):
            return self.record(Arr(self.resolve_type(te.elem)))
        if isinstance(te, A.NamedType):
            if te.name in TYPE_NAMES:
                return TYPE_NAMES[te.name]
            if te.name in self.structs:
                return self.structs[te.name]
            if te.name in UNSUPPORTED_TYPES:
                self.err(f"type `{te.name}` not yet supported (use Int, Float, Bool, Str, sized ints, Ptr, CStr)", te)
            sugg = difflib.get_close_matches(te.name, list(TYPE_NAMES) + list(self.structs), n=1)
            hint = f" (did you mean `{sugg[0]}`?)" if sugg else ""
            self.err(f"unknown type `{te.name}`{hint}", te)
        self.err("unsupported type expression", te)

    def type_err(self, what: str, expected: Ty, got: Ty, node):
        self.err(f"{what}: expected `{prune(expected)!r}` but found `{prune(got)!r}`", node)

    def push_scope(self):
        self.scopes.append({})

    def pop_scope(self):
        self.scopes.pop()

    def fresh_cname(self, name: str, is_global: bool) -> str:
        counts = self.global_counts if is_global else self.name_counts
        n = counts.get(name, 0)
        counts[name] = n + 1
        safe = name.replace(".", "_")
        base = ("g_" if is_global else "v_") + safe
        return base if n == 0 else f"{base}_{n}"

    def declare(self, name: str, ty: Ty, mutable: bool, kind: str, loc) -> Symbol:
        is_global = not self.scopes
        sym = Symbol(name, ty, mutable, self.fresh_cname(name, is_global), kind if not is_global else "global", loc)
        if is_global:
            self.globals[name] = sym
        else:
            self.scopes[-1][name] = sym
        return sym

    def lookup(self, name: str) -> Symbol | None:
        for s in reversed(self.scopes):
            if name in s:
                return s[name]
        return self.globals.get(name)

    def all_names(self):
        names = set(self.globals) | set(self.fns) | BUILTINS
        for s in self.scopes:
            names |= set(s)
        return names

    # ── program ────────────────────────────────────────────────────
    def check_program(self, prog: A.Program, require_main: bool = True):
        # Module metadata from the loader (optional)
        mod_map = getattr(prog, "_kiln_modules", {}) or {}
        for mname, loaded in mod_map.items():
            self.module_exports[mname] = loaded.exports

        # Bind `use` aliases as module symbols (available as globals)
        for d in prog.decls:
            if isinstance(d, A.UseStmt):
                alias = d.alias or d.path.split(".")[-1]
                if d.path not in self.module_exports and d.path not in mod_map:
                    # allow use of a module that only contributes decls (exports may be empty)
                    self.module_exports.setdefault(d.path, {})
                self.module_binds[alias] = d.path
                # declare a module binding so `gfx` resolves
                if alias not in self.globals:
                    self.globals[alias] = Symbol(alias, ModuleT(d.path), False, "mod_" + alias, "module", d.loc)

        structs = [d for d in prog.decls if isinstance(d, A.StructDecl)]
        fns = [d for d in prog.decls if isinstance(d, A.FnDecl)]
        for d in structs:
            self.declare_struct_name(d)
        for d in structs:
            self.resolve_struct_fields(d)
        self.check_struct_cycles()
        for d in fns:
            self.declare_fn(d)
        for d in prog.decls:
            if isinstance(d, A.LetStmt):
                self.check_global_let(d)
        for d in fns:
            if d.is_extern:
                continue
            key = d.qual_name or d.name
            self.check_fn_body(self.fns[key])
        for d in prog.decls:
            if isinstance(d, A.TestDecl):
                self.check_test(d)
        if require_main:
            if "main" not in self.fns:
                self.err("no `fn main()` entry point found", SourceLoc(self.file, 1, 1))
            m = self.fns["main"]
            if len(m.params) > 1 or (m.params and not same(m.params[0][1], Arr(STR))):
                self.err("`main` must be `fn main()` or `fn main(args: [Str])`", m.decl)
            if prune(m.ret) not in (UNIT, INT):
                self.err("`main` must return nothing or an Int exit code", m.decl)
        self.finalize()

    def finalize(self):
        for tv, loc in self.pending_tvars:
            if not tv.is_concrete():
                self.err("cannot infer the element type of this empty list; add a type annotation, "
                         "e.g. `var xs: [Int] = []`", loc)
        self.pending_tvars = []

    def declare_struct_name(self, d: A.StructDecl):
        # Struct type name is the bare name (Kiln types are not currently qualified in annotations).
        # Modules should use distinct struct names, or we register under bare name.
        key = d.name
        if key in self.structs and self.struct_decls.get(key) is not d:
            if not getattr(self, "allow_redefine", False):
                self.err(f"struct `{d.name}` is already defined", d)
        if key in TYPE_NAMES:
            self.err(f"`{d.name}` is a built-in type name", d)
        st = StructT(d.name)
        self.structs[key] = st
        self.struct_decls[key] = d
        d.sty = st

    def resolve_struct_fields(self, d: A.StructDecl):
        st = self.structs[d.name]
        seen = set()
        st.fields = []
        for f in d.fields:
            if f.name in seen:
                self.err(f"duplicate field `{f.name}` in struct `{d.name}`", f)
            seen.add(f.name)
            st.fields.append((f.name, self.resolve_type(f.typ)))
        self.record(st)

    def check_struct_cycles(self):
        state: dict[str, int] = {}

        def visit(name, loc):
            if state.get(name) == 1:
                self.err(f"struct `{name}` contains itself by value (recursive structs need a list field, e.g. `[{name}]`)", loc)
            if state.get(name) == 2:
                return
            state[name] = 1
            for fname, ft in self.structs[name].fields:
                ft = prune(ft)
                if isinstance(ft, StructT):
                    visit(ft.name, self.struct_decls[name])
            state[name] = 2

        for name in self.structs:
            visit(name, self.struct_decls[name])

    def declare_fn(self, d: A.FnDecl):
        qname = d.qual_name or d.name
        if qname in self.fns and self.fns[qname].decl is not d and not getattr(self, "allow_redefine", False):
            self.err(f"function `{qname}` is already defined", d)
        if d.name in TYPE_NAMES or d.name in self.structs:
            self.err(f"`{d.name}` is a type name and cannot be a function name", d)
        params = []
        seen = set()
        for p in d.params:
            if p.name in seen:
                self.err(f"duplicate parameter `{p.name}`", p)
            seen.add(p.name)
            params.append((p.name, self.resolve_type(p.typ)))
        ret = self.resolve_type(d.ret_type) if d.ret_type else UNIT
        info = FnInfo(qname, params, ret, d, is_extern=d.is_extern, link_lib=d.link_lib)
        self.fns[qname] = info
        # Entry-file (no module) functions also register under bare name (= qname)
        if not d.module:
            self.fns[d.name] = info
        else:
            # Track per-module bare names for body checking
            self.module_fns.setdefault(d.module, {})[d.name] = info
        if d.is_extern:
            if d.link_lib:
                self.link_libs.add(d.link_lib)
            self.extern_fns.append(info)
        d.info = info
        return info

    def check_fn_body(self, info: FnInfo):
        d = info.decl
        if info.is_extern or d.body is None:
            return
        self._body_module = d.module
        saved_fns = None
        saved_globals = None
        if d.module and d.module in self.module_fns:
            # Overlay bare names from this module for local calls
            saved_fns = {}
            for bname, finfo in self.module_fns[d.module].items():
                saved_fns[bname] = self.fns.get(bname)
                self.fns[bname] = finfo
        if d.module and d.module in self.module_globals:
            saved_globals = {}
            for bname, sym in self.module_globals[d.module].items():
                saved_globals[bname] = self.globals.get(bname)
                self.globals[bname] = sym
        self.cur_fn = info
        self.name_counts = {}
        self.scopes = [{}]
        d.param_syms = []
        for (pname, pty), p in zip(info.params, d.params):
            d.param_syms.append(self.declare(pname, pty, False, "param", p))
        want = prune(info.ret) is not UNIT
        try:
            bt = self.check_block(d.body, used=want, expected=info.ret if want else None)
            if want:
                if prune(bt) is UNIT and self.block_ends_with_value(d.body):
                    self.err(f"function `{d.name}` must return `{prune(info.ret)!r}`, but some path reaches the end "
                             f"of the body without a value (add an `else` branch, a final expression, or `return`)",
                             d.body.stmts[-1])
                if bt is UNIT and not self.block_ends_with_value(d.body):
                    self.err(f"function `{d.name}` must return `{prune(info.ret)!r}`, but its body ends without a value "
                             f"(end with an expression or `return`)", d)
                if unify(info.ret, bt) is None:
                    last = d.body.stmts[-1] if d.body.stmts else d
                    self.type_err(f"function `{d.name}` returns the wrong type", info.ret, bt, last)
        finally:
            if saved_fns is not None:
                for bname, prev in saved_fns.items():
                    if prev is None:
                        self.fns.pop(bname, None)
                    else:
                        self.fns[bname] = prev
            if saved_globals is not None:
                for bname, prev in saved_globals.items():
                    if prev is None:
                        self.globals.pop(bname, None)
                    else:
                        self.globals[bname] = prev
        self.scopes = []
        self.cur_fn = None
        self._body_module = None
        self.finalize()

    def block_ends_with_value(self, b: A.Block) -> bool:
        return bool(b.stmts) and isinstance(b.stmts[-1], A.ExprStmt)

    def check_test(self, t: A.TestDecl):
        self.name_counts = {}
        self.scopes = [{}]
        self.cur_fn = FnInfo("<test>", [], UNIT, None)
        self.check_block(t.body, used=False)
        self.scopes = []
        self.cur_fn = None
        self.tests.append(t)
        self.finalize()

    def check_global_let(self, st: A.LetStmt):
        saved = self.scopes
        self.scopes = []
        self.cur_fn = None
        qn = getattr(st, "qual_name", None) or st.name
        saved_name = st.name
        st.name = qn
        # Overlay earlier globals from the same module under bare names
        overlay = {}
        mod = getattr(st, "module", None)
        if mod and mod in self.module_globals:
            for bname, sym in self.module_globals[mod].items():
                overlay[bname] = self.globals.get(bname)
                self.globals[bname] = sym
        try:
            self.check_let(st)
        finally:
            for bname, prev in overlay.items():
                if prev is None:
                    self.globals.pop(bname, None)
                else:
                    self.globals[bname] = prev
        st.name = saved_name
        if mod:
            self.module_globals.setdefault(mod, {})[saved_name] = st.sym
        self.scopes = saved
        self.global_inits.append(st)
        self.finalize()

    # ── statements ─────────────────────────────────────────────────
    def check_block(self, b: A.Block, used: bool, expected: Ty | None = None, new_scope: bool = True) -> Ty:
        if new_scope:
            self.push_scope()
        ty = UNIT
        diverges = False
        n = len(b.stmts)
        for i, s in enumerate(b.stmts):
            last = i == n - 1
            if isinstance(s, A.ExprStmt):
                t = self.check_expr(s.expr, expected=expected if last and used else None, used=used and last)
                if prune(t) is NEVER:
                    diverges = True
                if last:
                    ty = t if used else UNIT
            else:
                t = self.check_stmt(s)
                if t is NEVER:
                    diverges = True
        if new_scope:
            self.pop_scope()
        if diverges:
            ty = NEVER
        b.ty = ty
        b.used = used
        return ty

    def check_stmt(self, s) -> Ty:
        if isinstance(s, A.LetStmt):
            self.check_let(s)
            return UNIT
        if isinstance(s, A.ReturnStmt):
            if self.cur_fn is None:
                self.err("`return` outside of a function", s)
            rt = self.cur_fn.ret
            if s.value is None:
                if prune(rt) is not UNIT:
                    self.err(f"`return` needs a value of type `{prune(rt)!r}`", s)
            else:
                if prune(rt) is UNIT:
                    vt = self.check_expr(s.value, used=True)
                    if prune(vt) is not UNIT:
                        self.err(f"function `{self.cur_fn.name}` has no return type, but `return` has a value "
                                 f"(add `-> {prune(vt)!r}` to the signature)", s)
                else:
                    vt = self.check_expr(s.value, expected=rt, used=True)
                    if unify(rt, vt) is None:
                        self.type_err("wrong return type", rt, vt, s.value)
            return NEVER
        if isinstance(s, A.WhileStmt):
            self.expect_bool(s.cond, "`while` condition")
            self.loops.append({"break": False})
            self.check_block(s.body, used=False)
            self.loops.pop()
            if isinstance(s.cond, A.BoolLit) and s.cond.value:
                return UNIT
            return UNIT
        if isinstance(s, A.LoopStmt):
            self.loops.append({"break": False})
            self.check_block(s.body, used=False)
            info = self.loops.pop()
            return UNIT if info["break"] else NEVER
        if isinstance(s, A.ForStmt):
            return self.check_for(s)
        if isinstance(s, (A.BreakStmt, A.ContinueStmt)):
            if not self.loops:
                kw = "break" if isinstance(s, A.BreakStmt) else "continue"
                self.err(f"`{kw}` outside of a loop", s)
            if isinstance(s, A.BreakStmt):
                self.loops[-1]["break"] = True
            return NEVER
        if isinstance(s, A.GuardStmt):
            self.expect_bool(s.cond, "`guard` condition")
            t = self.check_block(s.else_body, used=False)
            if t is not NEVER:
                self.err("the `else` block of `guard` must exit (return, break, continue, panic, or exit)", s.else_body)
            return UNIT
        if isinstance(s, A.AssertStmt):
            self.expect_bool(s.cond, "`assert` condition")
            if s.msg is not None:
                mt = self.check_expr(s.msg, used=True)
                if prune(mt) is not STR:
                    self.type_err("assert message", STR, mt, s.msg)
            return UNIT
        if isinstance(s, A.ExprStmt):
            return self.check_expr(s.expr, used=False)
        if isinstance(s, A.FnDecl):
            self.err("nested functions not yet supported", s)
        self.err(f"unsupported statement {type(s).__name__}", s)

    def expect_bool(self, e, what):
        t = self.check_expr(e, expected=BOOL, used=True)
        if prune(t) is not BOOL:
            self.type_err(what, BOOL, t, e)

    def check_let(self, s: A.LetStmt):
        declared = self.resolve_type(s.typ) if s.typ else None
        vt = self.check_expr(s.value, expected=declared, used=True)
        if declared is not None:
            if unify(declared, vt) is None:
                self.type_err(f"type mismatch in `{s.name}`", declared, vt, s.value)
            ty = declared
        else:
            ty = vt
        pt = prune(ty)
        if pt is UNIT:
            if isinstance(s.value, A.IfExpr) and s.value.else_body is None:
                self.err("an `if` without `else` has no value; add an `else` branch", s.value)
            self.err(f"cannot bind `{s.name}` to an expression with no value (Unit)", s.value)
        if pt is NEVER:
            self.err(f"`{s.name}` would never be assigned (the expression always exits)", s.value)
        if s.name == "_":
            self.err("`_` cannot be used as a variable name", s)
        s.sym = self.declare(s.name, ty, s.mutable, "local", s)
        s.ty = ty

    def check_for(self, s: A.ForStmt) -> Ty:
        it = s.iter
        self.push_scope()
        if isinstance(it, A.RangeExpr):
            for part in (it.start, it.end):
                t = self.check_expr(part, expected=INT, used=True)
                if prune(t) is not INT:
                    self.type_err("range bound", INT, t, part)
            it.ty = INT
            s.kind = "range"
            elem = INT
        else:
            t = prune(self.check_expr(it, used=True))
            if isinstance(t, Arr):
                s.kind = "array"
                elem = t.elem
            elif t is STR:
                s.kind = "str"
                elem = STR
            else:
                self.err(f"cannot iterate over a value of type `{t!r}` (expected a range `a..b`, a list, or a Str)", it)
        s.sym = self.declare(s.var, elem, False, "local", s)
        self.loops.append({"break": False})
        self.check_block(s.body, used=False)
        self.loops.pop()
        self.pop_scope()
        return UNIT

    # ── expressions ────────────────────────────────────────────────
    def check_expr(self, e: A.Expr, expected: Ty | None = None, used: bool = True) -> Ty:
        m = getattr(self, "x_" + type(e).__name__, None)
        if m is None:
            self.err(f"unsupported expression {type(e).__name__}", e)
        e.used = used
        t = m(e, expected, used)
        e.ty = self.record(t)
        return t

    def x_IntLit(self, e, expected, used):
        exp = prune(expected) if expected is not None else None
        if exp is FLOAT:
            e.as_float = True
            return FLOAT
        if exp is PTR:
            e.as_ptr = True
            return PTR
        if exp in SIZED_INTS:
            if not int_lit_fits(e.value, exp):
                self.err(f"integer literal {e.value} does not fit in `{exp!r}`", e)
            e.sized_ty = exp
            return exp
        if not (-2**63 <= e.value < 2**63):
            self.err("integer literal does not fit in 64 bits", e)
        return INT

    def x_NullLit(self, e, expected, used):
        return PTR

    def x_FloatLit(self, e, expected, used):
        return FLOAT

    def x_BoolLit(self, e, expected, used):
        return BOOL

    def x_StrLit(self, e, expected, used):
        if e.parts is not None:
            new_parts = []
            for p in e.parts:
                if isinstance(p, str):
                    new_parts.append(p)
                    continue
                ex, spec, loc = p
                t = prune(self.check_expr(ex, used=True))
                self.expect_printable(t, ex)
                fs = parse_fmt_spec(spec, loc)
                if fs is not None:
                    if fs[5] in ("f", "e") and t not in (FLOAT, INT):
                        self.err(f"format `:{spec}` needs a number, found `{t!r}`", ex)
                    if fs[5] in ("x", "X", "b", "o", "d") and t is not INT:
                        self.err(f"format `:{spec}` needs an Int, found `{t!r}`", ex)
                new_parts.append((ex, fs, loc))
            e.parts = new_parts
        return STR

    def expect_printable(self, t, node):
        t = prune(t)
        if t is UNIT or t is NEVER or isinstance(t, ModuleT):
            self.err("this expression has no value to print", node)
        if not t.is_concrete():
            self.err("cannot infer the type of this value", node)

    def x_Ident(self, e, expected, used):
        sym = self.lookup(e.name)
        if sym is None:
            if e.name in self.fns or e.name in BUILTINS:
                self.err(f"`{e.name}` is a function; call it with `{e.name}(...)` "
                         f"(functions are not first-class values yet)", e)
            if e.name in self.structs:
                self.err(f"`{e.name}` is a struct type; construct it with `{e.name} {{ field: value, ... }}`", e)
            sugg = difflib.get_close_matches(e.name, list(self.all_names()), n=1)
            hint = f" (did you mean `{sugg[0]}`?)" if sugg else ""
            self.err(f"unknown name `{e.name}`{hint}", e)
        e.sym = sym
        return sym.ty

    def x_UnaryOp(self, e, expected, used):
        t = prune(self.check_expr(e.operand, expected=expected, used=True))
        if e.op == "-":
            if not is_numeric(t):
                self.err(f"unary `-` needs Int or Float, found `{t!r}`", e)
            return t
        if e.op == "!":
            if t is not BOOL:
                self.err(f"`!` / `not` needs Bool, found `{t!r}`", e)
            return BOOL
        self.err(f"unknown unary operator {e.op}", e)

    def coerce_int_lit(self, e, t):
        """If e is an Int literal and t is Float, retype the literal as Float."""
        if prune(t) is FLOAT and isinstance(e, A.IntLit):
            e.as_float = True
            e.ty = FLOAT
            return True
        return False

    def x_BinaryOp(self, e, expected, used):
        op = e.op
        if op in ("&&", "||"):
            for side in (e.left, e.right):
                t = prune(self.check_expr(side, expected=BOOL, used=True))
                if t is not BOOL:
                    self.err(f"`{op}` needs Bool operands, found `{t!r}`", side)
            return BOOL
        if op == "in":
            if isinstance(e.right, A.RangeExpr):
                lt = prune(self.check_expr(e.left, used=True))
                for part in (e.right.start, e.right.end):
                    pt = prune(self.check_expr(part, expected=INT, used=True))
                    if pt is not INT:
                        self.type_err("range bound", INT, pt, part)
                e.right.ty = INT
                if lt is not INT:
                    self.type_err("`in` range", INT, lt, e.left)
                e.in_kind = "range"
                return BOOL
            rt = prune(self.check_expr(e.right, used=True))
            if rt is STR:
                lt = prune(self.check_expr(e.left, used=True))
                if lt is not STR:
                    self.type_err("`in` on a Str", STR, lt, e.left)
                e.in_kind = "str"
                return BOOL
            if isinstance(rt, Arr):
                lt = self.check_expr(e.left, expected=rt.elem, used=True)
                if unify(lt, rt.elem) is None:
                    self.type_err("`in` element", rt.elem, lt, e.left)
                if prune(rt.elem) not in (INT, FLOAT, BOOL, STR):
                    self.err(f"`in` is only supported for lists of Int, Float, Bool or Str", e)
                e.in_kind = "arr"
                return BOOL
            self.err(f"`in` needs a list, Str or range on the right, found `{rt!r}`", e.right)
        lt = prune(self.check_expr(e.left, used=True))
        rt = prune(self.check_expr(e.right, expected=lt if is_numeric(lt) else None, used=True))
        if lt is FLOAT and rt is INT:
            self.coerce_int_lit(e.right, FLOAT)
            rt = prune(e.right.ty)
        if lt is INT and rt is FLOAT and self.coerce_int_lit(e.left, FLOAT):
            lt = FLOAT
        if isinstance(lt, TVar) or isinstance(rt, TVar):
            unify(lt, rt)
            lt, rt = prune(lt), prune(rt)
        opname = f"`{op}`"
        if op in ("+", "-", "*", "/", "%"):
            if op == "+" and lt is STR and rt is STR:
                return STR
            if op == "+" and (lt is STR or rt is STR):
                self.err(f"cannot add `{lt!r}` and `{rt!r}`; convert with `str(x)` or use interpolation \"{{x}}\"", e)
            if is_numeric(lt) and lt is rt:
                return lt
            # allow Int literal already coerced; promote sized with Int if same family via cast only
            if is_numeric(lt) and is_numeric(rt):
                self.err(f"mismatched operand types `{lt!r}` and `{rt!r}` for {opname}; convert with `as ...`", e)
            self.err(f"{opname} is not defined for `{lt!r}` and `{rt!r}`", e)
        if op in ("&", "|", "^", "<<", ">>"):
            if is_integer(lt) and is_integer(rt) and lt is rt:
                return lt
            if lt is INT and rt is INT:
                return INT
            self.err(f"{opname} needs matching integer operands, found `{lt!r}` and `{rt!r}`", e)
        if op in ("==", "!="):
            if lt is not rt:
                if is_numeric(lt) and is_numeric(rt):
                    self.err(f"cannot compare `{lt!r}` with `{rt!r}`; convert with `as ...`", e)
                self.err(f"cannot compare `{lt!r}` with `{rt!r}`", e)
            if lt not in (INT, FLOAT, BOOL, STR, PTR) and lt not in SIZED_INTS:
                self.err(f"{opname} on `{lt!r}` values not yet supported (compare fields instead)", e)
            return BOOL
        if op in ("<", ">", "<=", ">="):
            if lt is rt and (lt in (INT, FLOAT, STR) or lt in SIZED_INTS):
                return BOOL
            self.err(f"{opname} needs two Int, Float, sized-int or Str operands, found `{lt!r}` and `{rt!r}`", e)
        self.err(f"unknown operator {opname}", e)

    def x_RangeExpr(self, e, expected, used):
        self.err("ranges can only be used in `for` loops, `in` tests and slices (`xs[a..b]`)", e)

    def x_CastExpr(self, e, expected, used):
        src = prune(self.check_expr(e.expr, used=True))
        dst = self.resolve_type(e.target)
        if dst is STR:
            self.expect_printable(src, e.expr)
            return STR
        if dst is CSTR:
            if src is STR or src is CSTR:
                return CSTR
            self.err(f"cannot cast `{src!r}` to CStr (expected Str)", e)
        if src is dst:
            return dst
        # integer family <-> sized ints <-> Ptr
        if is_integer(src) and is_integer(dst):
            return dst
        if (src is INT or src in SIZED_INTS or src is PTR) and dst is PTR:
            return PTR
        if src is PTR and (dst is INT or dst in SIZED_INTS):
            return dst
        if src is FLOAT and dst is FLOAT:
            return dst
        if (src is INT or src in SIZED_INTS) and dst is FLOAT:
            return dst
        if src is FLOAT and (dst is INT or dst in SIZED_INTS):
            return dst
        if src is BOOL and (dst is INT or dst in SIZED_INTS):
            return dst
        if src is BOOL and dst is BOOL:
            return dst
        hint = " (use `parse_int(s)` / `parse_float(s)`)" if src is STR else ""
        self.err(f"cannot cast `{src!r}` to `{dst!r}`{hint}", e)

    def x_ArrayLit(self, e, expected, used):
        exp_elem = None
        if expected is not None and isinstance(prune(expected), Arr):
            exp_elem = prune(expected).elem
        if not e.elems:
            if exp_elem is not None:
                return self.record(Arr(exp_elem))
            tv = TVar()
            self.pending_tvars.append((tv, e.loc))
            return self.record(Arr(tv))
        et = exp_elem
        for x in e.elems:
            t = self.check_expr(x, expected=et, used=True)
            if et is None:
                et = t
            elif prune(et) is FLOAT and self.coerce_int_lit(x, FLOAT):
                pass
            elif unify(et, t) is None:
                self.type_err("list elements must all have the same type", et, t, x)
        if prune(et) in (UNIT, NEVER):
            self.err("list elements must have a value", e)
        # re-coerce earlier int literals if the list became Float
        if prune(et) is FLOAT:
            for x in e.elems:
                self.coerce_int_lit(x, FLOAT)
        return self.record(Arr(et))

    def x_StructLit(self, e, expected, used):
        st = self.structs.get(e.name)
        if st is None:
            sugg = difflib.get_close_matches(e.name, list(self.structs), n=1)
            hint = f" (did you mean `{sugg[0]}`?)" if sugg else ""
            self.err(f"unknown struct `{e.name}`{hint}", e)
        given = {}
        for fname, val, floc in e.fields:
            ft = st.field(fname)
            if ft is None:
                self.err(f"struct `{e.name}` has no field `{fname}` (fields: {', '.join(n for n, _ in st.fields)})", floc)
            if fname in given:
                self.err(f"field `{fname}` given twice", floc)
            vt = self.check_expr(val, expected=ft, used=True)
            if unify(ft, vt) is None:
                self.type_err(f"field `{e.name}.{fname}`", ft, vt, val)
            given[fname] = val
        missing = [n for n, _ in st.fields if n not in given]
        if missing:
            self.err(f"missing field(s) {', '.join('`' + m + '`' for m in missing)} in `{e.name}` literal", e)
        e.ordered = [given[n] for n, _ in st.fields]
        e.sty = st
        return st

    def x_Index(self, e, expected, used):
        ot = prune(self.check_expr(e.obj, used=True))
        if isinstance(e.index, A.RangeExpr):
            for part in (e.index.start, e.index.end):
                t = prune(self.check_expr(part, expected=INT, used=True))
                if t is not INT:
                    self.type_err("slice bound", INT, t, part)
            e.index.ty = INT
            e.inclusive = e.index.inclusive
            if isinstance(ot, Arr):
                e.kind = "arr_slice"
                return ot
            if ot is STR:
                e.kind = "str_slice"
                return STR
            self.err(f"cannot slice a value of type `{ot!r}`", e)
        it = prune(self.check_expr(e.index, expected=INT, used=True))
        if it is not INT:
            self.type_err("index", INT, it, e.index)
        if isinstance(ot, Arr):
            e.kind = "arr"
            return ot.elem
        if ot is STR:
            e.kind = "str"
            return STR
        self.err(f"cannot index a value of type `{ot!r}`", e)

    def x_FieldAccess(self, e, expected, used):
        # Module qualified access: gfx.clear — handled specially when used as a call target;
        # as a value we error (functions are not first-class). Still type the module obj.
        if isinstance(e.obj, A.Ident) and e.obj.name in self.module_binds:
            mod = self.module_binds[e.obj.name]
            e.obj.sym = self.globals[e.obj.name]
            e.obj.ty = e.obj.sym.ty
            e.module_path = mod
            qname = f"{mod}.{e.field}"
            if qname in self.fns:
                e.kind = "module_fn"
                e.qual_fn = qname
                self.err(f"`{e.obj.name}.{e.field}` is a function; call it with `{e.obj.name}.{e.field}(...)`", e)
            exports = self.module_exports.get(mod, {})
            if e.field in exports:
                decl = exports[e.field]
                if isinstance(decl, A.LetStmt):
                    # pub const / let / var from the module
                    sym = decl.sym
                    e.kind = "module_global"
                    e.sym = sym
                    return sym.ty
                self.err(f"`{e.obj.name}.{e.field}` cannot be used as a value here", e)
            self.err(f"module `{mod}` has no public `{e.field}`", e)
        ot = prune(self.check_expr(e.obj, used=True))
        if isinstance(ot, ModuleT):
            e.kind = "module_field"
            e.module_path = ot.name
            self.err(f"`{ot.name}.{e.field}` is not a value (call functions with `(...)`)", e)
        if isinstance(ot, StructT):
            ft = ot.field(e.field)
            if ft is not None:
                e.kind = "field"
                return ft
        # list.ptr -> Ptr (pointer to first element / buffer)
        if isinstance(ot, Arr) and e.field == "ptr":
            e.kind = "arr_ptr"
            return PTR
        # UFCS property style: `xs.len` == `len(xs)`
        if e.field in self.fns or e.field in BUILTINS:
            call = A.Call(loc=e.loc, func=A.Ident(loc=e.loc, name=e.field), args=[])
            call.receiver = e.obj
            e.kind = "call"
            e.as_call = call
            return self.check_expr(call, expected=expected, used=used)
        if isinstance(ot, StructT):
            self.err(f"struct `{ot.name}` has no field `{e.field}` (fields: {', '.join(n for n, _ in ot.fields)})", e)
        self.err(f"type `{ot!r}` has no field or method `{e.field}`", e)

    def x_Assign(self, e, expected, used):
        if used:
            self.err("assignment has no value and cannot be used as an expression", e)
        tt = self.check_lvalue(e.target)
        vt = self.check_expr(e.value, expected=tt, used=True)
        if getattr(e.value, "compound", False):
            pass
        if unify(tt, vt) is None:
            self.type_err("assignment type mismatch", tt, vt, e.value)
        return UNIT

    def check_lvalue(self, t) -> Ty:
        if isinstance(t, A.Ident):
            ty = self.check_expr(t, used=True)
            if not t.sym.mutable:
                what = "loop variable" if t.sym.kind == "local" and t.sym.cname and False else "binding"
                self.err(f"cannot assign to immutable {what} `{t.name}` (declare it with `var`)", t)
            return ty
        if isinstance(t, A.Index):
            ty = self.check_expr(t, used=True)
            if t.kind != "arr":
                self.err("strings are immutable; only list elements can be assigned (`xs[i] = v`)", t)
            return ty
        if isinstance(t, A.FieldAccess):
            ty = self.check_expr(t, used=True)
            if getattr(t, "kind", None) != "field":
                self.err("can only assign to struct fields", t)
            root = t.obj
            while isinstance(root, A.FieldAccess):
                root = root.obj
            if isinstance(root, A.Ident) and not root.sym.mutable:
                self.err(f"cannot assign to a field of immutable binding `{root.name}` (declare it with `var`)", t)
            if not isinstance(root, (A.Ident, A.Index)):
                self.err("invalid assignment target", t)
            return ty
        self.err("invalid assignment target", t)

    def x_Block(self, e, expected, used):
        return self.check_block(e, used=used, expected=expected)

    def x_IfExpr(self, e, expected, used):
        self.expect_bool(e.cond, "`if` condition")
        tt = self.check_block(e.then_body, used=used, expected=expected)
        if e.else_body is None:
            return NEVER if False else UNIT
        et = self.check_expr(e.else_body, expected=expected, used=used)
        if not used:
            if prune(tt) is NEVER and prune(et) is NEVER:
                return NEVER
            return UNIT
        u = unify(tt, et)
        if u is None:
            if prune(tt) is FLOAT and self.retype_tail_int(e.else_body):
                return FLOAT
            if prune(et) is FLOAT and self.retype_tail_int(e.then_body):
                return FLOAT
            self.err(f"`if` and `else` branches have different types: `{prune(tt)!r}` vs `{prune(et)!r}`", e)
        return u

    def retype_tail_int(self, blk) -> bool:
        if isinstance(blk, A.Block) and blk.stmts and isinstance(blk.stmts[-1], A.ExprStmt):
            ex = blk.stmts[-1].expr
            if self.coerce_int_lit(ex, FLOAT):
                blk.ty = FLOAT
                return True
        return False

    def x_MatchExpr(self, e, expected, used):
        st = prune(self.check_expr(e.scrutinee, used=True))
        if st not in (INT, STR, BOOL):
            self.err(f"`match` supports Int, Str and Bool values, found `{st!r}`", e.scrutinee)
        result = None
        exhaustive = False
        seen_bool = set()
        for arm in e.arms:
            self.push_scope()
            for p in arm.pattern:
                self.check_pattern(p, st, arm)
            if arm.guard is not None:
                self.expect_bool(arm.guard, "`when` guard")
            else:
                for p in arm.pattern:
                    if p.kind in ("wildcard", "bind"):
                        exhaustive = True
                    if p.kind == "bool":
                        seen_bool.add(p.value)
            if isinstance(arm.body, A.Block):
                bt = self.check_block(arm.body, used=used, expected=expected)
            else:
                bt = self.check_expr(arm.body, expected=expected, used=used)
            arm.ty = bt
            self.pop_scope()
            if used:
                if result is None:
                    result = bt
                else:
                    u = unify(result, bt)
                    if u is None:
                        self.err(f"match arms have different types: `{prune(result)!r}` vs `{prune(bt)!r}`", arm.body)
                    result = u
        if st is BOOL and seen_bool == {True, False}:
            exhaustive = True
        if not exhaustive:
            self.err("non-exhaustive `match`: add a catch-all arm `_ => ...`", e)
        e.scrut_ty = st
        if not used:
            if e.arms and all(prune(a.ty) is NEVER for a in e.arms):
                return NEVER
            return UNIT
        return result if result is not None else UNIT

    def check_pattern(self, p: A.Pattern, st, arm):
        if len(arm.pattern) > 1 and p.kind == "bind":
            self.err("a binding pattern cannot be combined with `|`", p)
        if p.kind in ("int", "range"):
            if st is not INT:
                self.err(f"integer pattern cannot match a `{st!r}` value", p)
            if p.kind == "range" and p.value[0] > p.value[1]:
                self.err("empty range pattern", p)
        elif p.kind == "str":
            if st is not STR:
                self.err(f"string pattern cannot match a `{st!r}` value", p)
        elif p.kind == "bool":
            if st is not BOOL:
                self.err(f"`true`/`false` pattern cannot match a `{st!r}` value", p)
        elif p.kind == "bind":
            p.sym = self.declare(p.value, st, False, "local", p)

    # ── calls ──────────────────────────────────────────────────────
    def x_Call(self, e, expected, used):
        receiver = getattr(e, "receiver", None)
        module_call = False
        if (isinstance(e.func, A.FieldAccess) and isinstance(e.func.obj, A.Ident)
                and e.func.obj.name == "fs" and self.lookup("fs") is None):
            name = "fs." + e.func.field
            name_loc = e.func.loc
            if name not in BUILTINS:
                self.err(f"unknown function `{name}` (available: fs.read_text, fs.write_text, fs.exists)", name_loc)
        elif (isinstance(e.func, A.FieldAccess) and isinstance(e.func.obj, A.Ident)
              and e.func.obj.name in self.module_binds):
            # gfx.open(...) — qualified module call, not UFCS
            mod = self.module_binds[e.func.obj.name]
            e.func.obj.sym = self.globals[e.func.obj.name]
            e.func.obj.ty = e.func.obj.sym.ty
            name = f"{mod}.{e.func.field}"
            name_loc = e.func.loc
            module_call = True
            receiver = None
            # ensure export
            exports = self.module_exports.get(mod, {})
            if e.func.field not in exports:
                if name in self.fns:
                    self.err(f"`{e.func.field}` is not `pub` in module `{mod}`", name_loc)
                self.err(f"module `{mod}` has no public function `{e.func.field}`", name_loc)
        elif isinstance(e.func, A.FieldAccess):
            receiver = e.func.obj
            name = e.func.field
            name_loc = e.func.loc
        elif isinstance(e.func, A.Ident):
            name = e.func.name
            name_loc = e.func.loc
            if receiver is None and self.lookup(name) is not None:
                self.err(f"`{name}` is a variable, not a function", e.func)
        else:
            self.err("only named functions can be called", e)
        args = ([receiver] if receiver is not None else []) + list(e.args)
        e.cargs = args
        if name in self.fns:
            info = self.fns[name]
            if len(args) != len(info.params):
                self.err(f"`{name}` expects {len(info.params)} argument(s) but got {len(args)}", e)
            for a, (pn, pt) in zip(args, info.params):
                at = self.check_expr(a, expected=pt, used=True)
                at = prune(at)
                pt = prune(pt)
                # Auto-coerce Str -> CStr for extern / CStr params
                if pt is CSTR and at is STR:
                    a.coerce_to_cstr = True
                    at = CSTR
                elif unify(pt, at) is None:
                    self.type_err(f"argument `{pn}` of `{name}`", pt, at, a)
            e.target = ("user", info)
            return info.ret
        if name in BUILTINS:
            e.target = ("builtin", name)
            return self.check_builtin(name, args, e, expected)
        if name in self.structs:
            self.err(f"`{name}` is a struct; construct it with `{name} {{ field: value }}`", e)
        sugg = difflib.get_close_matches(name, list(self.fns) + sorted(BUILTINS), n=1)
        hint = f" (did you mean `{sugg[0]}`?)" if sugg else ""
        if receiver is not None:
            rt = prune(self.check_expr(receiver, used=True))
            self.err(f"no method or function `{name}` for type `{rt!r}`{hint}", name_loc)
        self.err(f"unknown function `{name}`{hint}", name_loc)

    def arity(self, name, args, lo, hi, e):
        if not (lo <= len(args) <= hi):
            want = str(lo) if lo == hi else f"{lo}-{hi}"
            self.err(f"`{name}` expects {want} argument(s) but got {len(args)}", e)

    def arg(self, a, want: Ty | None, what: str) -> Ty:
        t = self.check_expr(a, expected=want, used=True)
        if want is not None and unify(want, t) is None:
            self.type_err(what, want, t, a)
        return prune(t)

    def check_builtin(self, name, args, e, expected) -> Ty:
        if name in ("print", "println", "eprint", "eprintln"):
            for a in args:
                self.expect_printable(self.check_expr(a, used=True), a)
            return UNIT
        if name in ("str", "to_str"):
            self.arity(name, args, 1, 1, e)
            self.expect_printable(self.check_expr(args[0], used=True), args[0])
            return STR
        if name == "len":
            self.arity(name, args, 1, 1, e)
            t = self.arg(args[0], None, "len")
            if not (t is STR or isinstance(t, Arr)):
                self.err(f"`len` needs a Str or list, found `{t!r}`", args[0])
            return INT
        if name in ("push", "pop", "clear", "sort", "reverse", "sum", "join", "contains", "fill"):
            if name == "fill":
                self.arity(name, args, 2, 2, e)
                self.arg(args[0], INT, "fill count")
                want = prune(expected).elem if expected is not None and isinstance(prune(expected), Arr) else None
                t = self.arg(args[1], want, "fill value")
                self.expect_printable(t, args[1])
                return self.record(Arr(want if want is not None else t))
            if not args:
                self.err(f"`{name}` needs a list argument", e)
            if name == "contains":
                self.arity(name, args, 2, 2, e)
                t = self.arg(args[0], None, "contains")
                if t is STR:
                    self.arg(args[1], STR, "contains")
                    e.variant = "str"
                    return BOOL
                if isinstance(t, Arr):
                    self.arg(args[1], t.elem, "contains element")
                    if prune(t.elem) not in (INT, FLOAT, BOOL, STR):
                        self.err("`contains` is only supported for lists of Int, Float, Bool or Str", e)
                    e.variant = "arr"
                    return BOOL
                self.err(f"`contains` needs a Str or list, found `{t!r}`", args[0])
            t = self.arg(args[0], None, name)
            if not isinstance(t, Arr):
                self.err(f"`{name}` needs a list, found `{t!r}`", args[0])
            el = t.elem
            if name == "push":
                self.arity(name, args, 2, 2, e)
                self.arg(args[1], el, "pushed value")
                return UNIT
            self_ = None
            if name == "join":
                self.arity(name, args, 1, 2, e)
                if prune(el) is not STR:
                    self.err("`join` needs a list of Str (convert with `str(x)` first)", args[0])
                if len(args) == 2:
                    self.arg(args[1], STR, "join separator")
                return STR
            self.arity(name, args, 1, 1, e)
            if name == "pop":
                return el
            if name == "clear":
                return UNIT
            if name == "sort":
                if prune(el) not in (INT, FLOAT, STR):
                    self.err("`sort` needs a list of Int, Float or Str", args[0])
                return t
            if name == "reverse":
                return t
            if name == "sum":
                if prune(el) not in (INT, FLOAT):
                    self.err("`sum` needs a list of Int or Float", args[0])
                return prune(el)
        if name in ("split", "starts_with", "ends_with", "find", "repeat", "replace"):
            sig = {"split": ([STR, STR], Arr(STR)), "starts_with": ([STR, STR], BOOL),
                   "ends_with": ([STR, STR], BOOL), "find": ([STR, STR], INT),
                   "repeat": ([STR, INT], STR), "replace": ([STR, STR, STR], STR)}[name]
            self.arity(name, args, len(sig[0]), len(sig[0]), e)
            for a, w in zip(args, sig[0]):
                self.arg(a, w, f"`{name}` argument")
            return self.record(sig[1])
        if name in ("split_whitespace", "lines", "trim", "to_upper", "to_lower", "parse_int", "parse_float"):
            self.arity(name, args, 1, 1, e)
            self.arg(args[0], STR, f"`{name}` argument")
            return {"split_whitespace": self.record(Arr(STR)), "lines": self.record(Arr(STR)), "trim": STR,
                    "to_upper": STR, "to_lower": STR, "parse_int": INT, "parse_float": FLOAT}[name]
        if name == "abs":
            self.arity(name, args, 1, 1, e)
            t = self.arg(args[0], None, "abs")
            if not is_numeric(t):
                self.err(f"`abs` needs Int or Float, found `{t!r}`", args[0])
            return t
        if name in ("min", "max"):
            self.arity(name, args, 2, 2, e)
            a = self.arg(args[0], None, name)
            b = self.arg(args[1], a if is_numeric(a) else None, name)
            if a is FLOAT and b is INT and self.coerce_int_lit(args[1], FLOAT):
                b = FLOAT
            if a is INT and b is FLOAT and self.coerce_int_lit(args[0], FLOAT):
                a = FLOAT
            if a is not b or a not in (INT, FLOAT, STR):
                self.err(f"`{name}` needs two Int, two Float or two Str values, found `{a!r}` and `{b!r}`", e)
            return a
        if name in ("sqrt", "floor", "ceil", "sin", "cos"):
            self.arity(name, args, 1, 1, e)
            self.arg(args[0], FLOAT, f"`{name}` argument")
            return FLOAT
        if name == "pow":
            self.arity(name, args, 2, 2, e)
            self.arg(args[0], FLOAT, "`pow` base")
            self.arg(args[1], FLOAT, "`pow` exponent")
            return FLOAT
        if name == "exit":
            self.arity(name, args, 1, 1, e)
            self.arg(args[0], INT, "exit code")
            return NEVER
        if name == "panic":
            self.arity(name, args, 1, 1, e)
            self.arg(args[0], STR, "panic message")
            return NEVER
        if name == "assert":
            self.arity(name, args, 1, 2, e)
            self.arg(args[0], BOOL, "assert condition")
            if len(args) == 2:
                self.arg(args[1], STR, "assert message")
            return UNIT
        if name == "ask":
            self.arity(name, args, 0, 1, e)
            if args:
                self.arg(args[0], STR, "prompt")
            return STR
        if name == "read_line":
            self.arity(name, args, 0, 0, e)
            return STR
        if name == "now_ms":
            self.arity(name, args, 0, 0, e)
            return INT
        if name == "fs.read_text":
            self.arity(name, args, 1, 1, e)
            self.arg(args[0], STR, "path")
            return STR
        if name == "fs.write_text":
            self.arity(name, args, 2, 2, e)
            self.arg(args[0], STR, "path")
            self.arg(args[1], STR, "text")
            return UNIT
        if name == "fs.exists":
            self.arity(name, args, 1, 1, e)
            self.arg(args[0], STR, "path")
            return BOOL
        if name == "env":
            self.arity(name, args, 1, 1, e)
            self.arg(args[0], STR, "env name")
            return STR
        self.err(f"builtin `{name}` not implemented", e)

    # ── collected types for codegen ────────────────────────────────
    def concrete_types(self):
        """All array and struct types used, pruned and de-duplicated, in dependency order."""
        arrs: dict[str, Ty] = {}
        from .ktypes import mangle

        def add(t):
            t = prune(t)
            if isinstance(t, Arr):
                if not t.is_concrete():
                    return
                add(t.elem)
                k = mangle(t)
                if k not in arrs:
                    arrs[k] = Arr(prune(t.elem))
        for t in self.all_types:
            add(t)
        for st in self.structs.values():
            for _, ft in st.fields:
                add(ft)
        return arrs

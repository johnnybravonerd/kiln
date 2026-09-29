"""Kiln parser: tokens -> AST (recursive descent + precedence climbing)."""

from __future__ import annotations
from .lexer import Token, tokenize
from .errors import CompileError, SourceLoc
from . import ast_nodes as A

UNSUPPORTED_KW = {
    "enum": "enums / algebraic data types",
    "trait": "traits",
    "impl": "impl blocks (use plain functions; UFCS `x.f(y)` calls `f(x, y)`)",
    "spawn": "concurrency (`spawn`)",
    "scope": None,  # only unsupported when followed by IDENT '{'
    "defer": "`defer`",
    "try": "error unions / `try`",
    "async": "async functions",
    "await": "`await`",
    "select": "`select`",
    "actor": "actors",
    "comptime": "`comptime`",
    "macro": "macros",
    "unsafe": "`unsafe` blocks",
    "asm": "inline assembly",
    "yield": "generators / `yield`",
    "with": "`with` resource blocks",
    "error": None,  # `error Name` declarations
    "type": None,   # type aliases
    "bench": "`bench` blocks",
    "from": None,
}

BINARY_PREC = [
    # (operators, precedence) higher binds tighter
    ({"||", "or"}, 1),
    ({"&&", "and"}, 2),
    ({"==", "!=", "<", ">", "<=", ">=", "in"}, 3),
    ({"..", "..="}, 4),
    ({"|>"}, 5),
    ({"|"}, 6),
    ({"^"}, 7),
    ({"&"}, 8),
    ({"<<", ">>"}, 9),
    ({"+", "-"}, 10),
    ({"*", "/", "%"}, 11),
]
PREC = {op: p for ops, p in BINARY_PREC for op in ops}
ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%="}


class Parser:
    def __init__(self, tokens: list[Token], source: str = "", filename: str = "<input>"):
        self.toks = tokens
        self.i = 0
        self.source = source
        self.file = filename
        # stack: True = newlines are insignificant (inside () / [])
        self.nl_stack: list[bool] = [False]
        self.no_struct = 0  # >0 while parsing if/while/for/match headers

    # ── token helpers ──────────────────────────────────────────────
    def _skip_nl_if_needed(self):
        if self.nl_stack[-1]:
            while self.toks[self.i].kind == "NEWLINE":
                self.i += 1

    def peek(self, off: int = 0) -> Token:
        self._skip_nl_if_needed()
        j = self.i
        k = 0
        while True:
            t = self.toks[j]
            if self.nl_stack[-1] and t.kind == "NEWLINE":
                j += 1
                continue
            if k == off:
                return t
            k += 1
            j = min(j + 1, len(self.toks) - 1)

    def next(self) -> Token:
        self._skip_nl_if_needed()
        t = self.toks[self.i]
        if self.i < len(self.toks) - 1:
            self.i += 1
        return t

    def at(self, kind: str, value=None, off: int = 0) -> bool:
        t = self.peek(off)
        if t.kind != kind:
            return False
        return value is None or t.value == value

    def at_op(self, value: str, off: int = 0) -> bool:
        return self.at("OP", value, off)

    def at_kw(self, value: str, off: int = 0) -> bool:
        return self.at("KW", value, off)

    def accept_op(self, value: str) -> bool:
        if self.at_op(value):
            self.next()
            return True
        return False

    def error(self, msg: str, tok: Token | None = None):
        tok = tok or self.peek()
        raise CompileError(msg, tok.loc)

    def describe(self, t: Token) -> str:
        if t.kind == "NEWLINE":
            return "end of line"
        if t.kind == "EOF":
            return "end of file"
        if t.kind == "STRING":
            return "string literal"
        return f"`{t.value}`"

    def expect_op(self, value: str) -> Token:
        t = self.peek()
        if t.kind == "OP" and t.value == value:
            return self.next()
        self.error(f"expected `{value}` but found {self.describe(t)}")

    def expect_ident(self, what: str = "identifier") -> Token:
        t = self.peek()
        if t.kind == "IDENT":
            return self.next()
        self.error(f"expected {what} but found {self.describe(t)}")

    def skip_newlines(self):
        while self.toks[self.i].kind == "NEWLINE" or (self.toks[self.i].kind == "OP" and self.toks[self.i].value == ";"):
            self.i += 1

    def raw(self) -> Token:
        return self.toks[self.i]

    def end_stmt(self):
        t = self.raw()
        if t.kind in ("NEWLINE", "EOF") or (t.kind == "OP" and t.value in (";", "}")):
            return
        self.error(f"expected end of statement (newline) but found {self.describe(t)}", t)

    # ── program ────────────────────────────────────────────────────
    def parse_program(self) -> A.Program:
        prog = A.Program(loc=self.raw().loc, filename=self.file)
        self.skip_newlines()
        while self.raw().kind != "EOF":
            d = self.parse_top_level()
            if d is not None:
                if isinstance(d, list):
                    prog.decls.extend(d)
                else:
                    prog.decls.append(d)
            # extern blocks / attributes may leave us mid-line; skip trailing junk carefully
            if self.raw().kind not in ("NEWLINE", "EOF") and not (self.raw().kind == "OP" and self.raw().value == "}"):
                # allow nothing else on the line after a decl
                pass
            self.end_stmt()
            self.skip_newlines()
        # hoist module name
        for d in prog.decls:
            if isinstance(d, A.ModuleStmt):
                if prog.module_name and prog.module_name != d.path:
                    self.error(f"module already declared as `{prog.module_name}`", d)
                prog.module_name = d.path
        return prog

    def parse_top_level(self):
        t = self.raw()
        link_lib = None
        # attributes: @link("lib") is meaningful; others are accepted and ignored
        while t.kind == "OP" and t.value == "@":
            self.next()
            aname = self.expect_ident("attribute name")
            if aname.value == "link":
                self.expect_op("(")
                st = self.peek()
                if st.kind != "STRING" or len(st.value) != 1 or not isinstance(st.value[0], str):
                    self.error("`@link` expects a string library name, e.g. `@link(\"user32\")`", st)
                self.next()
                link_lib = st.value[0]
                self.expect_op(")")
            elif self.at_op("("):
                self.skip_balanced("(", ")")
            self.skip_newlines()
            t = self.raw()
        is_pub = False
        if t.kind == "KW" and t.value == "pub":
            self.next()
            is_pub = True
            self.skip_newlines()
            t = self.raw()
        if t.kind == "KW" and t.value == "module":
            return self.parse_module_stmt()
        if t.kind == "KW" and t.value == "use":
            return self.parse_use_stmt()
        if t.kind == "KW" and t.value == "extern":
            return self.parse_extern(link_lib=link_lib, is_pub=is_pub)
        if t.kind == "KW" and t.value == "fn":
            d = self.parse_fn()
            d.is_pub = is_pub
            if link_lib:
                self.error("`@link` applies only to `extern` declarations", t)
            return d
        if t.kind == "KW" and t.value == "struct":
            d = self.parse_struct()
            d.is_pub = is_pub
            return d
        if t.kind == "KW" and t.value in ("let", "var", "const"):
            d = self.parse_let()
            d.is_pub = is_pub
            return d
        if t.kind == "IDENT" and t.value == "test" and self.peek(1).kind == "STRING":
            return self.parse_test()
        self.check_unsupported()
        self.error(f"expected a declaration (`fn`, `struct`, `let`, `var`, `const`, `extern`, `use`, `test`) at top level but found {self.describe(t)}")

    def parse_module_stmt(self) -> A.ModuleStmt:
        kw = self.next()
        parts = []
        parts.append(self.expect_ident("module name").value)
        while self.accept_op("."):
            parts.append(self.expect_ident("module path segment").value)
        return A.ModuleStmt(loc=kw.loc, path=".".join(parts))

    def parse_use_stmt(self) -> A.UseStmt:
        kw = self.next()
        parts = []
        parts.append(self.expect_ident("module name").value)
        while self.accept_op("."):
            # allow use a.b.c  or  use a.{X, Y}  — brace form not yet
            if self.at_op("{"):
                self.error("`use path.{Item, ...}` selective imports not yet supported; use `use path`")
            parts.append(self.expect_ident("module path segment").value)
        if self.at_op("{"):
            self.error("`use path.{Item, ...}` selective imports not yet supported; use `use path`")
        if self.at_op("*"):
            self.error("`use path.*` glob imports not yet supported; use `use path`")
        alias = None
        if self.at_kw("as"):
            self.next()
            alias = self.expect_ident("import alias").value
        return A.UseStmt(loc=kw.loc, path=".".join(parts), alias=alias)

    def parse_extern(self, link_lib=None, is_pub=False):
        kw = self.next()  # extern
        # extern "lib" { fn ... ; ... }   OR   extern fn Name(...) -> T
        if self.peek().kind == "STRING":
            st = self.next()
            if len(st.value) != 1 or not isinstance(st.value[0], str):
                self.error('`extern "lib"` expects a plain string library name', st)
            lib = st.value[0]
            if link_lib and link_lib != lib:
                self.error(f"`@link(\"{link_lib}\")` disagrees with `extern \"{lib}\"`", st)
            link_lib = lib
            self.skip_newlines()
            if self.at_op("{"):
                return self.parse_extern_block(link_lib=link_lib, is_pub=is_pub, loc=kw.loc)
        self.skip_newlines()
        if not self.at_kw("fn"):
            self.error("expected `fn` after `extern` (e.g. `extern fn puts(s: CStr) -> I32`)")
        d = self.parse_extern_fn(link_lib=link_lib, is_pub=is_pub)
        return d

    def parse_extern_block(self, link_lib, is_pub, loc):
        """Parse `extern "lib" { fn a(...); fn b(...) }` as a synthetic block of decls.
        Returns a list-like Program fragment via a special ExternBlock node — we flatten
        into multiple decls by returning the first and stashing the rest.
        Actually we return a list wrapped; caller flattens. We use a ModuleStmt-like
        trick: return a list from parse_top_level is messy. Instead parse into a
        temporary and have parse_program flatten lists.
        """
        self.expect_op("{")
        self.nl_stack.append(True)
        decls = []
        self.skip_newlines()
        while not self.at_op("}"):
            if self.raw().kind == "EOF":
                self.error("unclosed `extern` block", loc)
            if self.at_kw("fn") or (self.at_kw("pub") and self.peek(1).kind == "KW" and self.peek(1).value == "fn"):
                pub = False
                if self.at_kw("pub"):
                    self.next()
                    pub = True
                    self.skip_newlines()
                d = self.parse_extern_fn(link_lib=link_lib, is_pub=pub or is_pub)
                decls.append(d)
            else:
                self.error(f"expected `fn` in extern block but found {self.describe(self.peek())}")
            self.skip_newlines()
            if self.accept_op(";"):
                self.skip_newlines()
        self.nl_stack.pop()
        self.expect_op("}")
        # Return a list; parse_program will flatten
        return decls

    def parse_extern_fn(self, link_lib=None, is_pub=False) -> A.FnDecl:
        if not self.at_kw("fn"):
            self.error("expected `fn` in extern declaration")
        kw = self.next()
        name = self.expect_ident("extern function name")
        if self.at_op("["):
            self.error("generic extern functions not yet supported")
        self.expect_op("(")
        self.nl_stack.append(True)
        params = []
        while not self.at_op(")"):
            pn = self.expect_ident("parameter name")
            self.expect_op(":")
            pt = self.parse_type()
            params.append(A.FnParam(loc=pn.loc, name=pn.value, typ=pt))
            if not self.accept_op(","):
                break
        self.nl_stack.pop()
        self.expect_op(")")
        ret = None
        if self.accept_op("->"):
            ret = self.parse_type()
        return A.FnDecl(loc=name.loc, name=name.value, params=params, ret_type=ret,
                        body=None, is_pub=is_pub, is_extern=True, link_lib=link_lib)

    def skip_balanced(self, open_: str, close: str):
        self.expect_op(open_)
        depth = 1
        while depth:
            t = self.next()
            if t.kind == "EOF":
                self.error(f"unbalanced `{open_}`", t)
            if t.kind == "OP" and t.value == open_:
                depth += 1
            elif t.kind == "OP" and t.value == close:
                depth -= 1

    def check_unsupported(self):
        t = self.raw()
        if t.kind in ("IDENT", "KW") and t.value in UNSUPPORTED_KW:
            what = UNSUPPORTED_KW[t.value]
            nxt = self.toks[self.i + 1] if self.i + 1 < len(self.toks) else t
            if t.value == "error" and nxt.kind == "IDENT":
                what = "typed error declarations (`error Name`)"
            elif t.value == "type" and nxt.kind == "IDENT":
                what = "type aliases"
            elif t.value == "scope" and nxt.kind == "IDENT":
                what = "structured concurrency (`scope`)"
            elif t.value == "from" and nxt.kind == "IDENT":
                what = "query expressions"
            if what:
                self.error(f"{what} not yet supported in Kiln {VERSION}", t)

    def parse_test(self) -> A.TestDecl:
        t = self.next()
        s = self.next()
        if len(s.value) != 1 or not isinstance(s.value[0], str):
            self.error("test name must be a plain string", s)
        body = self.parse_block()
        return A.TestDecl(loc=t.loc, name=s.value[0], body=body)

    def parse_fn(self) -> A.FnDecl:
        kw = self.next()
        name = self.expect_ident("function name")
        if self.at_op("["):
            self.error("generic functions not yet supported")
        self.expect_op("(")
        self.nl_stack.append(True)
        params = []
        while not self.at_op(")"):
            if self.at("IDENT", "mut"):
                self.error("`mut` parameters not yet supported (arrays are passed by reference already)")
            if self.at_op("..."):
                self.error("variadic parameters not yet supported")
            pn = self.expect_ident("parameter name")
            self.expect_op(":")
            pt = self.parse_type()
            if self.at_op("="):
                self.error("default parameter values not yet supported")
            params.append(A.FnParam(loc=pn.loc, name=pn.value, typ=pt))
            if not self.accept_op(","):
                break
        self.nl_stack.pop()
        self.expect_op(")")
        ret = None
        if self.accept_op("->"):
            ret = self.parse_type()
        if self.at_op("="):
            eq = self.next()
            self.skip_newlines()
            e = self.parse_expr()
            body = A.Block(loc=eq.loc, stmts=[A.ExprStmt(loc=e.loc, expr=e)])
        else:
            body = self.parse_block()
        return A.FnDecl(loc=name.loc, name=name.value, params=params, ret_type=ret, body=body)

    def parse_struct(self) -> A.StructDecl:
        self.next()
        name = self.expect_ident("struct name")
        self.expect_op("{")
        self.nl_stack.append(True)
        fields = []
        while not self.at_op("}"):
            fn = self.expect_ident("field name")
            self.expect_op(":")
            ft = self.parse_type()
            fields.append(A.StructField(loc=fn.loc, name=fn.value, typ=ft))
            if not self.accept_op(","):
                # allow newline-separated fields
                if self.at_op("}"):
                    break
                t = self.toks[self.i - 1]
                if self.peek().kind != "IDENT":
                    self.error(f"expected `,` or `}}` in struct but found {self.describe(self.peek())}")
        self.nl_stack.pop()
        self.expect_op("}")
        return A.StructDecl(loc=name.loc, name=name.value, fields=fields)

    def parse_type(self) -> A.TypeExpr:
        t = self.peek()
        if t.kind == "OP" and t.value == "[":
            self.next()
            self.nl_stack.append(True)
            elem = self.parse_type()
            self.nl_stack.pop()
            self.expect_op("]")
            ty = A.ArrayType(loc=t.loc, elem=elem)
        elif t.kind == "OP" and t.value == "(":
            self.next()
            self.expect_op(")")
            ty = A.NamedType(loc=t.loc, name="Unit")
        elif t.kind == "OP" and t.value == "{":
            self.error("map types not yet supported")
        elif t.kind == "KW" and t.value == "fn":
            self.error("function types not yet supported")
        elif t.kind == "IDENT":
            self.next()
            ty = A.NamedType(loc=t.loc, name=t.value)
            if self.at_op("["):
                self.error(f"generic type `{t.value}[...]` not yet supported (use `[T]` for lists)")
        else:
            self.error(f"expected a type but found {self.describe(t)}")
        r = self.raw()
        if r.kind == "OP" and r.value == "?":
            self.error("optional types (`T?`) not yet supported", r)
        if r.kind == "OP" and r.value == "!":
            self.error("error-union types (`T!E`) not yet supported", r)
        return ty

    # ── statements ─────────────────────────────────────────────────
    def parse_block(self) -> A.Block:
        lb = self.expect_op("{")
        self.nl_stack.append(False)
        blk = A.Block(loc=lb.loc)
        self.skip_newlines()
        while not (self.raw().kind == "OP" and self.raw().value == "}"):
            if self.raw().kind == "EOF":
                self.nl_stack.pop()
                self.error("unclosed `{` (block opened here)", lb)
            blk.stmts.append(self.parse_stmt())
            self.end_stmt()
            self.skip_newlines()
        self.nl_stack.pop()
        self.next()
        return blk

    def parse_let(self) -> A.LetStmt:
        kw = self.next()
        if self.at_op("("):
            self.error("destructuring bindings not yet supported")
        name = self.expect_ident("variable name")
        typ = None
        if self.accept_op(":"):
            typ = self.parse_type()
        if not self.at_op("="):
            self.error(f"`{kw.value} {name.value}` needs an initial value (`= ...`)")
        self.next()
        self.skip_newlines()
        val = self.parse_expr()
        st = A.LetStmt(loc=name.loc, name=name.value, mutable=(kw.value == "var"), typ=typ, value=val)
        st.is_const = kw.value == "const"
        return st

    def parse_stmt(self):
        t = self.raw()
        if t.kind == "KW":
            v = t.value
            if v in ("let", "var", "const"):
                return self.parse_let()
            if v == "return":
                self.next()
                r = self.raw()
                val = None
                if not (r.kind in ("NEWLINE", "EOF") or (r.kind == "OP" and r.value in ("}", ";"))):
                    val = self.parse_expr()
                return A.ReturnStmt(loc=t.loc, value=val)
            if v == "while":
                self.next()
                self.no_struct += 1
                cond = self.parse_expr()
                self.no_struct -= 1
                return A.WhileStmt(loc=t.loc, cond=cond, body=self.parse_block())
            if v == "loop":
                self.next()
                return A.LoopStmt(loc=t.loc, body=self.parse_block())
            if v == "for":
                return self.parse_for()
            if v == "break":
                self.next()
                r = self.raw()
                if not (r.kind in ("NEWLINE", "EOF") or (r.kind == "OP" and r.value in ("}", ";"))):
                    self.error("`break` with a value / label not yet supported")
                return A.BreakStmt(loc=t.loc)
            if v == "continue":
                self.next()
                return A.ContinueStmt(loc=t.loc)
            if v == "fn":
                self.error("nested functions not yet supported (declare functions at top level)")
            if v == "struct":
                self.error("structs must be declared at top level")
        if t.kind == "IDENT":
            if t.value == "guard":
                self.next()
                self.no_struct += 1
                cond = self.parse_expr()
                self.no_struct -= 1
                if not self.at_kw("else"):
                    self.error("expected `else` after guard condition")
                self.next()
                return A.GuardStmt(loc=t.loc, cond=cond, else_body=self.parse_block())
            if t.value == "assert":
                nxt = self.toks[self.i + 1]
                if not (nxt.kind == "OP" and nxt.value == "("):
                    self.next()
                    cond = self.parse_expr()
                    msg = None
                    if self.accept_op(","):
                        msg = self.parse_expr()
                    return A.AssertStmt(loc=t.loc, cond=cond, msg=msg)
            if t.value == "test" and self.toks[self.i + 1].kind == "STRING":
                self.error("`test` blocks must be at top level")
            self.check_unsupported()
        if t.kind == "OP" and t.value == "@":
            self.error("labels / attributes inside functions not yet supported")
        e = self.parse_expr()
        # assignment
        r = self.raw()
        if r.kind == "OP" and r.value in ASSIGN_OPS:
            op = self.next().value
            self.skip_newlines()
            val = self.parse_expr()
            if op != "=":
                val = A.BinaryOp(loc=r.loc, op=op[0], left=e, right=val)
                val.compound = True
            return A.ExprStmt(loc=e.loc, expr=A.Assign(loc=r.loc, target=e, value=val))
        return A.ExprStmt(loc=e.loc, expr=e)

    def parse_for(self) -> A.ForStmt:
        t = self.next()
        if self.at_op("("):
            self.error("tuple destructuring in `for` not yet supported (use `for i in 0..len(xs)`)")
        name = self.expect_ident("loop variable")
        if not self.at_kw("in"):
            self.error(f"expected `in` after loop variable but found {self.describe(self.peek())}")
        self.next()
        self.no_struct += 1
        it = self.parse_expr()
        self.no_struct -= 1
        body = self.parse_block()
        return A.ForStmt(loc=t.loc, var=name.value, iter=it, body=body)

    # ── expressions ────────────────────────────────────────────────
    def parse_expr(self) -> A.Expr:
        return self.parse_binary(1)

    def _continue_line(self, ops) -> bool:
        """Allow a line to continue when the next line starts with one of ops."""
        if self.raw().kind != "NEWLINE" or self.nl_stack[-1]:
            return False
        j = self.i
        while self.toks[j].kind == "NEWLINE":
            j += 1
        t = self.toks[j]
        if t.kind == "OP" and t.value in ops:
            self.i = j
            return True
        return False

    def parse_binary(self, min_prec: int) -> A.Expr:
        left = self.parse_unary()
        while True:
            self._continue_line({"|>"})
            t = self.raw() if not self.nl_stack[-1] else self.peek()
            op = t.value if t.kind in ("OP", "KW") else None
            if op not in PREC or PREC[op] < min_prec:
                return left
            prec = PREC[op]
            self.next()
            self.skip_newlines_in_expr()
            if op in ("..", "..="):
                right = self.parse_binary(prec + 1)
                left = A.RangeExpr(loc=t.loc, start=left, end=right, inclusive=(op == "..="))
                continue
            if op == "|>":
                right = self.parse_binary(prec + 1)
                left = self.make_pipe(left, right, t)
                continue
            right = self.parse_binary(prec + 1)
            if op == "and":
                op = "&&"
            elif op == "or":
                op = "||"
            left = A.BinaryOp(loc=t.loc, op=op, left=left, right=right)

    def skip_newlines_in_expr(self):
        while self.toks[self.i].kind == "NEWLINE":
            self.i += 1

    def make_pipe(self, left: A.Expr, right: A.Expr, tok: Token) -> A.Expr:
        # x |> f       => f(x)
        # x |> f(a, b) => f(x, a, b)
        if isinstance(right, A.Ident):
            return A.Call(loc=right.loc, func=right, args=[left])
        if isinstance(right, A.Call) and isinstance(right.func, A.Ident):
            return A.Call(loc=right.loc, func=right.func, args=[left] + right.args)
        if isinstance(right, A.StructLit) or isinstance(right, A.Block):
            self.error("trailing-block lambdas (`{ it ... }`) not yet supported", tok)
        self.error("right side of `|>` must be a function name or call", tok)

    def parse_unary(self) -> A.Expr:
        t = self.peek()
        if t.kind == "OP" and t.value in ("-", "!"):
            self.next()
            operand = self.parse_unary()
            if t.value == "-" and isinstance(operand, A.IntLit):
                return A.IntLit(loc=t.loc, value=-operand.value)
            if t.value == "-" and isinstance(operand, A.FloatLit):
                return A.FloatLit(loc=t.loc, value=-operand.value)
            return A.UnaryOp(loc=t.loc, op=t.value, operand=operand)
        if t.kind == "KW" and t.value == "not":
            self.next()
            return A.UnaryOp(loc=t.loc, op="!", operand=self.parse_unary())
        if t.kind == "OP" and t.value == "|":
            self.error("lambdas (`|x| ...`) not yet supported")
        if t.kind == "IDENT" and t.value == "try":
            self.error("error unions / `try` not yet supported")
        e = self.parse_postfix()
        while self.at_kw("as"):
            at = self.next()
            if self.at_op("?"):
                self.error("checked casts (`as?`) not yet supported")
            e = A.CastExpr(loc=at.loc, expr=e, target=self.parse_type())
        return e

    def parse_postfix(self) -> A.Expr:
        e = self.parse_primary()
        while True:
            self._continue_line({"."})
            t = self.raw() if not self.nl_stack[-1] else self.peek()
            if t.kind != "OP":
                return e
            if t.value == "(":
                self.next()
                self.nl_stack.append(True)
                args = []
                while not self.at_op(")"):
                    if self.at("IDENT") and self.at_op(":", 1):
                        self.error("named arguments not yet supported")
                    args.append(self.parse_expr())
                    if not self.accept_op(","):
                        break
                self.nl_stack.pop()
                self.expect_op(")")
                e = A.Call(loc=e.loc, func=e, args=args)
                e.call_loc = t.loc
            elif t.value == "[":
                self.next()
                self.nl_stack.append(True)
                idx = self.parse_expr()
                self.nl_stack.pop()
                self.expect_op("]")
                e = A.Index(loc=t.loc, obj=e, index=idx)
            elif t.value == ".":
                self.next()
                f = self.peek()
                if f.kind == "INT":
                    self.error("tuples not yet supported")
                if f.kind not in ("IDENT", "KW"):
                    self.error(f"expected field or method name after `.` but found {self.describe(f)}")
                self.next()
                e = A.FieldAccess(loc=f.loc, obj=e, field=f.value)
            elif t.value == "?" :
                self.error("safe navigation / optionals (`?`) not yet supported", t)
            elif t.value == "!" and self.toks[self.i + 1].kind == "OP" and self.toks[self.i + 1].value == "!":
                self.error("force unwrap (`!!`) not yet supported", t)
            else:
                return e

    def parse_primary(self) -> A.Expr:
        t = self.peek()
        k = t.kind
        if k == "INT":
            self.next()
            return A.IntLit(loc=t.loc, value=t.value)
        if k == "FLOAT":
            self.next()
            return A.FloatLit(loc=t.loc, value=t.value)
        if k == "STRING":
            self.next()
            return self.make_string(t)
        if k == "KW":
            v = t.value
            if v in ("true", "false"):
                self.next()
                return A.BoolLit(loc=t.loc, value=(v == "true"))
            if v == "null":
                self.next()
                return A.NullLit(loc=t.loc)
            if v == "if":
                return self.parse_if()
            if v == "match":
                return self.parse_match()
            if v in ("return", "break", "continue"):
                st = self.parse_stmt()
                return A.Block(loc=t.loc, stmts=[st])
            if v == "fn":
                self.error("anonymous functions not yet supported")
            if v in ("while", "for", "loop"):
                st = self.parse_stmt()
                return A.Block(loc=t.loc, stmts=[st])
        if k == "IDENT":
            self.next()
            if (self.no_struct == 0 and t.value[:1].isupper() and self.at_op("{")
                    and self.looks_like_struct_lit()):
                return self.parse_struct_lit(t)
            return A.Ident(loc=t.loc, name=t.value)
        if k == "OP":
            if t.value == "(":
                self.next()
                self.nl_stack.append(True)
                saved = self.no_struct
                self.no_struct = 0
                if self.at_op(")"):
                    self.error("unit literal `()` / tuples not yet supported")
                e = self.parse_expr()
                if self.at_op(","):
                    self.error("tuples not yet supported")
                self.no_struct = saved
                self.nl_stack.pop()
                self.expect_op(")")
                return e
            if t.value == "[":
                self.next()
                self.nl_stack.append(True)
                saved = self.no_struct
                self.no_struct = 0
                elems = []
                while not self.at_op("]"):
                    if self.at_op("..."):
                        self.error("spread (`...xs`) not yet supported")
                    elems.append(self.parse_expr())
                    if self.at_kw("for"):
                        self.error("list comprehensions not yet supported")
                    if not self.accept_op(","):
                        break
                self.no_struct = saved
                self.nl_stack.pop()
                self.expect_op("]")
                return A.ArrayLit(loc=t.loc, elems=elems)
            if t.value == "{":
                saved = self.no_struct
                self.no_struct = 0
                b = self.parse_block()
                self.no_struct = saved
                return b
            if t.value == "#":
                self.error("set literals not yet supported")
        self.error(f"expected an expression but found {self.describe(t)}")

    def looks_like_struct_lit(self) -> bool:
        # we are at '{'; look past newlines
        j = self.i + 1
        while self.toks[j].kind == "NEWLINE":
            j += 1
        a = self.toks[j]
        if a.kind == "OP" and a.value == "}":
            return True
        if a.kind != "IDENT":
            return False
        j += 1
        while self.toks[j].kind == "NEWLINE":
            j += 1
        b = self.toks[j]
        return b.kind == "OP" and b.value in (":", ",", "}")

    def parse_struct_lit(self, name: Token) -> A.StructLit:
        self.expect_op("{")
        self.nl_stack.append(True)
        fields = []
        while not self.at_op("}"):
            fn = self.expect_ident("field name")
            if self.accept_op(":"):
                val = self.parse_expr()
            else:  # shorthand `Point { x, y }`
                val = A.Ident(loc=fn.loc, name=fn.value)
            fields.append((fn.value, val, fn.loc))
            if not self.accept_op(","):
                break
        self.nl_stack.pop()
        self.expect_op("}")
        return A.StructLit(loc=name.loc, name=name.value, fields=fields)

    def parse_if(self) -> A.IfExpr:
        t = self.next()
        if self.at_kw("let"):
            self.error("`if let` (optionals) not yet supported")
        self.no_struct += 1
        saved_nl = self.nl_stack
        self.nl_stack = [False]
        cond = self.parse_expr()
        self.nl_stack = saved_nl
        self.no_struct -= 1
        then = self.parse_block()
        els = None
        # allow `else` on the next line
        j = self.i
        while self.toks[j].kind == "NEWLINE":
            j += 1
        if self.toks[j].kind == "KW" and self.toks[j].value == "else":
            self.i = j + 1
            if self.at_kw("if"):
                els = self.parse_if()
            else:
                els = self.parse_block()
        return A.IfExpr(loc=t.loc, cond=cond, then_body=then, else_body=els)

    def parse_match(self) -> A.MatchExpr:
        t = self.next()
        self.no_struct += 1
        saved_nl = self.nl_stack
        self.nl_stack = [False]
        scrut = self.parse_expr()
        self.nl_stack = saved_nl
        self.no_struct -= 1
        lb = self.expect_op("{")
        self.nl_stack.append(False)
        arms = []
        self.skip_newlines_and_commas()
        while not (self.raw().kind == "OP" and self.raw().value == "}"):
            if self.raw().kind == "EOF":
                self.error("unclosed `match` block", lb)
            arms.append(self.parse_arm())
            r = self.raw()
            if not (r.kind in ("NEWLINE",) or (r.kind == "OP" and r.value in (",", "}", ";"))):
                self.error(f"expected newline or `,` after match arm but found {self.describe(r)}", r)
            self.skip_newlines_and_commas()
        self.nl_stack.pop()
        self.next()
        return A.MatchExpr(loc=t.loc, scrutinee=scrut, arms=arms)

    def skip_newlines_and_commas(self):
        while self.raw().kind == "NEWLINE" or (self.raw().kind == "OP" and self.raw().value in (",", ";")):
            self.i += 1

    def parse_arm(self) -> A.MatchArm:
        start = self.raw()
        pats = [self.parse_pattern()]
        while self.accept_op("|"):
            pats.append(self.parse_pattern())
        guard = None
        if self.at("IDENT", "when"):
            self.next()
            guard = self.parse_expr()
        self.expect_op("=>")
        self.skip_newlines_in_expr()
        if self.at_op("{"):
            body = self.parse_block()
        else:
            body = self.parse_expr()
            r = self.raw()
            if r.kind == "OP" and r.value in ASSIGN_OPS:
                op = self.next().value
                val = self.parse_expr()
                if op != "=":
                    val = A.BinaryOp(loc=r.loc, op=op[0], left=body, right=val)
                body = A.Block(loc=body.loc, stmts=[A.ExprStmt(loc=body.loc, expr=A.Assign(loc=r.loc, target=body, value=val))])
        arm = A.MatchArm(loc=start.loc, pattern=pats, body=body)
        arm.guard = guard
        return arm

    def parse_pattern(self) -> A.Pattern:
        t = self.peek()
        neg = False
        if t.kind == "OP" and t.value == "-":
            self.next()
            neg = True
            t = self.peek()
        if t.kind == "INT":
            self.next()
            v = -t.value if neg else t.value
            if self.at_op("..") or self.at_op("..="):
                incl = self.next().value == "..="
                n2 = False
                if self.accept_op("-"):
                    n2 = True
                e = self.peek()
                if e.kind != "INT":
                    self.error("expected integer after range in pattern")
                self.next()
                hi = -e.value if n2 else e.value
                if not incl:
                    hi -= 1
                return A.Pattern(loc=t.loc, kind="range", value=(v, hi))
            return A.Pattern(loc=t.loc, kind="int", value=v)
        if neg:
            self.error("expected number after `-` in pattern")
        if t.kind == "FLOAT":
            self.error("float literal patterns not supported (use if/else)")
        if t.kind == "STRING":
            self.next()
            if len(t.value) != 1 or not isinstance(t.value[0], str):
                self.error("interpolated strings are not allowed as patterns", t)
            return A.Pattern(loc=t.loc, kind="str", value=t.value[0])
        if t.kind == "KW" and t.value in ("true", "false"):
            self.next()
            return A.Pattern(loc=t.loc, kind="bool", value=(t.value == "true"))
        if t.kind == "IDENT":
            self.next()
            if t.value == "_":
                return A.Pattern(loc=t.loc, kind="wildcard")
            if self.at_op("(") or self.at_op("{"):
                self.error("enum / struct destructuring patterns not yet supported")
            return A.Pattern(loc=t.loc, kind="bind", value=t.value)
        if t.kind == "OP" and t.value == "[":
            self.error("list patterns not yet supported")
        self.error(f"expected a pattern (literal, `_`, or name) but found {self.describe(t)}")

    def make_string(self, t: Token) -> A.StrLit:
        parts = t.value
        if len(parts) == 1 and isinstance(parts[0], str):
            return A.StrLit(loc=t.loc, value=parts[0])
        out = []
        for p in parts:
            if isinstance(p, str):
                out.append(p)
            else:
                _, text, loc, spec = p
                toks = tokenize(text, self.file)
                # fix up token locations relative to the string
                for tk in toks:
                    if tk.loc.line == 1:
                        tk.loc = SourceLoc(self.file, loc.line, loc.col + tk.loc.col - 1)
                    else:
                        tk.loc = SourceLoc(self.file, loc.line + tk.loc.line - 1, tk.loc.col)
                sub = Parser(toks, self.source, self.file)
                sub.nl_stack = [True]
                e = sub.parse_expr()
                if sub.peek().kind != "EOF":
                    sub.error(f"unexpected {sub.describe(sub.peek())} in string interpolation")
                out.append((e, spec, loc))
        return A.StrLit(loc=t.loc, value="", parts=out)


from . import __version__ as VERSION


def parse(source: str, filename: str = "<input>") -> A.Program:
    toks = tokenize(source, filename)
    return Parser(toks, source, filename).parse_program()


def parse_repl_entry(source: str, filename: str = "<repl>"):
    """Parse one REPL entry: a mix of top-level declarations and statements."""
    toks = tokenize(source, filename)
    p = Parser(toks, source, filename)
    items = []
    p.skip_newlines()
    while p.raw().kind != "EOF":
        t = p.raw()
        if (t.kind == "KW" and t.value in ("fn", "struct", "module", "use", "pub", "extern")) or \
                (t.kind == "OP" and t.value == "@") or \
                (t.kind == "IDENT" and t.value == "test" and p.toks[p.i + 1].kind == "STRING"):
            d = p.parse_top_level()
        else:
            d = p.parse_stmt()
        if d is not None:
            items.append(d)
        p.end_stmt()
        p.skip_newlines()
    return items

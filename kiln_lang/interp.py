"""Kiln interpreter: compiles the type-checked AST into Python closures.

Used by the REPL and by `kiln run --interp` (no C compiler needed).
Semantics mirror the C backend: 64-bit wrapping Int, truncating division,
value-type structs, reference-type lists, byte-indexed strings.
"""

from __future__ import annotations
import math
import os
import sys
import time
from . import ast_nodes as A
from .ktypes import Arr, StructT, INT, FLOAT, BOOL, STR, UNIT, NEVER, prune

I_MIN, I_MAX = -2**63, 2**63 - 1
MASK = 2**64 - 1


class KilnPanic(Exception):
    def __init__(self, msg, code=101):
        super().__init__(msg)
        self.msg = msg
        self.code = code


class KilnExit(Exception):
    def __init__(self, code):
        self.code = code


class BrkExc(Exception):
    pass


class CntExc(Exception):
    pass


class RetExc(Exception):
    def __init__(self, value):
        self.value = value


BRK = "<break>"
CNT = "<continue>"


class RetSig:
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def wrap(v):
    v &= MASK
    return v - 2**64 if v > I_MAX else v


def idiv(a, b):
    if b == 0:
        raise KilnPanic("division by zero")
    q = abs(a) // abs(b)
    if (a < 0) != (b < 0):
        q = -q
    return wrap(q)


def imod(a, b):
    if b == 0:
        raise KilnPanic("division by zero")
    r = abs(a) % abs(b)
    return -r if a < 0 else r


def fdiv(a, b):
    try:
        return a / b
    except ZeroDivisionError:
        if a != a or a == 0:
            return math.nan
        return math.copysign(math.inf, a) * (math.copysign(1, b))


def fmod(a, b):
    try:
        return math.fmod(a, b)
    except ValueError:
        return math.nan


def fmt_float(v: float) -> str:
    if v != v:
        return "nan"
    if v == math.inf:
        return "inf"
    if v == -math.inf:
        return "-inf"
    s = ""
    for prec in (15, 16, 17):
        s = "%.*g" % (prec, v)
        if float(s) == v:
            break
    if not any(c in s for c in ".eEn"):
        s += ".0"
    return s


# ── byte-semantics string helpers (fast path for ASCII) ─────────────
def s_len(s: str) -> int:
    return len(s) if s.isascii() else len(s.encode("utf-8"))


def s_at(s: str, i: int) -> str:
    if s.isascii():
        try:
            return s[i]
        except IndexError:
            raise KilnPanic(f"index {i} out of bounds (length {len(s)})")
    b = s.encode("utf-8")
    try:
        return bytes([b[i]]).decode("utf-8", "replace")
    except IndexError:
        raise KilnPanic(f"index {i} out of bounds (length {len(b)})")


def slice_bounds(n, a, b):
    a2 = a + n if a < 0 else a
    b2 = b + n if b < 0 else b
    if a2 < 0 or b2 > n or a2 > b2:
        raise KilnPanic(f"slice {a}..{b} out of bounds (length {n})")
    return a2, b2


def s_slice(s: str, a: int, b: int) -> str:
    if s.isascii():
        a, b = slice_bounds(len(s), a, b)
        return s[a:b]
    by = s.encode("utf-8")
    a, b = slice_bounds(len(by), a, b)
    return by[a:b].decode("utf-8", "replace")


def s_find(s: str, sub: str) -> int:
    if s.isascii() and sub.isascii():
        return s.find(sub)
    return s.encode("utf-8").find(sub.encode("utf-8"))


def repr_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'


def pad(s, fill, align, zero, width, numeric):
    n = s_len(s)
    if n >= width:
        return s
    p = width - n
    if zero and not align:
        sign = ""
        if s and s[0] in "+-":
            sign, s = s[0], s[1:]
        return sign + "0" * p + s
    if not align:
        align = ">" if numeric else "<"
    left = p if align == ">" else (p // 2 if align == "^" else 0)
    return fill * left + s + fill * (p - left)


def fmt_int(v, fs):
    fill, align, zero, width, prec, typ = fs
    if typ in ("x", "X", "o", "b"):
        base = {"x": "x", "X": "X", "o": "o", "b": "b"}[typ]
        s = format(abs(v), base)
        if v < 0:
            s = "-" + s
    elif typ in ("f", "e") or prec >= 0:
        s = ("%.*e" if typ == "e" else "%.*f") % (prec if prec >= 0 else 6, float(v))
    else:
        s = str(v)
    return pad(s, fill, align, zero, width, True)


def fmt_float_spec(v, fs):
    fill, align, zero, width, prec, typ = fs
    if typ == "e":
        s = "%.*e" % (prec if prec >= 0 else 6, v)
    elif prec >= 0 or typ == "f":
        s = "%.*f" % (prec if prec >= 0 else 6, v)
    else:
        s = fmt_float(v)
    return pad(s, fill, align, zero, width, True)


def fmt_str(s, fs):
    fill, align, zero, width, prec, typ = fs
    if prec >= 0 and s_len(s) > prec:
        s = s_slice(s, 0, prec)
    return pad(s, fill, align, zero, width, False)


def to_str_fn(t, repr_mode=False):
    """Return a function converting a runtime value of type t to its Kiln string form."""
    t = prune(t)
    if t is INT:
        return str
    if t is FLOAT:
        return fmt_float
    if t is BOOL:
        return lambda v: "true" if v else "false"
    if t is STR:
        return repr_str if repr_mode else (lambda v: v)
    if isinstance(t, Arr):
        el = to_str_fn(t.elem, True)
        return lambda v: "[" + ", ".join(el(x) for x in v) + "]"
    if isinstance(t, StructT):
        def f(v, t=t):
            fs = [f"{n}: {to_str_fn(ft, True)(v[i])}" for i, (n, ft) in enumerate(t.fields)]
            return t.name + " { " + ", ".join(fs) + " }" if fs else t.name + " {}"
        return f
    raise ValueError(f"cannot stringify {t!r}")


def copier(t):
    """Return a function that copies a value of type t with C value semantics, or None if no copy needed."""
    t = prune(t)
    if not isinstance(t, StructT):
        return None
    subs = [(i, copier(ft)) for i, (_, ft) in enumerate(t.fields)]
    subs = [(i, c) for i, c in subs if c is not None]
    if not subs:
        return list

    def cp(v):
        r = list(v)
        for i, c in subs:
            r[i] = c(r[i])
        return r
    return cp


def is_place(e) -> bool:
    return isinstance(e, (A.Ident, A.Index)) or (isinstance(e, A.FieldAccess) and getattr(e, "kind", None) == "field")


class Interp:
    def __init__(self, out=None):
        self.fns: dict[str, list] = {}       # name -> [callable]
        self.G: list = []                    # global slots
        self.gslot: dict[int, int] = {}      # id(Symbol) -> global slot
        self.out = out or sys.stdout
        self.err_out = sys.stderr
        self.argv: list[str] = []

    # ── slots ──────────────────────────────────────────────────────
    def global_slot(self, sym) -> int:
        k = id(sym)
        if k not in self.gslot:
            self.gslot[k] = len(self.G)
            self.G.append(None)
            sym._keep = True
        return self.gslot[k]

    def local_slot(self, sym) -> int:
        k = id(sym)
        if k not in self.lslots:
            self.lslots[k] = len(self.lslots)
        return self.lslots[k]

    def slot_of(self, sym):
        if sym.kind == "global":
            return ("g", self.global_slot(sym))
        return ("l", self.local_slot(sym))

    # ── functions ──────────────────────────────────────────────────
    def define_fn(self, decl: A.FnDecl):
        cell = self.fns.setdefault(decl.name, [None])
        self.lslots = {}
        info = decl.info
        pslots = [self.local_slot(s) for s in decl.param_syms]
        copies = [copier(t) for _, t in info.params]
        want = prune(info.ret) is not UNIT
        body = self.value_block(decl.body, want, fn_body=True)
        nparams = len(pslots)
        nslots_ref = self.lslots  # grows while compiling; read after

        nslots = len(nslots_ref)
        name = decl.name

        if nparams == 1 and copies[0] is None:
            extra = [None] * (nslots - 1)

            def fn(a):
                fr = [a, *extra]
                try:
                    return body(fr)
                except RetExc as r:
                    return r.value
        else:
            extra = [None] * (nslots - nparams)

            def fn(*args):
                fr = list(args)
                for i, c in enumerate(copies):
                    if c is not None:
                        fr[i] = c(fr[i])
                fr.extend(extra)
                try:
                    return body(fr)
                except RetExc as r:
                    return r.value
        fn.__name__ = "kiln_" + name
        cell[0] = fn
        return fn

    def compile_toplevel_stmts(self, stmts):
        """Compile REPL/global statements; returns runner(fr) and frame size."""
        self.lslots = {}
        runner = self.stmt_block(stmts)
        return runner, len(self.lslots)

    # ── blocks ─────────────────────────────────────────────────────
    def value_block(self, b: A.Block, want_value: bool, fn_body=False):
        """Closure that runs the block and returns its value.
        Signals (break/continue/return) from inside are raised as exceptions,
        except that in a function body a `return` signal returns its value."""
        stmts = b.stmts
        if not stmts:
            return lambda fr: None
        last = stmts[-1]
        tail_expr = None
        if want_value and isinstance(last, A.ExprStmt):
            tail_expr = self.expr(last.expr)
            head = [self.stmt(s) for s in stmts[:-1]]
        elif want_value and isinstance(last, A.ReturnStmt) and fn_body and last.value is not None:
            tail_expr = self.expr(last.value)
            head = [self.stmt(s) for s in stmts[:-1]]
        else:
            head = [self.stmt(s) for s in stmts]

        def signal(r):
            if r is BRK:
                raise BrkExc()
            if r is CNT:
                raise CntExc()
            raise RetExc(r.value)

        if fn_body:
            def signal(r):  # noqa: F811
                if r is BRK or r is CNT:
                    raise KilnPanic("internal: break outside loop")
                return r.value

        if tail_expr is not None:
            if not head:
                return tail_expr
            if len(head) == 1:
                h0 = head[0]

                def run1(fr):
                    r = h0(fr)
                    if r is not None:
                        return signal(r)
                    return tail_expr(fr)
                return run1

            def run(fr):
                for h in head:
                    r = h(fr)
                    if r is not None:
                        return signal(r)
                return tail_expr(fr)
            return run

        def run_nov(fr):
            for h in head:
                r = h(fr)
                if r is not None:
                    return signal(r)
            return None
        return run_nov

    def stmt_block(self, stmts):
        """Closure that runs statements, returning None or a signal."""
        fs = [self.stmt(s) for s in stmts]
        if not fs:
            return lambda fr: None
        if len(fs) == 1:
            return fs[0]
        if len(fs) == 2:
            a, b = fs

            def run2(fr):
                r = a(fr)
                if r is not None:
                    return r
                return b(fr)
            return run2

        def run(fr):
            for f in fs:
                r = f(fr)
                if r is not None:
                    return r
            return None
        return run

    # ── statements ─────────────────────────────────────────────────
    def store(self, sym, valf):
        kind, i = self.slot_of(sym)
        if kind == "l":
            def st(fr):
                fr[i] = valf(fr)
        else:
            G = self.G

            def st(fr):
                G[i] = valf(fr)
        return st

    def maybe_copy(self, e, f):
        c = copier(e.ty) if is_place(e) else None
        if c is None:
            return f
        return lambda fr: c(f(fr))

    def stmt(self, s):
        if isinstance(s, A.ExprStmt):
            return self.expr_stmt(s.expr)
        if isinstance(s, A.LetStmt):
            v = self.maybe_copy(s.value, self.expr(s.value))
            return self.store(s.sym, v)
        if isinstance(s, A.ReturnStmt):
            if s.value is None:
                return lambda fr: RetSig(None)
            v = self.expr(s.value)
            return lambda fr: RetSig(v(fr))
        if isinstance(s, A.BreakStmt):
            return lambda fr: BRK
        if isinstance(s, A.ContinueStmt):
            return lambda fr: CNT
        if isinstance(s, A.WhileStmt):
            cond = self.expr(s.cond)
            body = self.stmt_block(s.body.stmts)

            def w(fr):
                while cond(fr):
                    try:
                        r = body(fr)
                    except BrkExc:
                        break
                    except CntExc:
                        continue
                    if r is not None:
                        if r is BRK:
                            break
                        if r is CNT:
                            continue
                        return r
                return None
            return w
        if isinstance(s, A.LoopStmt):
            body = self.stmt_block(s.body.stmts)

            def lp(fr):
                while True:
                    try:
                        r = body(fr)
                    except BrkExc:
                        break
                    except CntExc:
                        continue
                    if r is not None:
                        if r is BRK:
                            break
                        if r is CNT:
                            continue
                        return r
                return None
            return lp
        if isinstance(s, A.ForStmt):
            return self.for_stmt(s)
        if isinstance(s, A.GuardStmt):
            cond = self.expr(s.cond)
            els = self.stmt_block(s.else_body.stmts)

            def g(fr):
                if not cond(fr):
                    r = els(fr)
                    if r is None:
                        raise KilnPanic("guard else-block did not exit")
                    return r
                return None
            return g
        if isinstance(s, A.AssertStmt):
            cond = self.expr(s.cond)
            msg = self.expr(s.msg) if s.msg is not None else (lambda fr: "assert")
            where = f"{s.loc.file}:{s.loc.line}:{s.loc.col}"

            def asrt(fr):
                if not cond(fr):
                    raise KilnPanic(f"assertion failed at {where}: {msg(fr)}", code=101)
                return None
            return asrt
        raise NotImplementedError(type(s).__name__)

    def expr_stmt(self, e):
        """Statement-context expression: if/match/blocks propagate signals."""
        if isinstance(e, A.IfExpr):
            return self.if_stmt(e)
        if isinstance(e, A.MatchExpr):
            return self.match_(e, stmt_mode=True)
        if isinstance(e, A.Block):
            return self.stmt_block(e.stmts)
        f = self.expr(e)

        def es(fr):
            f(fr)
        return es

    def if_stmt(self, e):
        cond = self.expr(e.cond)
        then = self.stmt_block(e.then_body.stmts)
        if e.else_body is None:
            def f(fr):
                if cond(fr):
                    return then(fr)
                return None
            return f
        if isinstance(e.else_body, A.IfExpr):
            els = self.if_stmt(e.else_body)
        else:
            els = self.stmt_block(e.else_body.stmts)

        def f2(fr):
            if cond(fr):
                return then(fr)
            return els(fr)
        return f2

    def for_stmt(self, s):
        kind, i = self.slot_of(s.sym)
        body = self.stmt_block(s.body.stmts)
        G = self.G

        def run_body_loop(it, fr):
            if kind == "l":
                for v in it:
                    fr[i] = v
                    try:
                        r = body(fr)
                    except BrkExc:
                        break
                    except CntExc:
                        continue
                    if r is not None:
                        if r is BRK:
                            break
                        if r is CNT:
                            continue
                        return r
            else:
                for v in it:
                    G[i] = v
                    try:
                        r = body(fr)
                    except BrkExc:
                        break
                    except CntExc:
                        continue
                    if r is not None:
                        if r is BRK:
                            break
                        if r is CNT:
                            continue
                        return r
            return None

        if s.kind == "range":
            a = self.expr(s.iter.start)
            b = self.expr(s.iter.end)
            incl = 1 if s.iter.inclusive else 0

            def fr_range(fr):
                return run_body_loop(range(a(fr), b(fr) + incl), fr)
            return fr_range
        it = self.expr(s.iter)
        if s.kind == "array":
            c = copier(prune(s.iter.ty).elem)

            def fr_arr(fr):
                arr = it(fr)
                # index-based so that pushes during iteration are seen (like the C backend)
                def gen():
                    k = 0
                    while k < len(arr):
                        yield arr[k] if c is None else c(arr[k])
                        k += 1
                return run_body_loop(gen(), fr)
            return fr_arr

        def fr_str(fr):
            v = it(fr)
            if v.isascii():
                seq = v
            else:
                seq = [bytes([x]).decode("utf-8", "replace") for x in v.encode("utf-8")]
            return run_body_loop(seq, fr)
        return fr_str

    # ── expressions ────────────────────────────────────────────────
    def expr(self, e):
        return getattr(self, "e_" + type(e).__name__)(e)

    def e_IntLit(self, e):
        v = float(e.value) if getattr(e, "as_float", False) else e.value
        return lambda fr: v

    def e_FloatLit(self, e):
        v = e.value
        return lambda fr: v

    def e_BoolLit(self, e):
        v = e.value
        return lambda fr: v

    def e_StrLit(self, e):
        if e.parts is None:
            v = e.value
            return lambda fr: v
        fs = []
        for p in e.parts:
            if isinstance(p, str):
                fs.append(lambda fr, p=p: p)
                continue
            ex, spec, _ = p
            f = self.expr(ex)
            t = prune(ex.ty)
            if spec is None:
                conv = to_str_fn(t)
                if conv is str or t is STR:
                    if t is STR:
                        fs.append(f)
                    else:
                        fs.append(lambda fr, f=f: str(f(fr)))
                else:
                    fs.append(lambda fr, f=f, conv=conv: conv(f(fr)))
            else:
                if t is INT:
                    fs.append(lambda fr, f=f, spec=spec: fmt_int(f(fr), spec))
                elif t is FLOAT:
                    fs.append(lambda fr, f=f, spec=spec: fmt_float_spec(f(fr), spec))
                else:
                    conv = to_str_fn(t)
                    fs.append(lambda fr, f=f, spec=spec, conv=conv: fmt_str(conv(f(fr)), spec))
        return lambda fr: "".join([f(fr) for f in fs])

    def e_Ident(self, e):
        kind, i = self.slot_of(e.sym)
        if kind == "l":
            return lambda fr: fr[i]
        G = self.G
        return lambda fr: G[i]

    def e_UnaryOp(self, e):
        f = self.expr(e.operand)
        if e.op == "!":
            return lambda fr: not f(fr)
        if prune(e.ty) is INT:
            return lambda fr: wrap(-f(fr)) if f(fr) == I_MIN else -f(fr)
        return lambda fr: -f(fr)

    def e_BinaryOp(self, e):
        op = e.op
        if op == "in":
            return self.in_(e)
        l = self.expr(e.left)
        r = self.expr(e.right)
        if op == "&&":
            return lambda fr: l(fr) and r(fr)
        if op == "||":
            return lambda fr: l(fr) or r(fr)
        return self.binop(op, prune(e.left.ty), l, r)

    def binop(self, op, lt, l, r):
        if lt is INT:
            if op == "+":
                def add(fr):
                    v = l(fr) + r(fr)
                    return v if I_MIN <= v <= I_MAX else wrap(v)
                return add
            if op == "-":
                def sub(fr):
                    v = l(fr) - r(fr)
                    return v if I_MIN <= v <= I_MAX else wrap(v)
                return sub
            if op == "*":
                def mul(fr):
                    v = l(fr) * r(fr)
                    return v if I_MIN <= v <= I_MAX else wrap(v)
                return mul
            if op == "/":
                return lambda fr: idiv(l(fr), r(fr))
            if op == "%":
                def mod(fr):
                    a = l(fr)
                    b = r(fr)
                    if a >= 0 and b > 0:
                        return a % b
                    return imod(a, b)
                return mod
            if op == "&":
                return lambda fr: l(fr) & r(fr)
            if op == "|":
                return lambda fr: l(fr) | r(fr)
            if op == "^":
                return lambda fr: l(fr) ^ r(fr)
            if op == "<<":
                return lambda fr: wrap(l(fr) << (r(fr) & 63))
            if op == ">>":
                return lambda fr: l(fr) >> (r(fr) & 63)
        if lt is FLOAT:
            if op == "/":
                return lambda fr: fdiv(l(fr), r(fr))
            if op == "%":
                return lambda fr: fmod(l(fr), r(fr))
        if lt is STR and op in ("<", ">", "<=", ">="):
            # byte-wise ordering like memcmp
            cmp = {"<": lambda a, b: a < b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b}[op]
            return lambda fr: cmp(l(fr).encode("utf-8"), r(fr).encode("utf-8"))
        table = {
            "+": lambda fr: l(fr) + r(fr), "-": lambda fr: l(fr) - r(fr), "*": lambda fr: l(fr) * r(fr),
            "==": lambda fr: l(fr) == r(fr), "!=": lambda fr: l(fr) != r(fr),
            "<": lambda fr: l(fr) < r(fr), ">": lambda fr: l(fr) > r(fr),
            "<=": lambda fr: l(fr) <= r(fr), ">=": lambda fr: l(fr) >= r(fr),
        }
        return table[op]

    def in_(self, e):
        if e.in_kind == "range":
            x = self.expr(e.left)
            a = self.expr(e.right.start)
            b = self.expr(e.right.end)
            if e.right.inclusive:
                return lambda fr: a(fr) <= x(fr) <= b(fr)
            return lambda fr: a(fr) <= x(fr) < b(fr)
        x = self.expr(e.left)
        c = self.expr(e.right)
        return lambda fr: x(fr) in c(fr)

    def e_CastExpr(self, e):
        f = self.expr(e.expr)
        src, dst = prune(e.expr.ty), prune(e.ty)
        if dst is STR:
            conv = to_str_fn(src)
            return lambda fr: conv(f(fr))
        if src is dst:
            return f
        if dst is FLOAT:
            return lambda fr: float(f(fr))
        if dst is INT and src is FLOAT:
            def f2i(fr):
                v = f(fr)
                if v != v or v in (math.inf, -math.inf):
                    return I_MIN
                return wrap(int(v))
            return f2i
        if dst is INT and src is BOOL:
            return lambda fr: 1 if f(fr) else 0
        return f

    def e_ArrayLit(self, e):
        fs = [self.maybe_copy(x, self.expr(x)) for x in e.elems]
        if not fs:
            return lambda fr: []
        return lambda fr: [f(fr) for f in fs]

    def e_StructLit(self, e):
        fs = [self.maybe_copy(v, self.expr(v)) for v in e.ordered]
        return lambda fr: [f(fr) for f in fs]

    def e_Index(self, e):
        obj = self.expr(e.obj)
        if e.kind in ("arr_slice", "str_slice"):
            a = self.expr(e.index.start)
            b = self.expr(e.index.end)
            inc = 1 if e.inclusive else 0
            if e.kind == "str_slice":
                return lambda fr: s_slice(obj(fr), a(fr), b(fr) + inc)

            def asl(fr):
                arr = obj(fr)
                x, y = slice_bounds(len(arr), a(fr), b(fr) + inc)
                return arr[x:y]
            return asl
        idx = self.expr(e.index)
        if e.kind == "str":
            return lambda fr: s_at(obj(fr), idx(fr))

        def at(fr):
            arr = obj(fr)
            i = idx(fr)
            try:
                return arr[i]
            except IndexError:
                raise KilnPanic(f"index {i} out of bounds (length {len(arr)})")
        return at

    def field_index(self, e):
        st = prune(e.obj.ty)
        for i, (n, _) in enumerate(st.fields):
            if n == e.field:
                return i
        raise KeyError(e.field)

    def e_FieldAccess(self, e):
        if e.kind == "call":
            return self.expr(e.as_call)
        obj = self.expr(e.obj)
        i = self.field_index(e)
        return lambda fr: obj(fr)[i]

    def e_Assign(self, e):
        tgt = e.target
        val = self.maybe_copy(e.value, self.expr(e.value))
        if isinstance(tgt, A.Ident):
            st = self.store(tgt.sym, val)

            def a1(fr):
                st(fr)
            return a1
        if isinstance(tgt, A.Index):
            obj = self.expr(tgt.obj)
            idx = self.expr(tgt.index)
            if getattr(e.value, "compound", False):
                rhs = self.expr(e.value.right)
                op = self.binop(e.value.op, prune(tgt.ty), lambda fr: fr[-1][0][fr[-1][1]], rhs)

                def ac(fr):
                    arr = obj(fr)
                    i = idx(fr)
                    try:
                        cur = arr[i]
                    except IndexError:
                        raise KilnPanic(f"index {i} out of bounds (length {len(arr)})")
                    fr.append((arr, i))
                    try:
                        v = op(fr)
                    finally:
                        fr.pop()
                    arr[i] = v
                return ac

            def ai(fr):
                arr = obj(fr)
                i = idx(fr)
                v = val(fr)
                try:
                    arr[i] = v
                except IndexError:
                    raise KilnPanic(f"index {i} out of bounds (length {len(arr)})")
            return ai
        if isinstance(tgt, A.FieldAccess):
            obj = self.expr(tgt.obj)
            i = self.field_index(tgt)

            def af(fr):
                o = obj(fr)
                o[i] = val(fr)
            return af
        raise NotImplementedError("assign target")

    def e_Block(self, e):
        return self.value_block(e, bool(e.used) and prune(e.ty) not in (UNIT, NEVER))

    def e_IfExpr(self, e):
        want = bool(e.used) and prune(e.ty) not in (UNIT, NEVER)
        cond = self.expr(e.cond)
        then = self.value_block(e.then_body, want)
        if e.else_body is None:
            return lambda fr: then(fr) if cond(fr) else None
        els = self.expr(e.else_body) if isinstance(e.else_body, A.IfExpr) else self.value_block(e.else_body, want)
        return lambda fr: then(fr) if cond(fr) else els(fr)

    def e_MatchExpr(self, e):
        return self.match_(e, stmt_mode=False)

    def match_(self, e, stmt_mode):
        want = (not stmt_mode) and bool(e.used) and prune(e.ty) not in (UNIT, NEVER)
        scrut = self.expr(e.scrutinee)
        arms = []
        # Fast path: pure literal dispatch table for Int/Str without guards
        for arm in e.arms:
            tests = []
            bind = None
            for p in arm.pattern:
                if p.kind in ("wildcard",):
                    tests.append(None)
                elif p.kind == "bind":
                    bind = self.slot_of(p.sym)
                    tests.append(None)
                elif p.kind == "range":
                    lo, hi = p.value
                    tests.append(lambda v, lo=lo, hi=hi: lo <= v <= hi)
                else:
                    tests.append(p.value)
            guard = self.expr(arm.guard) if arm.guard is not None else None
            if isinstance(arm.body, A.Block):
                body = self.stmt_block(arm.body.stmts) if stmt_mode else self.value_block(arm.body, want)
            else:
                body = self.expr_stmt(arm.body) if stmt_mode else self.expr(arm.body)
            arms.append((tests, bind, guard, body))
        G = self.G

        def m(fr):
            v = scrut(fr)
            for tests, bind, guard, body in arms:
                ok = False
                for t in tests:
                    if t is None:
                        ok = True
                        break
                    if callable(t):
                        if t(v):
                            ok = True
                            break
                    elif t == v and type(t) is type(v):
                        ok = True
                        break
                if not ok:
                    continue
                if bind is not None:
                    if bind[0] == "l":
                        fr[bind[1]] = v
                    else:
                        G[bind[1]] = v
                if guard is not None and not guard(fr):
                    continue
                return body(fr)
            return None
        return m

    # ── calls ──────────────────────────────────────────────────────
    def e_Call(self, e):
        kind, tgt = e.target
        if kind == "user":
            cell = self.fns.setdefault(tgt.name, [None])
            args = [self.expr(a) for a in e.cargs]
            n = len(args)
            if n == 0:
                return lambda fr: cell[0]()
            if n == 1:
                a0 = args[0]
                return lambda fr: cell[0](a0(fr))
            if n == 2:
                a0, a1 = args
                return lambda fr: cell[0](a0(fr), a1(fr))
            return lambda fr: cell[0](*[a(fr) for a in args])
        return self.builtin(tgt, e)

    def builtin(self, name, e):
        a = e.cargs
        fs = [self.expr(x) for x in a]
        interp = self
        if name in ("print", "println", "eprint", "eprintln"):
            to_err = name.startswith("e")
            nl = "\n" if name.endswith("ln") else ""
            convs = [to_str_fn(x.ty) for x in a]
            pairs = list(zip(fs, convs))

            def pr(fr):
                s = " ".join(c(f(fr)) for f, c in pairs) + nl
                (interp.err_out if to_err else interp.out).write(s)
            return pr
        if name in ("str", "to_str"):
            conv = to_str_fn(a[0].ty)
            f = fs[0]
            return lambda fr: conv(f(fr))
        if name == "len":
            f = fs[0]
            if prune(a[0].ty) is STR:
                return lambda fr: s_len(f(fr))
            return lambda fr: len(f(fr))
        if name == "push":
            f, v = fs
            v = self.maybe_copy(a[1], v)
            return lambda fr: f(fr).append(v(fr))
        if name == "pop":
            f = fs[0]

            def pop(fr):
                arr = f(fr)
                if not arr:
                    raise KilnPanic("pop from empty list")
                return arr.pop()
            return pop
        if name == "clear":
            f = fs[0]
            return lambda fr: f(fr).clear()
        if name == "fill":
            n, v = fs
            c = copier(prune(e.ty).elem)
            if c is None:
                return lambda fr: [v(fr)] * max(0, n(fr))

            def fill(fr):
                x = v(fr)
                return [c(x) for _ in range(max(0, n(fr)))]
            return fill
        if name == "contains":
            x, y = fs
            if e.variant == "str":
                return lambda fr: s_find(x(fr), y(fr)) >= 0
            return lambda fr: y(fr) in x(fr)
        if name == "sort":
            f = fs[0]
            if prune(prune(a[0].ty).elem) is STR:
                return lambda fr: sorted(f(fr), key=lambda s: s.encode("utf-8"))
            return lambda fr: sorted(f(fr))
        if name == "reverse":
            f = fs[0]
            return lambda fr: f(fr)[::-1]
        if name == "sum":
            f = fs[0]
            if prune(prune(a[0].ty).elem) is INT:
                return lambda fr: wrap(sum(f(fr)))
            return lambda fr: math.fsum(f(fr)) if False else sum(f(fr), 0.0)
        if name == "join":
            f = fs[0]
            sep = fs[1] if len(fs) > 1 else (lambda fr: "")
            return lambda fr: sep(fr).join(f(fr))
        if name == "split":
            s, sep = fs

            def split(fr):
                x, y = s(fr), sep(fr)
                if y == "":
                    return [ch for ch in x] if x.isascii() else [bytes([b]).decode("utf-8", "replace") for b in x.encode()]
                return x.split(y)
            return split
        if name == "split_whitespace":
            f = fs[0]
            return lambda fr: f(fr).split()
        if name == "lines":
            f = fs[0]

            def lines(fr):
                x = f(fr)
                parts = x.split("\n")
                if parts and parts[-1] == "":
                    parts.pop()
                return [p[:-1] if p.endswith("\r") else p for p in parts]
            return lines
        simple = {
            "trim": lambda s: s.strip(" \t\n\r\v\f"),
            "to_upper": lambda s: "".join(c.upper() if "a" <= c <= "z" else c for c in s),
            "to_lower": lambda s: "".join(c.lower() if "A" <= c <= "Z" else c for c in s),
            "starts_with": lambda s, p: s.startswith(p),
            "ends_with": lambda s, p: s.endswith(p),
            "replace": lambda s, x, y: s.replace(x, y) if x else s,
            "repeat": lambda s, n: s * max(0, n),
            "find": s_find,
            "sqrt": lambda x: math.sqrt(x) if x >= 0 else math.nan,
            "floor": lambda x: float(math.floor(x)) if math.isfinite(x) else x,
            "ceil": lambda x: float(math.ceil(x)) if math.isfinite(x) else x,
            "sin": math.sin, "cos": math.cos,
            "pow": lambda x, y: _pow(x, y),
            "now_ms": lambda: int(time.time() * 1000),
            "env": lambda n: os.environ.get(n, ""),
            "parse_int": _parse_int, "parse_float": _parse_float,
            "fs.read_text": _read_text, "fs.write_text": _write_text,
            "fs.exists": lambda p: os.path.isfile(p),
        }
        if name in simple:
            fn = simple[name]
            if len(fs) == 0:
                return lambda fr: fn()
            if len(fs) == 1:
                f0 = fs[0]
                return lambda fr: fn(f0(fr))
            return lambda fr: fn(*[f(fr) for f in fs])
        if name == "abs":
            f = fs[0]
            if prune(a[0].ty) is INT:
                return lambda fr: wrap(abs(f(fr)))
            return lambda fr: abs(f(fr))
        if name in ("min", "max"):
            x, y = fs
            if prune(a[0].ty) is STR:
                if name == "min":
                    return lambda fr: min(x(fr), y(fr), key=lambda s: s.encode())
                return lambda fr: max(y(fr), x(fr), key=lambda s: s.encode()) if False else _smax(x(fr), y(fr))
            if name == "min":
                return lambda fr: (lambda p, q: p if p < q else q)(x(fr), y(fr))
            return lambda fr: (lambda p, q: p if p > q else q)(x(fr), y(fr))
        if name == "exit":
            f = fs[0]

            def ex(fr):
                raise KilnExit(f(fr))
            return ex
        if name == "panic":
            f = fs[0]

            def pn(fr):
                raise KilnPanic(f(fr))
            return pn
        if name == "assert":
            c = fs[0]
            m = fs[1] if len(fs) > 1 else (lambda fr: "assert")
            where = f"{e.loc.file}:{e.loc.line}:{e.loc.col}"

            def asrt(fr):
                if not c(fr):
                    raise KilnPanic(f"assertion failed at {where}: {m(fr)}")
            return asrt
        if name in ("ask", "read_line"):
            p = fs[0] if fs else (lambda fr: "")

            def ask(fr):
                interp.out.write(p(fr))
                interp.out.flush()
                line = sys.stdin.readline()
                if line.endswith("\n"):
                    line = line[:-1]
                if line.endswith("\r"):
                    line = line[:-1]
                return line
            return ask
        raise NotImplementedError(name)


def _smax(a, b):
    return a if a.encode() >= b.encode() else b


def _pow(x, y):
    try:
        return math.pow(x, y)
    except (OverflowError, ValueError):
        try:
            return float(x ** y) if not isinstance(x ** y, complex) else math.nan
        except OverflowError:
            return math.inf


def _read_text(p):
    try:
        with open(p, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        raise KilnPanic(f"fs.read_text: cannot open file: {p}")


def _write_text(p, t):
    try:
        with open(p, "wb") as f:
            f.write(t.encode("utf-8"))
    except OSError:
        raise KilnPanic(f"fs.write_text: cannot open file: {p}")


def _parse_int(s):
    t = s.strip(" \t\n\r\v\f")
    try:
        if not t or t[0] in "_" or "_" in t:
            raise ValueError
        v = int(t, 10)
    except ValueError:
        raise KilnPanic(f"parse_int: invalid integer: {s}")
    if not (I_MIN <= v <= I_MAX):
        return I_MAX if v > 0 else I_MIN
    return v


def _parse_float(s):
    t = s.strip(" \t\n\r\v\f")
    try:
        if "_" in t:
            raise ValueError
        return float(t)
    except ValueError:
        raise KilnPanic(f"parse_float: invalid number: {s}")


def run_in_big_stack(fn):
    """Run fn() in a thread with a large stack so deep Kiln recursion works."""
    import threading
    result = {}

    def target():
        try:
            result["v"] = fn()
        except BaseException as ex:  # noqa: BLE001
            result["e"] = ex
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, 1_500_000))
    for size in (1024, 512, 256, 64):
        try:
            threading.stack_size(size * 1024 * 1024)
            break
        except (ValueError, RuntimeError, OverflowError):
            continue
    t = threading.Thread(target=target)
    t.start()
    t.join()
    if "e" in result:
        raise result["e"]
    return result.get("v")


def load_program(interp: Interp, prog: A.Program, ck):
    """Define all functions and initialize globals of a checked program."""
    for d in prog.decls:
        if isinstance(d, A.FnDecl):
            interp.define_fn(d)
    for g in ck.global_inits:
        runner, n = interp.compile_toplevel_stmts([g])
        runner([None] * n)


def run_program(prog: A.Program, ck, argv: list[str]) -> int:
    interp = Interp()

    def go():
        load_program(interp, prog, ck)
        main = interp.fns["main"][0]
        info = ck.fns["main"]
        if info.params:
            rc = main(list(argv))
        else:
            rc = main()
        return rc if isinstance(rc, int) and not isinstance(rc, bool) else 0

    try:
        rc = run_in_big_stack(go)
        sys.stdout.flush()
        return rc
    except KilnPanic as p:
        sys.stdout.flush()
        print(f"panic: {p.msg}" if not p.msg.startswith("assertion failed") else p.msg, file=sys.stderr)
        return p.code
    except KilnExit as ex:
        sys.stdout.flush()
        return ex.code
    except RecursionError:
        sys.stdout.flush()
        print("panic: stack overflow (recursion too deep)", file=sys.stderr)
        return 101

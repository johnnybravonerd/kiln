"""Kiln Abstract Syntax Tree node definitions."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional
from .errors import SourceLoc


@dataclass
class Node:
    loc: SourceLoc | None = None


# ── Types (syntactic) ──────────────────────────────────────────────

@dataclass
class TypeExpr(Node):
    pass

@dataclass
class NamedType(TypeExpr):
    name: str = ""

@dataclass
class ArrayType(TypeExpr):
    elem: TypeExpr | None = None

@dataclass
class FuncType(TypeExpr):
    params: list[TypeExpr] = field(default_factory=list)
    ret: TypeExpr | None = None


# ── Expressions ────────────────────────────────────────────────────

@dataclass
class Expr(Node):
    pass

@dataclass
class IntLit(Expr):
    value: int = 0

@dataclass
class FloatLit(Expr):
    value: float = 0.0

@dataclass
class BoolLit(Expr):
    value: bool = False

@dataclass
class StrLit(Expr):
    value: str = ""
    # parts: list of (literal_str | Expr) for interpolation; None = plain
    parts: list | None = None

@dataclass
class NullLit(Expr):
    """`null` — a null Ptr."""
    pass

@dataclass
class Ident(Expr):
    name: str = ""

@dataclass
class BinaryOp(Expr):
    op: str = ""
    left: Expr | None = None
    right: Expr | None = None

@dataclass
class UnaryOp(Expr):
    op: str = ""
    operand: Expr | None = None

@dataclass
class Call(Expr):
    func: Expr | None = None
    args: list[Expr] = field(default_factory=list)

@dataclass
class Index(Expr):
    obj: Expr | None = None
    index: Expr | None = None

@dataclass
class FieldAccess(Expr):
    obj: Expr | None = None
    field: str = ""

@dataclass
class ArrayLit(Expr):
    elems: list[Expr] = field(default_factory=list)

@dataclass
class StructLit(Expr):
    name: str = ""
    fields: list[tuple[str, Expr]] = field(default_factory=list)  # (name or "", expr)

@dataclass
class RangeExpr(Expr):
    start: Expr | None = None
    end: Expr | None = None
    inclusive: bool = False

@dataclass
class IfExpr(Expr):
    cond: Expr | None = None
    then_body: "Block" | None = None
    else_body: "Block | IfExpr | None" = None

@dataclass
class MatchExpr(Expr):
    scrutinee: Expr | None = None
    arms: list["MatchArm"] = field(default_factory=list)

@dataclass
class MatchArm(Node):
    pattern: "Pattern | None" = None
    body: Expr | None = None  # can be Block or Expr

@dataclass
class Pattern(Node):
    kind: str = ""  # "int", "str", "ident", "wildcard"
    value: Any = None

@dataclass
class Assign(Expr):
    target: Expr | None = None
    value: Expr | None = None

@dataclass
class Block(Expr):
    stmts: list = field(default_factory=list)  # Stmt | Expr


# ── Statements / Declarations ──────────────────────────────────────

@dataclass
class Stmt(Node):
    pass

@dataclass
class LetStmt(Stmt):
    name: str = ""
    mutable: bool = False
    typ: TypeExpr | None = None
    value: Expr | None = None
    is_pub: bool = False
    module: str | None = None
    qual_name: str | None = None

@dataclass
class ExprStmt(Stmt):
    expr: Expr | None = None

@dataclass
class ReturnStmt(Stmt):
    value: Expr | None = None

@dataclass
class WhileStmt(Stmt):
    cond: Expr | None = None
    body: Block | None = None

@dataclass
class ForStmt(Stmt):
    var: str = ""
    iter: Expr | None = None  # RangeExpr or any iterable
    body: Block | None = None

@dataclass
class BreakStmt(Stmt):
    pass

@dataclass
class ContinueStmt(Stmt):
    pass

@dataclass
class FnParam(Node):
    name: str = ""
    typ: TypeExpr | None = None

@dataclass
class FnDecl(Stmt):
    name: str = ""
    params: list[FnParam] = field(default_factory=list)
    ret_type: TypeExpr | None = None
    body: Block | None = None
    is_pub: bool = False
    is_extern: bool = False
    link_lib: str | None = None  # from @link("...") or extern "lib"
    # module this decl belongs to (None = main / entry file)
    module: str | None = None
    # qualified name used in the checker (e.g. "gfx.open"); None = use name
    qual_name: str | None = None

@dataclass
class StructField(Node):
    name: str = ""
    typ: TypeExpr | None = None

@dataclass
class StructDecl(Stmt):
    name: str = ""
    fields: list[StructField] = field(default_factory=list)
    is_pub: bool = False
    module: str | None = None
    qual_name: str | None = None

@dataclass
class UseStmt(Stmt):
    """`use gfx` or `use gfx as g` — resolved by the module loader."""
    path: str = ""          # dotted path as written, e.g. "gfx"
    alias: str | None = None

@dataclass
class ModuleStmt(Stmt):
    """`module gfx` — declares this file's module name."""
    path: str = ""

@dataclass
class Program(Node):
    decls: list = field(default_factory=list)  # FnDecl | StructDecl | LetStmt | UseStmt | ...
    module_name: str | None = None  # set from `module` decl or file path
    filename: str | None = None


@dataclass
class CastExpr(Expr):
    expr: Expr | None = None
    target: TypeExpr | None = None

@dataclass
class GuardStmt(Stmt):
    cond: Expr | None = None
    else_body: Block | None = None

@dataclass
class LoopStmt(Stmt):
    body: Block | None = None

@dataclass
class AssertStmt(Stmt):
    cond: Expr | None = None
    msg: Expr | None = None

@dataclass
class TestDecl(Stmt):
    name: str = ""
    body: Block | None = None

"""Kiln semantic types."""

from __future__ import annotations


class Ty:
    def prune(self) -> "Ty":
        return self

    def is_concrete(self) -> bool:
        return True


class Prim(Ty):
    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return self.name


INT = Prim("Int")
FLOAT = Prim("Float")
BOOL = Prim("Bool")
STR = Prim("Str")
UNIT = Prim("Unit")
NEVER = Prim("Never")
PTR = Prim("Ptr")
CSTR = Prim("CStr")

# Sized integer types for FFI / packed buffers
I8 = Prim("I8")
U8 = Prim("U8")
I16 = Prim("I16")
U16 = Prim("U16")
I32 = Prim("I32")
U32 = Prim("U32")
I64 = Prim("I64")
U64 = Prim("U64")

SIZED_INTS = {I8, U8, I16, U16, I32, U32, I64, U64}
SIGNED_SIZED = {I8, I16, I32, I64}
UNSIGNED_SIZED = {U8, U16, U32, U64}

SIZED_C = {
    I8: "int8_t", U8: "uint8_t",
    I16: "int16_t", U16: "uint16_t",
    I32: "int32_t", U32: "uint32_t",
    I64: "int64_t", U64: "uint64_t",
}
SIZED_BITS = {
    I8: 8, U8: 8, I16: 16, U16: 16,
    I32: 32, U32: 32, I64: 64, U64: 64,
}
SIZED_SIGNED = {
    I8: True, U8: False, I16: True, U16: False,
    I32: True, U32: False, I64: True, U64: False,
}


class Arr(Ty):
    def __init__(self, elem: Ty):
        self.elem = elem

    def prune(self):
        return self

    def is_concrete(self):
        return prune(self.elem).is_concrete()

    def __repr__(self):
        return f"[{prune(self.elem)!r}]"


class StructT(Ty):
    def __init__(self, name: str):
        self.name = name
        self.fields: list[tuple[str, Ty]] = []

    def field(self, name: str):
        for n, t in self.fields:
            if n == name:
                return t
        return None

    def __repr__(self):
        return self.name


class ModuleT(Ty):
    """Type of a module binding introduced by `use name`."""
    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return f"module {self.name}"


class TVar(Ty):
    """Unresolved type (e.g. element type of an empty list literal)."""
    counter = 0

    def __init__(self):
        TVar.counter += 1
        self.id = TVar.counter
        self.ref: Ty | None = None

    def prune(self):
        if self.ref is None:
            return self
        r = self.ref.prune()
        self.ref = r
        return r

    def is_concrete(self):
        return self.ref is not None and self.ref.prune().is_concrete()

    def __repr__(self):
        if self.ref is not None:
            return repr(self.prune())
        return "?"


def prune(t: Ty) -> Ty:
    return t.prune() if t is not None else t


def same(a: Ty, b: Ty) -> bool:
    a, b = prune(a), prune(b)
    if a is b:
        return True
    if isinstance(a, Arr) and isinstance(b, Arr):
        return same(a.elem, b.elem)
    return False


def unify(a: Ty, b: Ty) -> Ty | None:
    """Return the unified type or None if incompatible. Never unifies with anything."""
    a, b = prune(a), prune(b)
    if a is NEVER:
        return b
    if b is NEVER:
        return a
    if a is b:
        return a
    if isinstance(a, TVar):
        if occurs(a, b):
            return None
        a.ref = b
        return b
    if isinstance(b, TVar):
        if occurs(b, a):
            return None
        b.ref = a
        return a
    if isinstance(a, Arr) and isinstance(b, Arr):
        e = unify(a.elem, b.elem)
        return a if e is not None else None
    return None


def occurs(v: TVar, t: Ty) -> bool:
    t = prune(t)
    if t is v:
        return True
    if isinstance(t, Arr):
        return occurs(v, t.elem)
    return False


def mangle(t: Ty) -> str:
    t = prune(t)
    if t is INT:
        return "int"
    if t is FLOAT:
        return "float"
    if t is BOOL:
        return "bool"
    if t is STR:
        return "str"
    if t is PTR:
        return "ptr"
    if t is CSTR:
        return "cstr"
    if t in SIZED_INTS:
        return t.name.lower()
    if isinstance(t, Arr):
        return "arr_" + mangle(t.elem)
    if isinstance(t, StructT):
        return "S" + t.name
    raise ValueError(f"cannot mangle type {t!r}")


def is_numeric(t: Ty) -> bool:
    t = prune(t)
    return t is INT or t is FLOAT or t in SIZED_INTS


def is_integer(t: Ty) -> bool:
    t = prune(t)
    return t is INT or t in SIZED_INTS


def int_lit_fits(value: int, t: Ty) -> bool:
    t = prune(t)
    if t is INT or t is I64:
        return -2**63 <= value < 2**63
    if t is U64:
        return 0 <= value < 2**64
    bits = SIZED_BITS.get(t)
    if bits is None:
        return False
    if SIZED_SIGNED[t]:
        return -(1 << (bits - 1)) <= value < (1 << (bits - 1))
    return 0 <= value < (1 << bits)

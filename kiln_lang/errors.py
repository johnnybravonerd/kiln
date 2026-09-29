"""Compile-time error reporting with source locations."""

from __future__ import annotations


class SourceLoc:
    __slots__ = ("file", "line", "col")

    def __init__(self, file: str, line: int, col: int):
        self.file = file
        self.line = line
        self.col = col

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}"


class CompileError(Exception):
    def __init__(self, message: str, loc: SourceLoc | None = None):
        self.message = message
        self.loc = loc
        if loc:
            super().__init__(f"{loc}: error: {message}")
        else:
            super().__init__(f"error: {message}")


def format_error(message: str, loc: SourceLoc | None = None, source: str | None = None) -> str:
    if loc is None:
        return f"error: {message}"
    lines = [f"{loc}: error: {message}"]
    if source is not None:
        src_lines = source.splitlines()
        if 1 <= loc.line <= len(src_lines):
            text = src_lines[loc.line - 1]
            lines.append(f"  {text}")
            caret = " " * (loc.col - 1) + "^"
            lines.append(f"  {caret}")
    return "\n".join(lines)

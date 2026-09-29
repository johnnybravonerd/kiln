"""Kiln lexer: source text -> tokens.

Newlines are significant (they end statements) except inside (...) and [...]
or after a binary operator / comma, where the parser skips them.
"""

from __future__ import annotations
from dataclasses import dataclass
from .errors import CompileError, SourceLoc

KEYWORDS = {
    "let", "var", "const", "fn", "return", "if", "else", "while", "for", "in",
    "break", "continue", "match", "struct", "true", "false", "loop", "and", "or",
    "not", "as", "module", "use", "pub", "extern", "null",
}

# longest first
OPERATORS = [
    "..=", "...", "<<=", ">>=",
    "->", "=>", "==", "!=", "<=", ">=", "&&", "||", "..", "+=", "-=", "*=", "/=", "%=",
    "|>", "<<", ">>",
    "+", "-", "*", "/", "%", "<", ">", "=", "!", "(", ")", "{", "}", "[", "]",
    ",", ":", ".", "&", "|", "^", "@", "?", ";", "#",
]


@dataclass
class Token:
    kind: str       # INT FLOAT STRING IDENT KW OP NEWLINE EOF
    value: object
    loc: SourceLoc

    def __repr__(self) -> str:
        return f"Token({self.kind},{self.value!r},{self.loc.line}:{self.loc.col})"


class Lexer:
    def __init__(self, source: str, filename: str = "<input>"):
        self.src = source
        self.file = filename
        self.pos = 0
        self.line = 1
        self.col = 1
        self.tokens: list[Token] = []

    def loc(self) -> SourceLoc:
        return SourceLoc(self.file, self.line, self.col)

    def error(self, msg: str, loc: SourceLoc | None = None):
        raise CompileError(msg, loc or self.loc())

    def peek(self, off: int = 0) -> str:
        p = self.pos + off
        return self.src[p] if p < len(self.src) else ""

    def advance(self, n: int = 1) -> str:
        s = self.src[self.pos:self.pos + n]
        for ch in s:
            if ch == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
        self.pos += n
        return s

    def add(self, kind: str, value, loc: SourceLoc):
        self.tokens.append(Token(kind, value, loc))

    def tokenize(self) -> list[Token]:
        # shebang
        if self.src.startswith("#!"):
            while self.peek() and self.peek() != "\n":
                self.advance()
        while self.pos < len(self.src):
            ch = self.peek()
            if ch in " \t\r":
                self.advance()
            elif ch == "\n":
                loc = self.loc()
                self.advance()
                if not self.tokens or self.tokens[-1].kind != "NEWLINE":
                    self.add("NEWLINE", "\n", loc)
            elif ch == "/" and self.peek(1) == "/":
                while self.peek() and self.peek() != "\n":
                    self.advance()
            elif ch == "/" and self.peek(1) == "*":
                self.block_comment()
            elif ch.isdigit():
                self.number()
            elif ch.isalpha() or ch == "_":
                self.ident()
            elif ch == '"':
                self.string()
            elif ch == "'":
                self.char_lit()
            else:
                loc = self.loc()
                for op in OPERATORS:
                    if self.src.startswith(op, self.pos):
                        self.advance(len(op))
                        self.add("OP", op, loc)
                        break
                else:
                    self.error(f"unexpected character {ch!r}")
        self.add("NEWLINE", "\n", self.loc())
        self.add("EOF", None, self.loc())
        return self.tokens

    def block_comment(self):
        start = self.loc()
        self.advance(2)
        depth = 1
        while depth > 0:
            if not self.peek():
                self.error("unterminated block comment", start)
            if self.peek() == "/" and self.peek(1) == "*":
                depth += 1
                self.advance(2)
            elif self.peek() == "*" and self.peek(1) == "/":
                depth -= 1
                self.advance(2)
            else:
                self.advance()

    def number(self):
        loc = self.loc()
        start = self.pos
        if self.peek() == "0" and self.peek(1) in "xXbBoO" and self.peek(1):
            base = {"x": 16, "b": 2, "o": 8}[self.peek(1).lower()]
            self.advance(2)
            digits = ""
            while self.peek() and (self.peek().isalnum() or self.peek() == "_"):
                digits += self.advance()
            try:
                self.add("INT", int(digits.replace("_", ""), base), loc)
            except ValueError:
                self.error(f"invalid numeric literal {self.src[start:self.pos]!r}", loc)
            return
        is_float = False
        while self.peek().isdigit() or self.peek() == "_":
            self.advance()
        # a '.' followed by a digit is a decimal point ('..' is a range)
        if self.peek() == "." and self.peek(1).isdigit():
            is_float = True
            self.advance()
            while self.peek().isdigit() or self.peek() == "_":
                self.advance()
        if self.peek() in ("e", "E") and (self.peek(1).isdigit() or (self.peek(1) in "+-" and self.peek(2).isdigit())):
            is_float = True
            self.advance()
            if self.peek() in "+-":
                self.advance()
            while self.peek().isdigit():
                self.advance()
        text = self.src[start:self.pos].replace("_", "")
        if is_float:
            self.add("FLOAT", float(text), loc)
        else:
            self.add("INT", int(text), loc)

    def ident(self):
        loc = self.loc()
        start = self.pos
        while self.peek() and (self.peek().isalnum() or self.peek() == "_"):
            self.advance()
        word = self.src[start:self.pos]
        self.add("KW" if word in KEYWORDS else "IDENT", word, loc)

    ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'", "{": "{", "}": "}"}

    def char_lit(self):
        # 'c' is treated as a one-character Str
        loc = self.loc()
        self.advance()
        if self.peek() == "\\":
            self.advance()
            e = self.advance()
            if e not in self.ESCAPES:
                self.error(f"unknown escape \\{e}")
            val = self.ESCAPES[e]
        else:
            val = self.advance()
        if self.peek() != "'":
            self.error("unterminated character literal", loc)
        self.advance()
        self.add("STRING", [val], loc)

    def skip_nested_string(self) -> str:
        """Consume a (possibly interpolated) string literal nested inside an
        interpolation and return its raw source text."""
        start = self.pos
        sloc = self.loc()
        self.advance()
        while True:
            c = self.peek()
            if not c or c == "\n":
                self.error("unterminated string literal inside interpolation", sloc)
            if c == "\\":
                self.advance(2)
                continue
            if c == '"':
                self.advance()
                break
            if c == "{":
                if self.peek(1) == "{":
                    self.advance(2)
                    continue
                self.advance()
                depth = 1
                while depth:
                    d = self.peek()
                    if not d or d == "\n":
                        self.error("unterminated interpolation", sloc)
                    if d == '"':
                        self.skip_nested_string()
                        continue
                    if d == "{":
                        depth += 1
                    elif d == "}":
                        depth -= 1
                    self.advance()
                continue
            self.advance()
        return self.src[start:self.pos]

    def string(self):
        """Produce a STRING token whose value is a list of parts:
        str (literal text) or tuple ('expr', source_text, SourceLoc, fmt_spec)."""
        loc = self.loc()
        triple = self.src.startswith('"""', self.pos)
        self.advance(3 if triple else 1)
        parts: list = []
        buf = ""
        while True:
            ch = self.peek()
            if not ch:
                self.error("unterminated string literal", loc)
            if triple and self.src.startswith('"""', self.pos):
                self.advance(3)
                break
            if not triple and ch == '"':
                self.advance()
                break
            if not triple and ch == "\n":
                self.error("unterminated string literal (use \"\"\" for multi-line strings)", loc)
            if ch == "\\":
                self.advance()
                e = self.advance()
                if e not in self.ESCAPES:
                    self.error(f"unknown escape sequence \\{e}")
                buf += self.ESCAPES[e]
            elif ch == "{":
                if self.peek(1) == "{":      # '{{' -> literal '{'
                    self.advance(2)
                    buf += "{"
                    continue
                if buf:
                    parts.append(buf)
                    buf = ""
                self.advance()
                eloc = self.loc()
                depth = 1
                text = ""
                while True:
                    c = self.peek()
                    if not c or c == "\n" and not triple:
                        self.error("unterminated interpolation in string", eloc)
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            self.advance()
                            break
                    elif c == '"':
                        text += self.skip_nested_string()
                        continue
                    text += self.advance()
                # format spec: "{x:>8}" – split on a top-level ':' not part of '::'
                spec = None
                depth2 = 0
                for i, c in enumerate(text):
                    if c in "([":
                        depth2 += 1
                    elif c in ")]":
                        depth2 -= 1
                    elif c == ":" and depth2 == 0:
                        spec = text[i + 1:]
                        text = text[:i]
                        break
                if not text.strip():
                    self.error("empty interpolation {} in string", eloc)
                parts.append(("expr", text, eloc, spec))
            elif ch == "}":
                if self.peek(1) == "}":
                    self.advance(2)
                    buf += "}"
                else:
                    self.advance()
                    buf += "}"
            else:
                buf += self.advance()
        if buf or not parts:
            parts.append(buf)
        self.add("STRING", parts, loc)


def tokenize(source: str, filename: str = "<input>") -> list[Token]:
    return Lexer(source, filename).tokenize()

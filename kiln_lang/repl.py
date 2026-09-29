"""Kiln REPL (`kiln>` prompt), backed by the closure-compiling interpreter."""

from __future__ import annotations
import os
import subprocess
import sys
import time

from . import __version__
from . import ast_nodes as A
from .errors import CompileError, format_error
from .lexer import tokenize
from .parser import parse_repl_entry, parse
from .checker import Checker, Symbol
from .ktypes import UNIT, NEVER, prune
from .interp import (Interp, KilnPanic, KilnExit, to_str_fn, run_in_big_stack, load_program)

HELP = """Kiln REPL commands
  :help, :h            show this help
  :quit, :q, Ctrl-D    leave the REPL
  :type <expr>, :t     show the inferred type of an expression or function
  :info <name>, :i     show a definition's source
  :load <file>, :l     load declarations (fn/struct/let) from a .kn file
  :reload, :r          reload the last loaded file
  :time <code>         run code and report the elapsed time
  :reset               clear all definitions and bindings
  :history             show input history
  :save <file>         write this session's inputs to a file
  :env                 show toolchain information
  :why                 show the last error again in full
  :set numbered        number prompts (kiln[3]>); `_` is always the last result
  :elevate / :drop     switch the prompt to kiln# / back (no sandbox in 0.1)
  !<cmd>               run a shell command
Enter Kiln code directly: `let x = 5`, `fn sq(x: Int) -> Int { x * x }`, `sq(x) + 1`.
Unfinished input (open braces, trailing operator) continues on a `...>` line."""

CONTINUE_OPS = {"+", "-", "*", "/", "%", "=", "==", "!=", "<", ">", "<=", ">=", "&&", "||", "and", "or",
                ",", "->", "=>", "|>", "..", "..=", ".", "(", "[", "{", "+=", "-=", "*=", "/=", "&", "|", "^"}


def needs_more(src: str) -> bool:
    try:
        toks = tokenize(src, "<repl>")
    except CompileError as ce:
        m = ce.message
        return "unterminated" in m
    depth = 0
    last = None
    for t in toks:
        if t.kind == "OP" and t.value in ("(", "[", "{"):
            depth += 1
        elif t.kind == "OP" and t.value in (")", "]", "}"):
            depth -= 1
        if t.kind not in ("NEWLINE", "EOF"):
            last = t
    if depth > 0:
        return True
    if last is not None and last.kind in ("OP", "KW") and last.value in CONTINUE_OPS and last.value not in (")", "]", "}"):
        return True
    return False


class Session:
    def __init__(self):
        self.reset()
        self.history: list[str] = []
        self.numbered = False
        self.elevated = False
        self.failed = False
        self.last_error = ""
        self.last_loaded = None
        self.counter = 0

    def reset(self):
        self.ck = Checker("<repl>")
        self.ck.allow_redefine = True
        self.ck.scopes = []
        self.interp = Interp()
        self.sources: dict[str, str] = {}

    def prompt(self, cont=False) -> str:
        term = "#" if self.elevated else ">"
        if cont:
            return f"...{term} "
        status = "!" if self.failed else ""
        num = f"[{self.counter + 1}]" if self.numbered else ""
        return f"kiln{num}{status}{term} "

    # ── evaluation ─────────────────────────────────────────────────
    def run_entry(self, src: str, filename="<repl>", echo=True):
        items = parse_repl_entry(src, filename)
        ck = self.ck
        ck.file = filename
        outputs = []
        for it in items:
            if isinstance(it, A.StructDecl):
                ck.declare_struct_name(it)
                ck.resolve_struct_fields(it)
                ck.check_struct_cycles()
                self.sources[it.name] = self.snippet(src, it)
            elif isinstance(it, A.FnDecl):
                ck.declare_fn(it)
                try:
                    ck.check_fn_body(it.info)
                except CompileError:
                    ck.scopes = []
                    ck.cur_fn = None
                    raise
                self.interp.define_fn(it)
                self.sources[it.name] = self.snippet(src, it)
            elif isinstance(it, A.TestDecl):
                raise CompileError("`test` blocks are not run in the REPL; use `kiln test file.kn`", it.loc)
            else:
                outputs.append(self.run_stmt(it, echo))
        return outputs

    def snippet(self, src, node):
        lines = src.splitlines()
        return "\n".join(lines[node.loc.line - 1:]) if node.loc else src

    def run_stmt(self, st, echo):
        ck = self.ck
        ck.scopes = []
        ck.cur_fn = None
        ck.name_counts = {}
        if isinstance(st, A.LetStmt):
            ck.check_global_let(st)
            self.sources[st.name] = f"{'var' if st.mutable else 'let'} {st.name}: {prune(st.sym.ty)!r}"
            runner, n = self.interp.compile_toplevel_stmts([st])
            self.execute(lambda: runner([None] * n))
            return None
        # other statements run in a temporary local scope (globals remain visible)
        ck.scopes = [{}]
        try:
            if isinstance(st, A.ExprStmt):
                t = ck.check_expr(st.expr, used=True) if not isinstance(st.expr, A.Assign) else ck.check_expr(st.expr, used=False)
            else:
                t = ck.check_stmt(st)
                if isinstance(st, A.ReturnStmt):
                    raise CompileError("`return` outside of a function", st.loc)
            ck.finalize()
        finally:
            ck.scopes = []
        self.interp.lslots = {}
        if isinstance(st, A.ExprStmt) and prune(t) not in (UNIT, NEVER) and not isinstance(st.expr, A.Assign):
            f = self.interp.expr(st.expr)
            n = len(self.interp.lslots)
            val = self.execute(lambda: f([None] * n))
            # bind `_` to the last result
            sym = Symbol("_", t, False, ck.fresh_cname("_", True), "global")
            ck.globals["_"] = sym
            slot = self.interp.global_slot(sym)
            self.interp.G[slot] = val
            if echo:
                return "=> " + to_str_fn(t, repr_mode=True)(val)
            return None
        runner = self.interp.stmt(st)
        n = len(self.interp.lslots)
        self.execute(lambda: runner([None] * n))
        return None

    def execute(self, fn):
        return run_in_big_stack(fn)

    def type_of(self, text: str) -> str:
        name = text.strip()
        if name in self.ck.fns:
            return f"{name} : {self.ck.fns[name].sig()}"
        if name in self.ck.structs:
            st = self.ck.structs[name]
            return f"{name} : struct {{ " + ", ".join(f"{n}: {prune(t)!r}" for n, t in st.fields) + " }"
        from .parser import Parser
        toks = tokenize(text, "<repl>")
        p = Parser(toks, text, "<repl>")
        e = p.parse_expr()
        self.ck.scopes = [{}]
        try:
            t = self.ck.check_expr(e, used=True)
            self.ck.finalize()
        finally:
            self.ck.scopes = []
        return f"{text.strip()} : {prune(t)!r}"

    def load_file(self, path: str):
        with open(path, encoding="utf-8-sig") as f:
            src = f.read()
        prog = parse(src, path)
        ck = self.ck
        ck.file = path
        structs = [d for d in prog.decls if isinstance(d, A.StructDecl)]
        fns = [d for d in prog.decls if isinstance(d, A.FnDecl)]
        for d in structs:
            ck.declare_struct_name(d)
        for d in structs:
            ck.resolve_struct_fields(d)
        ck.check_struct_cycles()
        for d in fns:
            ck.declare_fn(d)
        lets = [d for d in prog.decls if isinstance(d, A.LetStmt)]
        for d in lets:
            ck.check_global_let(d)
        for d in fns:
            ck.check_fn_body(d.info)
        for d in fns:
            self.interp.define_fn(d)
            self.sources[d.name] = self.snippet(src, d)
        for d in structs:
            self.sources[d.name] = self.snippet(src, d)
        for d in lets:
            runner, n = self.interp.compile_toplevel_stmts([d])
            self.execute(lambda: runner([None] * n))
        self.last_loaded = path
        names = [d.name for d in structs + fns + lets]
        return f"loaded {path}: {', '.join(names) if names else '(nothing)'}" + \
            ("  (call main() to run it)" if "main" in ck.fns else "")


def show_error(sess: Session, src: str, ce: CompileError):
    msg = format_error(ce.message, ce.loc, src if ce.loc and ce.loc.file == "<repl>" else None)
    sess.last_error = msg
    print(msg, file=sys.stderr)


def handle_meta(sess: Session, line: str) -> bool:
    """Returns False to quit."""
    parts = line[1:].strip().split(None, 1)
    cmd = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    if cmd in ("q", "quit", "exit"):
        return False
    if cmd in ("h", "help", "?"):
        print(HELP)
    elif cmd in ("t", "type"):
        if not arg:
            print("usage: :type <expr>")
        else:
            try:
                print(sess.type_of(arg))
                sess.failed = False
            except CompileError as ce:
                show_error(sess, arg, ce)
                sess.failed = True
    elif cmd in ("i", "info", "doc"):
        if arg in sess.sources:
            print(sess.sources[arg])
        elif arg in sess.ck.globals:
            s = sess.ck.globals[arg]
            print(f"{'var' if s.mutable else 'let'} {arg}: {prune(s.ty)!r}")
        else:
            print(f"no definition named `{arg}`")
    elif cmd in ("l", "load"):
        try:
            print(sess.load_file(arg))
            sess.failed = False
        except FileNotFoundError:
            print(f"file not found: {arg}", file=sys.stderr)
            sess.failed = True
        except CompileError as ce:
            print(format_error(ce.message, ce.loc, None), file=sys.stderr)
            sess.last_error = ce.message
            sess.failed = True
    elif cmd in ("r", "reload"):
        if not sess.last_loaded:
            print("nothing loaded yet (use :load file.kn)")
        else:
            return handle_meta(sess, ":load " + sess.last_loaded)
    elif cmd == "time":
        t0 = time.perf_counter()
        ok = eval_input(sess, arg)
        dt = time.perf_counter() - t0
        print(f"time: {dt * 1000:.3f} ms" + ("" if ok else " (failed)"))
    elif cmd == "reset":
        sess.reset()
        print("session cleared")
    elif cmd == "history":
        for i, h in enumerate(sess.history, 1):
            print(f"{i:4}  {h}")
    elif cmd == "save":
        if not arg:
            print("usage: :save <file>")
        else:
            with open(arg, "w", encoding="utf-8") as f:
                f.write("\n".join(h for h in sess.history if not h.startswith((":", "!"))) + "\n")
            print(f"saved {len(sess.history)} entries to {arg}")
    elif cmd == "env":
        from .driver import find_c_compiler
        try:
            cc = find_c_compiler()
        except CompileError as ce:
            cc = None
        print(f"Kiln {__version__} (REPL uses the built-in interpreter)")
        print(f"Python {sys.version.split()[0]} on {sys.platform}")
        print(f"C compiler for build/run: {' '.join(cc) if cc else 'none found'}")
        print("capabilities: all (no sandbox in 0.1)")
    elif cmd == "why":
        print(sess.last_error or "no error so far")
    elif cmd == "set":
        if arg.strip() == "numbered":
            sess.numbered = True
        elif arg.strip() in ("nonumbered", "numbered off"):
            sess.numbered = False
        else:
            print("options: :set numbered | :set nonumbered")
    elif cmd == "elevate":
        sess.elevated = True
        print("note: Kiln 0.1 has no capability sandbox; the prompt changes only")
    elif cmd == "drop":
        sess.elevated = False
    elif cmd in ("grant", "tasks", "enter", "leave", "edit", "paste", "break", "step", "next", "continue", "c",
                 "where", "locals", "finish"):
        print(f":{cmd} is not yet supported in Kiln {__version__}")
    else:
        print(f"unknown command :{cmd} (try :help)")
    return True


def eval_input(sess: Session, src: str) -> bool:
    try:
        outs = sess.run_entry(src)
        for o in outs:
            if o:
                print(o)
        sys.stdout.flush()
        sess.failed = False
        return True
    except CompileError as ce:
        show_error(sess, src, ce)
    except KilnPanic as p:
        sys.stdout.flush()
        msg = p.msg if p.msg.startswith("assertion failed") else f"panic: {p.msg}"
        sess.last_error = msg
        print(msg, file=sys.stderr)
    except KilnExit as ex:
        sys.stdout.flush()
        print(f"(program called exit({ex.code}))")
        sess.failed = ex.code != 0
        return ex.code == 0
    except RecursionError:
        sess.last_error = "panic: stack overflow (recursion too deep)"
        print(sess.last_error, file=sys.stderr)
    sess.failed = True
    return False


def run_repl(opts=None) -> int:
    try:
        import readline  # noqa: F401  (line editing + history where available)
    except ImportError:
        pass
    sess = Session()
    interactive = sys.stdin.isatty()
    print(f"Kiln {__version__} - :help for help, :q to quit")
    while True:
        try:
            line = input(sess.prompt()) if interactive else _read_line(sess.prompt())
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("\n(interrupted — type :q to quit)")
            continue
        if line is None:
            print()
            return 0
        if not line.strip():
            continue
        s = line.strip()
        if s.startswith(":"):
            sess.history.append(s)
            if not handle_meta(sess, s):
                return 0
            continue
        if s.startswith("!"):
            sess.history.append(s)
            subprocess.run(s[1:], shell=True)
            continue
        buf = line
        while needs_more(buf):
            try:
                more = input(sess.prompt(cont=True)) if interactive else _read_line(sess.prompt(cont=True))
            except EOFError:
                more = None
            except KeyboardInterrupt:
                print("\n(cancelled)")
                buf = None
                break
            if more is None:
                break
            buf += "\n" + more
        if buf is None:
            continue
        sess.history.append(buf)
        sess.counter += 1
        eval_input(sess, buf)


def _read_line(prompt):
    """Non-interactive input (piped): echo the prompt so transcripts look like a session."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.rstrip("\n")
    sys.stdout.write(line + "\n")
    return line


def eval_one_liner(src: str) -> int:
    sess = Session()
    ok = eval_input(sess, src)
    return 0 if ok else 1

"""Kiln command-line driver: compiler discovery, build, run, emit-c, check, test."""

from __future__ import annotations
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

from . import __version__
from .errors import CompileError, format_error
from .parser import parse
from .checker import Checker
from .codegen import generate_c
from .modules import load_program, STDLIB

IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_c_compiler(explicit: str | None = None) -> list[str] | None:
    """Return the C compiler command as an argv prefix, e.g. ['gcc'] or ['zig', 'cc']."""
    spec = explicit or os.environ.get("KILN_CC")
    if spec:
        parts = shlex.split(spec, posix=not IS_WIN)
        if parts and (shutil.which(parts[0]) or os.path.isfile(parts[0])):
            return parts
        raise CompileError(f"C compiler `{spec}` (from KILN_CC / --cc) was not found")
    # a toolchain bundled next to kiln.py (for the portable desktop package)
    bundled = [
        (os.path.join(HERE, "toolchain", "zig", "zig" + EXE), ["cc"]),
        (os.path.join(HERE, "toolchain", "tcc", "tcc" + EXE), []),
    ]
    for path, extra in bundled:
        if os.path.isfile(path):
            return [path] + extra
    for name in (["cc", "gcc", "clang"] if not IS_WIN else ["gcc", "clang", "cc"]):
        p = shutil.which(name)
        if p:
            return [p]
    z = shutil.which("zig")
    if z:
        return [z, "cc"]
    t = shutil.which("tcc")
    if t:
        return [t]
    return None


def compiler_flags(cc: list[str], debug: bool, release: bool = False,
                   target: str | None = None) -> list[str]:
    """Flags for the C compiler.
    default : -O2 -fwrapv            (bounds checks on)
    release : -O3 -march=native -flto -DNDEBUG -DKILN_RELEASE -fwrapv (bounds checks off)
              (-march=native is dropped when cross-compiling with --target)
    debug   : -O0 -g -fwrapv"""
    base = os.path.basename(cc[0]).lower()
    is_tcc = base.startswith("tcc")
    flags = []
    if debug:
        flags += ["-O0", "-g"]
    elif release:
        flags += ["-O3" if not is_tcc else "-O2", "-DNDEBUG", "-DKILN_RELEASE"]
        if not is_tcc:
            if not target:
                flags.append("-march=native")
            flags.append("-flto")
    else:
        flags += ["-O2"]
    if not is_tcc:
        flags += ["-fwrapv", "-w"]
    return flags


def read_source(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except FileNotFoundError:
        raise CompileError(f"file not found: {path}")
    except UnicodeDecodeError:
        raise CompileError(f"{path} is not valid UTF-8 text")


def frontend(source: str, filename: str, require_main: bool = True, resolve_modules: bool = True):
    """Type-check a program. When resolve_modules is True and filename is a real path,
    `use` imports are loaded and merged."""
    if resolve_modules and filename and not filename.startswith("<") and os.path.isfile(filename):
        prog, entry_source, _mods = load_program(filename)
        # Prefer caller-provided source only when it matches; load_program re-reads the file.
        source = entry_source
    else:
        prog = parse(source, filename)
    ck = Checker(filename)
    ck.check_program(prog, require_main=require_main)
    return prog, ck, source


def compile_result(source: str, filename: str, test_mode: bool = False):
    """Return (c_code, checker) after full frontend + codegen."""
    prog, ck, _src = frontend(source, filename, require_main=not test_mode)
    if test_mode and not ck.tests:
        raise CompileError(f"no `test \"...\" {{ }}` blocks found in {filename}")
    c = generate_c(prog, ck, filename, test_mode=test_mode)
    return c, ck


def compile_to_c(source: str, filename: str, test_mode: bool = False) -> str:
    c, _ck = compile_result(source, filename, test_mode=test_mode)
    return c


def link_flags_for(libs: set[str] | None, target: str | None = None) -> tuple[list[str], list[str]]:
    """Return (extra_c_sources, linker_flags) for @link libraries."""
    libs = set(libs or [])
    extra_c: list[str] = []
    flags: list[str] = []
    to_win = bool(target and "windows" in target) or (IS_WIN and not target)
    # Special built-in graphics backend
    if "kiln_gfx" in libs:
        gfx_c = os.path.join(HERE, "runtime", "kiln_gfx.c")
        if not os.path.isfile(gfx_c):
            raise CompileError(f"kiln graphics backend missing: {gfx_c}")
        extra_c.append(gfx_c)
        libs.discard("kiln_gfx")
        if to_win:
            for L in ("user32", "gdi32"):
                if f"-l{L}" not in flags:
                    flags.append(f"-l{L}")
        else:
            flags.append("-lX11")
    for lib in sorted(libs):
        flag = f"-l{lib}"
        if flag not in flags:
            flags.append(flag)
    return extra_c, flags


def compile_c(c_code: str, out_path: str, cc_spec: str | None = None, debug: bool = False,
              keep_c: str | None = None, target: str | None = None, release: bool = False,
              link_libs: set[str] | None = None) -> None:
    cc = find_c_compiler(cc_spec)
    if cc is None:
        raise CompileError(
            "no C compiler found. Install one of: gcc, clang, zig (https://ziglang.org/download/), or tcc; "
            "or set KILN_CC (e.g. KILN_CC=\"zig cc\"). Tip: `kiln run --interp file.kn` runs without a C compiler.")
    tmpdir = tempfile.mkdtemp(prefix="kiln-c-")
    try:
        c_path = keep_c or os.path.join(tmpdir, "main.c")
        with open(c_path, "w", encoding="utf-8") as f:
            f.write(c_code)
        is_zig = os.path.basename(cc[0]).lower().startswith("zig")
        tflags = []
        if target:
            if not is_zig:
                raise CompileError("--target (cross-compiling) needs Zig as the C compiler: "
                                   "set KILN_CC=\"zig cc\" or put zig in toolchain/zig/")
            tflags = ["-target", target]
        extra_c, lflags = link_flags_for(link_libs, target)
        libm = [] if (is_zig and (IS_WIN or (target and "windows" in target))) else ["-lm"]
        flags = compiler_flags(cc, debug, release, target)
        if is_zig and (target and "windows" in target or (IS_WIN and not target)):
            flags = [f for f in flags if f != "-flto"]   # zig cc does not support LTO for mingw targets
        env = dict(os.environ)
        if os.path.basename(cc[0]).lower().startswith("zig"):
            env.setdefault("ZIG_GLOBAL_CACHE_DIR", os.path.join(tempfile.gettempdir(), "kiln-zig-cache"))
            env.setdefault("ZIG_LOCAL_CACHE_DIR", os.path.join(tempfile.gettempdir(), "kiln-zig-cache"))
        # In release mode, fall back gracefully if the compiler rejects -flto, then -march=native.
        attempts = [flags]
        if "-flto" in flags:
            attempts.append([f for f in flags if f != "-flto"])
        if "-march=native" in flags:
            attempts.append([f for f in flags if f not in ("-flto", "-march=native")])
        sources = [c_path] + extra_c
        for fl in attempts:
            cmd = cc + tflags + fl + ["-o", out_path] + sources + libm + lflags
            r = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if os.environ.get("KILN_VERBOSE"):
                print(f"[kiln] {' '.join(cmd)}  -> exit {r.returncode}", file=sys.stderr)
            if r.returncode == 0:
                break
        if r.returncode != 0:
            raise CompileError("C compiler failed (this is a Kiln compiler bug; please report it):\n"
                               f"  command: {' '.join(cmd)}\n{r.stderr.strip()}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


USAGE = f"""Kiln {__version__} - a compiled, statically typed language

Usage:
  kiln                          start the REPL (same as `kiln repl`)
  kiln <file.kn> [args...]      compile and run a program
  kiln run <file.kn> [-- args]  compile to a temp dir and run
  kiln build <file.kn> [-o out] compile to a native executable
  kiln emit-c <file.kn>         print the generated C code
  kiln check <file.kn>          type-check only
  kiln test <file.kn>           run `test "name" {{ ... }}` blocks
  kiln repl                     interactive REPL (kiln> prompt)
  kiln -e "expr"                evaluate a one-liner and exit
  kiln new <name>               create a new project folder with main.kn
  kiln version                  print the version and detected C compiler

Options:
  -o <path>       output executable path (build)
  --cc <cmd>      C compiler to use, e.g. --cc "zig cc" (or set KILN_CC)
  --release       (build/run/test) maximum speed: -O3 -march=native -flto
                  -DNDEBUG, and no list/string bounds checks
  --debug         compile with -O0 -g instead of -O2
  --interp        (run / -e) use the built-in interpreter instead of a C compiler
  --keep-c <path> (build) also save the generated C file
  --target <t>    (build) cross-compile with zig, e.g. x86_64-windows-gnu,
                  aarch64-macos, x86_64-macos, aarch64-linux
"""


def default_out(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return base + EXE


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    opts = {"cc": None, "debug": False, "out": None, "interp": False, "keep_c": None, "target": None,
            "release": False}
    pos: list[str] = []
    passthru: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            passthru = argv[i + 1:]
            break
        if a in ("-o", "--out") and i + 1 < len(argv):
            opts["out"] = argv[i + 1]
            i += 2
            continue
        if a == "--cc" and i + 1 < len(argv):
            opts["cc"] = argv[i + 1]
            i += 2
            continue
        if a == "--target" and i + 1 < len(argv):
            opts["target"] = argv[i + 1]
            i += 2
            continue
        if a == "--keep-c" and i + 1 < len(argv):
            opts["keep_c"] = argv[i + 1]
            i += 2
            continue
        if a == "--debug":
            opts["debug"] = True
        elif a == "--release":
            opts["release"] = True
            opts["debug"] = False
        elif a == "--interp":
            opts["interp"] = True
        elif a in ("-h", "--help", "help"):
            print(USAGE)
            return 0
        elif a in ("-e",) and i + 1 < len(argv):
            pos = ["-e", argv[i + 1]]
            i += 2
            continue
        elif a.startswith("--allow"):
            pass  # capability flags accepted for compatibility (no sandbox yet)
        else:
            pos.append(a)
            # after `kiln file.kn`, remaining args go to the program
            if len(pos) == 1 and a.endswith(".kn"):
                passthru = argv[i + 1:]
                break
            if len(pos) == 2 and pos[0] == "run" and a.endswith(".kn"):
                rest = argv[i + 1:]
                if "--" in rest:
                    k = rest.index("--")
                    pre, passthru = rest[:k], rest[k + 1:]
                else:
                    pre, passthru = [], rest
                    # still honor flags placed after the file
                    pre = [x for x in rest if x.startswith("--")]
                    passthru = [x for x in rest if not x.startswith("--")] if pre else rest
                for x in pre:
                    if x == "--interp":
                        opts["interp"] = True
                    elif x == "--debug":
                        opts["debug"] = True
                    elif x == "--release":
                        opts["release"] = True
                break
        i += 1

    cmd = pos[0] if pos else "repl"
    source_text = None
    try:
        if cmd == "version" or cmd == "--version":
            cc = None
            try:
                cc = find_c_compiler(opts["cc"])
            except CompileError as ce:
                print(f"Kiln {__version__}\nC compiler: error: {ce.message}")
                return 1
            print(f"Kiln {__version__}")
            print(f"Python {sys.version.split()[0]} on {sys.platform}")
            print(f"C compiler: {' '.join(cc) if cc else 'none found (interpreter fallback only)'}")
            return 0
        if cmd == "repl":
            from .repl import run_repl
            return run_repl(opts)
        if cmd == "-e":
            from .repl import eval_one_liner
            return eval_one_liner(pos[1])
        if cmd == "new":
            if len(pos) < 2:
                print("usage: kiln new <name>", file=sys.stderr)
                return 2
            return cmd_new(pos[1])
        if cmd.endswith(".kn"):
            pos = ["run"] + pos
            cmd = "run"
        if cmd not in ("build", "run", "emit-c", "check", "test"):
            print(f"kiln: unknown command `{cmd}`\n", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        if len(pos) < 2:
            print(f"usage: kiln {cmd} <file.kn>", file=sys.stderr)
            return 2
        path = pos[1]
        source_text = read_source(path)
        fname = path
        if cmd == "check":
            frontend(source_text, fname)
            print(f"{path}: ok")
            return 0
        if cmd == "emit-c":
            sys.stdout.write(compile_to_c(source_text, fname))
            return 0
        if cmd == "build":
            c, ck = compile_result(source_text, fname)
            out = opts["out"] or default_out(path)
            to_windows = (opts["target"] and "windows" in opts["target"]) or (IS_WIN and not opts["target"])
            if to_windows and not out.lower().endswith(".exe"):
                out += ".exe"
            compile_c(c, out, opts["cc"], opts["debug"], opts["keep_c"], opts["target"], opts["release"],
                      link_libs=ck.link_libs)
            print(f"built {out}")
            return 0
        if cmd == "run" and opts["interp"]:
            from .interp import run_program
            prog, ck, _ = frontend(source_text, fname)
            return run_program(prog, ck, passthru)
        if cmd in ("run", "test"):
            c, ck = compile_result(source_text, fname, test_mode=(cmd == "test"))
            tmpdir = tempfile.mkdtemp(prefix="kiln-run-")
            try:
                exe = os.path.join(tmpdir, "prog" + EXE)
                compile_c(c, exe, opts["cc"], opts["debug"], release=opts["release"],
                          link_libs=ck.link_libs)
                sys.stdout.flush()
                r = subprocess.run([exe] + passthru)
                return r.returncode
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
    except CompileError as ce:
        print(format_error(ce.message, ce.loc, source_text if ce.loc and source_text is not None
                           and ce.loc.file == (pos[1] if len(pos) > 1 else None) else None), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


def cmd_new(name: str) -> int:
    if os.path.exists(name):
        print(f"kiln: `{name}` already exists", file=sys.stderr)
        return 1
    os.makedirs(name)
    with open(os.path.join(name, "main.kn"), "w", encoding="utf-8") as f:
        f.write('// ' + name + ' - a new Kiln program\n\nfn main() {\n    println("Hello from ' + name + '!")\n}\n')
    with open(os.path.join(name, "kiln.toml"), "w", encoding="utf-8") as f:
        f.write(f'[package]\nname = "{name}"\nversion = "0.1.0"\nentry = "main.kn"\n')
    print(f"created {name}/main.kn and {name}/kiln.toml\nrun it with: kiln run {name}/main.kn")
    return 0

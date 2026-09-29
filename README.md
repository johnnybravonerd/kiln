# Kiln 0.1

Kiln is a statically typed, expression-oriented language with type inference.
The compiler turns `.kn` source into C and then into a **native executable**
(gcc/clang/zig/tcc, built with `-O2`), so Kiln programs run at C speed.
It also has an interactive REPL (`kiln>`) powered by a built-in interpreter
that needs no C compiler.

```kiln
struct Point { x: Float, y: Float }

fn fib(n: Int) -> Int {
    if n < 2 { n } else { fib(n - 1) + fib(n - 2) }   // last expression is returned
}

fn main() {
    let p = Point { x: 3, y: 4 }
    var xs: [Int] = []
    for i in 0..10 { xs.push(fib(i)) }
    println("xs = {xs}, |p| = {sqrt(p.x * p.x + p.y * p.y):.1f}")
}
```

## Install

Kiln needs **Python 3.8+** (standard library only) and, for `build`/`run`, a **C compiler**.

| OS | Python | C compiler (pick one) |
|---|---|---|
| **Windows** | Use the ready-made `Kiln-windows` folder: it bundles Python and Zig, nothing to install. Otherwise install Python from python.org. | Zig: download the zip from https://ziglang.org/download/, unzip it to `toolchain\zig\` inside the Kiln folder (so `toolchain\zig\zig.exe` exists) or put it on PATH. MSYS2/MinGW `gcc` and `clang` also work. MSVC `cl` is **not** supported. |
| **macOS** | Preinstalled `python3` (or `brew install python`) | `xcode-select --install` gives `clang`/`cc`. Or `brew install zig`. |
| **Linux** | Usually preinstalled (`sudo apt install python3`) | `sudo apt install gcc` (or clang, tcc, zig) |

Check your setup with `kiln version`. It prints the C compiler it found.

**How the compiler is chosen:** `--cc "<cmd>"` flag, then the `KILN_CC` environment
variable (e.g. `KILN_CC="zig cc"`, `KILN_CC=tcc`), then a bundled
`toolchain/zig/zig[.exe]` or `toolchain/tcc/tcc[.exe]`, then `cc`, `gcc`, `clang`, `zig cc` and `tcc` on PATH.

Run the tool with `python3 kiln.py ...`, the `./kiln` bash wrapper (macOS/Linux),
or `kiln.cmd` / `Kiln.bat` on Windows.

## Commands

| Command | What it does |
|---|---|
| `kiln` / `kiln repl` | Interactive REPL with a `kiln>` prompt and a `...>` continuation prompt |
| `kiln file.kn [args]` | Compile and run (shortcut for `run`) |
| `kiln run file.kn [-- args]` | Compile into a temp dir, run, clean up. `--interp` uses the interpreter instead (no C compiler) |
| `kiln build file.kn [-o out]` | Produce a native executable (`--release` for maximum speed, `--debug` for `-O0 -g`, `--keep-c file.c` to keep the C) |
| `kiln build file.kn --target x86_64-windows-gnu` | Cross-compile (needs Zig). Targets: `x86_64-windows-gnu`, `aarch64-macos`, `x86_64-macos`, `aarch64-linux`, ... |
| `kiln emit-c file.kn` | Print the generated, self-contained C |
| `kiln check file.kn` | Type-check only |
| `kiln test file.kn` | Run the `test "name" { ... }` blocks |
| `kiln -e "expr"` | Evaluate a one-liner |
| `kiln new name` | Create `name/main.kn` + `kiln.toml` |
| `kiln version` | Version, Python, and detected C compiler |

**Build modes:**

- **Default:** `-O2 -fwrapv`, with list and string bounds checks turned on.
- **`--release`** (on `build` and `run`): `-O3 -march=native -flto -DNDEBUG -DKILN_RELEASE -fwrapv`.
  - Bounds checks are compiled out, so `xs[i]` and `s[i]` access memory directly. Negative indexes still count from the end.
  - Division by zero, `pop` on an empty list, and `assert` still panic.
  - With `--target`, `-march=native` is dropped so the exe runs on any CPU of that architecture.
  - `-flto` is dropped if the compiler rejects it. Zig can't do LTO for Windows targets.
  - tcc gets `-O2` without the extra flags.
- **`--debug`:** `-O0 -g`.

Only `KILN_RELEASE` turns off the checks, not `NDEBUG`. The reason is that `zig cc -O2` defines `NDEBUG` on its own,
and default builds must stay checked. Set `KILN_VERBOSE=1` to print the exact C compiler command.

**REPL meta-commands:** `:help`, `:q`, `:type expr` (`:t`), `:info name`, `:load file.kn`,
`:reload`, `:time code`, `:reset`, `:history`, `:save file`, `:env`, `:why`,
`:set numbered` (`kiln[3]>`), `:elevate`/`:drop` (prompt `kiln#`; cosmetic, there is no sandbox yet), `!shell-cmd`.
Results are echoed as `=> value` and bound to `_`. After an error the prompt shows `kiln!>`.

## Language reference (supported subset)

- **Comments:** `//`, nested `/* */`. No semicolons: a newline ends a statement (`;` is allowed as a separator).
- **Bindings:** `let` (immutable), `var` (mutable), `const`. Types are inferred; annotations are optional: `let x: Int = 5`.
- **Types:** `Int` (64-bit, wraps on overflow), `Float` (64-bit), `Bool`, `Str` (UTF-8, byte-indexed),
  lists `[T]`, and user `struct`s. The doc's lowercase names `int`, `float`, `bool`, `str`, `i64`, `f64` are aliases.
- **Literals:** `1_000_000`, `0xFF`, `0b1010`, `0o755`, `1.5e3`, `"..."`, `"""multi-line"""`, `'c'` (a 1-char Str), `[1, 2, 3]`, `Point { x: 1, y }` (field shorthand).
- **Functions:** `fn add(a: Int, b: Int) -> Int { a + b }` (implicit return of the last expression),
  `return`, and single-expression functions `fn area(r: Float) -> Float = 3.14 * r * r`. Functions can be declared in any order.
- **UFCS and pipelines:** `x.f(y)` is `f(x, y)`, and `xs.len` is `len(xs)`. `x |> f(y)` is `f(x, y)`.
- **Control flow (expressions where it makes sense):** `if/else if/else` produces a value;
  `match` on Int/Str/Bool with literals, `a | b`, ranges `1..=9`, bindings, `when` guards and `_`
  (it must be exhaustive); `while`, `loop`, `for i in a..b` / `a..=b`, `for x in list`, `for ch in str`,
  `break`, `continue`, `guard cond else { return ... }`, `assert cond, "msg"`.
- **Operators:** `+ - * / %` (Int division truncates like C), `== != < > <= >=` (also on Str),
  `&& || !` (also `and or not`), `& | ^ << >>`, `in` (list, substring, or range membership), `as` casts (`x as Float`, `f as Int`, `v as Str`).
- **Strings:** `+` concatenation; interpolation `"{expr}"` with format specs `{x:>8}` `{x:<20}` `{x:^6}` `{x:08}` `{x:.3f}` `{x:e}` `{n:x}` `{n:b}`; `{{`/`}}` are literal braces;
  `s[i]`, `s[-1]`, `s[a..b]`.
- **Lists:** `xs[i]`, `xs[-1]`, `xs[a..b]`, `xs[i] = v`, `push`, `pop`, `clear`, `len`, `contains`, `sort` (returns a sorted copy),
  `reverse` (copy), `sum`, `join`, `fill(n, v)`. Lists are reference types; structs are value types (copied when assigned).
- **Built-ins:** `print`, `println`, `eprint`, `eprintln` (any value, several args separated by spaces), `str`,
  `len`, `split`, `split_whitespace`, `lines`, `trim`, `to_upper`, `to_lower`, `starts_with`, `ends_with`,
  `find`, `replace`, `repeat`, `parse_int`, `parse_float`, `abs`, `min`, `max`, `sqrt`, `pow`, `floor`, `ceil`,
  `sin`, `cos`, `exit`, `panic`, `ask(prompt)`, `read_line`, `env(name)`, `now_ms`,
  `fs.read_text`, `fs.write_text`, `fs.exists`.
- **Entry point:** `fn main()`, `fn main() -> Int` (exit code), or `fn main(args: [Str])`.
- **Accepted and ignored:** `module ...`, `use ...`, `pub`, and attributes like `@derive(Show, Eq)`.
- **Runtime errors** (index out of bounds, division by zero, `panic`) print `panic: ...` and exit with code 101.
- **Compile errors** show `file:line:col: error: message`, the source line, and a caret, with "did you mean" suggestions.

## Not yet supported

Optionals (`T?`, `nil`, `?.`, `??`, `if let`), error unions (`T!E`, `try`, `catch`, `Ok/Err`),
enums/ADTs and destructuring patterns, traits/`impl`/methods, generics, lambdas and trailing blocks (`it`),
first-class functions, maps/sets/tuples, list comprehensions, slices with open ends (`xs[1..]`), `defer`,
`with`, labeled loops, `break value`, default/named/variadic parameters, `mut` params, modules and imports across files,
concurrency (`spawn`, `scope`, channels, `select`, actors, `async/await`), `comptime`, macros, `unsafe`/`asm`/FFI,
query expressions, broadcasting operators, sized ints (`i32`, `u8`, ...), `char`, struct/list equality,
capability sandboxing, `kiln fmt/lint/doc/add/publish` and the other package-manager verbs, and the debugger.
There is no garbage collector: memory is released when the program exits, which is fine for scripts and tools
but not for long-running servers.

## Tests and benchmarks

```
tests/run_tests.sh          # builds and runs every example, checks the interpreter, errors, REPL
python3 bench/run_bench.py  # fib(32), fib(40), 10M sieve: Kiln default / --release (gcc, zig cc) vs plain C vs Python
                            # options: --runs N --py-runs N --zig /path/to/zig --interp
```

## Layout

```
kiln.py            entry point            kiln / kiln.cmd   wrappers (bash / Windows)
kiln_lang/         lexer.py parser.py ast_nodes.py ktypes.py checker.py codegen.py runtime.py
                   interp.py (REPL engine) repl.py driver.py (CLI, compiler discovery)
runtime/           kiln_runtime.h (reference copy of the embedded C runtime)
examples/          hello fib fizzbuzz structs_arrays sieve features wordcount (+ data/)
tests/             run_tests.sh, expected outputs, error cases, REPL transcript
bench/             fib32 / fib40 / sieve10m in Kiln and Python, fib.c / sieve.c (plain C), run_bench.py
```

## Graphics, modules, and FFI

Kiln can call C / OS libraries, split programs across files, and draw pixels.

### Modules
```
use gfx                 // loads gfx.kn or gfx/mod.kn (program dir, then lib/)
pub fn clear(...)       // export for importers; call as gfx.clear(...)
```
Cycles are rejected with a clear error.

### FFI
```
@link("user32")
extern fn MessageBoxA(hwnd: Ptr, text: CStr, title: CStr, flags: U32) -> I32

@link("c")
extern fn puts(s: CStr) -> I32
```
Sized types: `I8`/`U8`/`I16`/`U16`/`I32`/`U32`/`I64`/`U64`, `Bool`, `Ptr`, `CStr` (auto from `Str`).
Lists expose `.ptr` for passing buffers to C. `@link("lib")` adds `-llib` (and `@link("kiln_gfx")` pulls in `runtime/kiln_gfx.c`).

### Graphics (`lib/gfx.kn`)
Software renderer into a `[U32]` pixel buffer; OS window via a small C shim (`runtime/kiln_gfx.c`: Win32 StretchDIBits / X11 XPutImage).

```
use gfx
fn main() {
    gfx.open("Hi", 320, 200, 2)
    while gfx.running() {
        gfx.clear(gfx.rgb(20, 20, 40))
        gfx.rect(10, 10, 50, 40, gfx.rgb(80, 180, 255))
        gfx.text(8, 8, "HELLO", gfx.rgb(255, 255, 255))
        gfx.present()
        gfx.limit_fps(60)
    }
}
```
Demos: `examples/gfx_demo.kn`, `examples/snake.kn`, `examples/ffi_hello.kn`, `examples/ffi_msgbox.kn` (Windows).


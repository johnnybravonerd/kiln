# Kiln for Windows (self-contained)

**Double-click `Kiln.bat`.** A terminal window opens with the Kiln REPL (`kiln>`).
Type `:q` to leave the REPL. You stay at a prompt in the Kiln folder where `kiln` works:

```
kiln run examples\hello.kn
kiln build examples\fib.kn        (creates fib.exe, which runs without Kiln)
kiln repl
```

Nothing needs installing: Python (embeddable) is in `toolchain\python` and the
C compiler (Zig) is in `toolchain\zig`. **The first build takes about 10-60 seconds**
while Zig prepares its C library cache (stored in `%TEMP%\kiln-zig-cache`); after that, builds take about a second.
If Windows SmartScreen warns about `Kiln.bat`, choose "More info" -> "Run anyway".
To change the window that opens, read `HOW-TO-EDIT-LAUNCHER.txt`.

---


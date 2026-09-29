@echo off
rem ======================================================================
rem  Kiln launcher + command-line wrapper (Windows)
rem
rem  Double-click      -> opens a terminal window running the Kiln REPL.
rem  kiln <command>    -> runs the compiler, e.g.  kiln run examples\fib.kn
rem
rem  Windows file names ignore case, so "kiln.bat" and "Kiln.bat" are the
rem  same file: this one file does both jobs. It uses only the Python and
rem  Zig bundled in the toolchain folder (no install needed).
rem  To change the window it opens, see HOW-TO-EDIT-LAUNCHER.txt
rem ======================================================================
setlocal
if not "%~1"=="" goto cli
if defined KILN_SHELL goto cli

rem ---------------- launcher mode (double-click) ----------------
rem Windows Terminal if available. -d sets the starting folder; the "." after
rem %~dp0 avoids a trailing backslash, which would break the quoting.
where wt >nul 2>nul
if not errorlevel 1 (
    start "" wt -d "%~dp0." --title "Kiln" cmd /k "%~dp0kiln-shell.bat"
    goto :eof
)
rem Fallback: classic console window
start "Kiln" /D "%~dp0." cmd /k "%~dp0kiln-shell.bat"
goto :eof

rem ---------------- command-line mode ----------------
:cli
"%~dp0toolchain\python\python.exe" "%~dp0kiln.py" %*
exit /b %ERRORLEVEL%

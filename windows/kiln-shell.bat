@echo off
rem Runs inside the window opened by Kiln.bat: puts `kiln` on PATH, starts the REPL,
rem and leaves you at a prompt in the Kiln folder when you quit the REPL with :q
set "KILN_SHELL=1"
set "PATH=%~dp0;%PATH%"
title Kiln
cd /d "%~dp0"
call "%~dp0Kiln.bat" repl
echo.
echo You are in the Kiln folder. Try:
echo    kiln run examples\hello.kn      compile and run a program
echo    kiln build examples\fib.kn      make fib.exe
echo    kiln repl                       start the REPL again
echo    kiln help                       all commands
echo.

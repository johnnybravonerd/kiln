@echo off
rem Kiln wrapper for Windows: forwards all arguments to kiln.py
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "%~dp0kiln.py" %*
) else (
  python "%~dp0kiln.py" %*
)
exit /b %ERRORLEVEL%

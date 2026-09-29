@echo off
rem Creates a "Kiln" shortcut on the desktop that starts Kiln.bat with kiln.ico
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Kiln.lnk'));" ^
  "$s.TargetPath='%~dp0Kiln.bat'; $s.WorkingDirectory='%~dp0.'; $s.IconLocation='%~dp0kiln.ico,0';" ^
  "$s.Description='Kiln programming language'; $s.WindowStyle=7; $s.Save(); Write-Host ('Created ' + (Join-Path $d 'Kiln.lnk'))"
pause

@echo off
setlocal
cd /d "%~dp0\..\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-LiveNoteMultiAutomation.ps1" -ConfigPath "%~dp0livenote-multi.json"
if errorlevel 1 (
  echo.
  echo LiveNote multi-server automation did not start. Check livenote-multi.json and .runtime-logs.
  pause
)
endlocal

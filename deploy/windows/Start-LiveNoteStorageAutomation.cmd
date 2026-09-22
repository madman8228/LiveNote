@echo off
setlocal
cd /d "%~dp0\..\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-LiveNoteStorageAutomation.ps1" -ConfigPath "%~dp0livenote-local.env"
if errorlevel 1 (
  echo.
  echo LiveNote local automation did not start. Check livenote-local.env and .runtime-logs.
  pause
)
endlocal

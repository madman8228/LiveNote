param(
  [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
  [string]$ConfigPath = (Join-Path $PSScriptRoot 'livenote-multi.json')
)

$ErrorActionPreference = 'Stop'
$logRoot = Join-Path $ProjectRoot '.runtime-logs'
$logPath = Join-Path $logRoot 'multi-automation-restart.log'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

function Find-MultiAutomation {
  @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | Where-Object {
    ([string]$_.CommandLine) -match '(?i)tools[\\/]livenote_multi_automation\.py'
  })
}

try {
  foreach ($process in @(Find-MultiAutomation)) {
    Stop-Process -Id $process.ProcessId -Force
  }
  for ($attempt = 0; $attempt -lt 20 -and (Find-MultiAutomation).Count -gt 0; $attempt++) {
    Start-Sleep -Milliseconds 250
  }
  & (Join-Path $ProjectRoot 'deploy\windows\Start-LiveNoteMultiAutomation.ps1') `
    -ConfigPath $ConfigPath | Out-File -FilePath $logPath -Encoding utf8
} catch {
  $_ | Out-File -FilePath $logPath -Encoding utf8
  exit 1
}

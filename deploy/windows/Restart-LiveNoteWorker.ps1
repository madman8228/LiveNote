param(
  [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
)

$ErrorActionPreference = 'Stop'
$logRoot = Join-Path $ProjectRoot '.runtime-logs'
$logPath = Join-Path $logRoot 'worker-restart.log'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

function Find-RunningWorker {
  @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | Where-Object {
    ([string]$_.CommandLine) -match '(?i)tools[\\/]livenote_worker\.py.*\bprocess\b.*--watch(?:\s|$)'
  })
}

try {
  $workers = @(Find-RunningWorker)
  foreach ($worker in $workers) {
    Stop-Process -Id $worker.ProcessId -Force
  }
  for ($attempt = 0; $attempt -lt 20 -and (Find-RunningWorker).Count -gt 0; $attempt++) {
    Start-Sleep -Milliseconds 250
  }

  & (Join-Path $ProjectRoot 'deploy\windows\Start-LiveNoteStorageAutomation.ps1') `
    -ConfigPath (Join-Path $ProjectRoot 'deploy\windows\livenote-local.env') |
    Out-File -FilePath $logPath -Encoding utf8
} catch {
  $_ | Out-File -FilePath $logPath -Encoding utf8
  exit 1
}

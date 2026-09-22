param(
  [string]$ServerUrl = '',
  [string]$ApiKey = '',
  [string]$WorkerToken = '',
  [string]$WorkerId = 'local-pc',
  [string]$Python = 'python',
  [string]$ConfigPath = (Join-Path $PSScriptRoot 'livenote-local.env')
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$logRoot = Join-Path $projectRoot '.runtime-logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

if (Test-Path -LiteralPath $ConfigPath) {
  foreach ($line in Get-Content -LiteralPath $ConfigPath) {
    if ($line -match '^\s*#' -or $line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { continue }
    $name = $Matches[1]
    $value = $Matches[2].Trim().Trim('"').Trim("'")
    if ($name -eq 'LIVENOTE_SERVER_URL' -and [string]::IsNullOrWhiteSpace($ServerUrl)) { $ServerUrl = $value }
    if ($name -eq 'LIVENOTE_API_KEY' -and [string]::IsNullOrWhiteSpace($ApiKey)) { $ApiKey = $value }
    if ($name -eq 'LIVENOTE_WORKER_TOKEN' -and [string]::IsNullOrWhiteSpace($WorkerToken)) { $WorkerToken = $value }
    if ($name -eq 'LIVENOTE_WORKER_ID' -and $WorkerId -eq 'local-pc') { $WorkerId = $value }
    if ($name -eq 'PYTHON' -and $Python -eq 'python') { $Python = $value }
  }
}

if ([string]::IsNullOrWhiteSpace($ServerUrl)) {
  throw "Missing ServerUrl. Copy livenote-local.env.example to livenote-local.env and fill in the ECS API address."
}
if ([string]::IsNullOrWhiteSpace($ApiKey)) { $ApiKey = $env:LIVENOTE_API_KEY }
if ([string]::IsNullOrWhiteSpace($WorkerToken)) { $WorkerToken = $env:LIVENOTE_WORKER_TOKEN }

function Find-RunningScript {
  param([string]$Pattern)
  @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | Where-Object {
    ([string]$_.CommandLine) -match $Pattern
  })
}

$previous = @{
  LIVENOTE_SERVER_URL = $env:LIVENOTE_SERVER_URL
  LIVENOTE_API_KEY = $env:LIVENOTE_API_KEY
  LIVENOTE_WORKER_TOKEN = $env:LIVENOTE_WORKER_TOKEN
  LIVENOTE_WORKER_ID = $env:LIVENOTE_WORKER_ID
  LIVENOTE_PROCESSING_MODE = $env:LIVENOTE_PROCESSING_MODE
}

$env:LIVENOTE_SERVER_URL = $ServerUrl.TrimEnd('/')
$env:LIVENOTE_API_KEY = $ApiKey
$env:LIVENOTE_WORKER_TOKEN = $WorkerToken
$env:LIVENOTE_WORKER_ID = $WorkerId
$env:LIVENOTE_PROCESSING_MODE = 'storage'

try {
  $worker = @(Find-RunningScript '(?i)tools[\\/]livenote_worker\.py.*\bprocess\b.*--watch(?:\s|$)')
  if ($worker.Count -eq 0) {
    $workerProcess = Start-Process -FilePath $Python `
      -ArgumentList @('tools/livenote_worker.py', '--server', $env:LIVENOTE_SERVER_URL, '--worker-id', $WorkerId, 'process', '--watch') `
      -WorkingDirectory $projectRoot `
      -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $logRoot 'storage-worker.stdout.log') `
      -RedirectStandardError (Join-Path $logRoot 'storage-worker.stderr.log') `
      -PassThru
    Write-Output "LiveNote storage Worker started. PID=$($workerProcess.Id)"
  } else {
    Write-Output "LiveNote storage Worker already running. PID=$($worker | Select-Object -ExpandProperty ProcessId -First 1)"
  }

  $bridge = @(Find-RunningScript '(?i)tools[\\/]livenote_codex_bridge\.py.*\bwatch(?:\s|$)')
  if ($bridge.Count -eq 0) {
    $bridgeProcess = Start-Process -FilePath $Python `
      -ArgumentList @('tools/livenote_codex_bridge.py', '--server', $env:LIVENOTE_SERVER_URL, 'watch') `
      -WorkingDirectory $projectRoot `
      -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $logRoot 'codex-bridge.stdout.log') `
      -RedirectStandardError (Join-Path $logRoot 'codex-bridge.stderr.log') `
      -PassThru
    Write-Output "LiveNote Codex Bridge started. PID=$($bridgeProcess.Id)"
  } else {
    Write-Output "LiveNote Codex Bridge already running. PID=$($bridge | Select-Object -ExpandProperty ProcessId -First 1)"
  }

  $dashboard = @(Find-RunningScript '(?i)tools[\\/]livenote_worker_dashboard\.py')
  if ($dashboard.Count -eq 0) {
    $dashboardProcess = Start-Process -FilePath $Python `
      -ArgumentList @('tools/livenote_worker_dashboard.py') `
      -WorkingDirectory $projectRoot `
      -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $logRoot 'worker-dashboard.stdout.log') `
      -RedirectStandardError (Join-Path $logRoot 'worker-dashboard.stderr.log') `
      -PassThru
    Write-Output "LiveNote Worker dashboard started. PID=$($dashboardProcess.Id)"
  } else {
    Write-Output "LiveNote Worker dashboard already running. PID=$($dashboard | Select-Object -ExpandProperty ProcessId -First 1)"
  }
  Start-Process 'http://127.0.0.1:8765/worker'
} finally {
  foreach ($name in $previous.Keys) {
    if ($null -eq $previous[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
    else { Set-Item "Env:$name" $previous[$name] }
  }
}

Write-Output 'Local automation started: ECS chunks -> local FFmpeg/Whisper -> local Codex -> ECS.'

param(
  [string]$ConfigPath = (Join-Path $PSScriptRoot 'livenote-multi.json'),
  [string]$EnvPath = (Join-Path $PSScriptRoot 'livenote-local.env'),
  [string]$Python = 'python',
  [switch]$SkipLocalServer
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$configFullPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$logRoot = Join-Path $projectRoot '.runtime-logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

if (Test-Path -LiteralPath $EnvPath) {
  foreach ($line in Get-Content -LiteralPath $EnvPath) {
    if ($line -match '^\s*#' -or $line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { continue }
    $name = $Matches[1]
    $value = $Matches[2].Trim().Trim('"').Trim("'")
    Set-Item "Env:$name" $value
  }
}

& $Python (Join-Path $projectRoot "tools\livenote_multi_automation.py") --config $configFullPath --python $Python --validate-only
if ($LASTEXITCODE -ne 0) { throw "Multi-server configuration validation failed." }

$existing = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | Where-Object {
  $line = [string]$_.CommandLine
  $line.Contains('livenote_worker.py') -or $line.Contains('livenote_codex_bridge.py')
})
if ($existing.Count -gt 0) {
  $pids = ($existing | Select-Object -ExpandProperty ProcessId) -join ', '
  throw "Existing single-service Worker or Codex Bridge found (PID: $pids). Stop the old automation before starting multi-server mode."
}

$multiConfig = Get-Content -LiteralPath $configFullPath -Raw | ConvertFrom-Json
$hasLocalTarget = $multiConfig.targets.id -contains "local"
if ($hasLocalTarget -and -not $SkipLocalServer) {
  $localApi = @(Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue)
  if ($localApi.Count -eq 0) {
    $previousMode = $env:LIVENOTE_PROCESSING_MODE
    $previousLive = $env:LIVENOTE_LIVE_PROCESSING_ENABLED
    $previousApiKey = $env:LIVENOTE_API_KEY
    $previousWorkerToken = $env:LIVENOTE_WORKER_TOKEN
    $previousInstanceId = $env:LIVENOTE_INSTANCE_ID
    try {
      $env:LIVENOTE_PROCESSING_MODE = "storage"
      $env:LIVENOTE_LIVE_PROCESSING_ENABLED = "0"
      $env:LIVENOTE_API_KEY = $env:LIVENOTE_LOCAL_API_KEY
      $env:LIVENOTE_WORKER_TOKEN = $env:LIVENOTE_LOCAL_WORKER_TOKEN
      $env:LIVENOTE_INSTANCE_ID = "local"
      & (Join-Path $PSScriptRoot "Start-LiveNoteApi.ps1") -Port 8000 -Python $Python
      if ($LASTEXITCODE -ne 0) { throw "Local Server startup failed." }
    } finally {
      if ($null -eq $previousMode) { Remove-Item Env:LIVENOTE_PROCESSING_MODE -ErrorAction SilentlyContinue } else { Set-Item Env:LIVENOTE_PROCESSING_MODE $previousMode }
      if ($null -eq $previousLive) { Remove-Item Env:LIVENOTE_LIVE_PROCESSING_ENABLED -ErrorAction SilentlyContinue } else { Set-Item Env:LIVENOTE_LIVE_PROCESSING_ENABLED $previousLive }
      if ($null -eq $previousApiKey) { Remove-Item Env:LIVENOTE_API_KEY -ErrorAction SilentlyContinue } else { Set-Item Env:LIVENOTE_API_KEY $previousApiKey }
      if ($null -eq $previousWorkerToken) { Remove-Item Env:LIVENOTE_WORKER_TOKEN -ErrorAction SilentlyContinue } else { Set-Item Env:LIVENOTE_WORKER_TOKEN $previousWorkerToken }
      if ($null -eq $previousInstanceId) { Remove-Item Env:LIVENOTE_INSTANCE_ID -ErrorAction SilentlyContinue } else { Set-Item Env:LIVENOTE_INSTANCE_ID $previousInstanceId }
    }
  } else {
    $healthHeaders = @{}
    if ($env:LIVENOTE_LOCAL_API_KEY) { $healthHeaders['X-API-Key'] = $env:LIVENOTE_LOCAL_API_KEY }
    try {
      $health = (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/v1/health' -Headers $healthHeaders -UseBasicParsing -TimeoutSec 5).Content | ConvertFrom-Json
    } catch {
      throw "Local port 8000 is in use but its health status could not be read: $($_.Exception.Message)"
    }
    if ($health.capabilities.storageOnly -ne $true) {
      throw 'Local Server is not in ECS storage mode. Stop the local transcriber and start it in storage mode to avoid duplicate task processing.'
    }
  }
}

$previousConfig = $env:LIVENOTE_MULTI_CONFIG
$env:LIVENOTE_MULTI_CONFIG = $configFullPath
try {
  $supervisor = Start-Process -FilePath $Python `
    -ArgumentList @((Join-Path $projectRoot 'tools\livenote_multi_automation.py'), '--config', $configFullPath, '--python', $Python) `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logRoot 'multi-automation.stdout.log') `
    -RedirectStandardError (Join-Path $logRoot 'multi-automation.stderr.log') `
    -PassThru

  $dashboard = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | Where-Object {
    ([string]$_.CommandLine).Contains('livenote_worker_dashboard.py')
  })
  if ($dashboard.Count -eq 0) {
    Start-Process -FilePath $Python `
      -ArgumentList @((Join-Path $projectRoot 'tools\livenote_worker_dashboard.py')) `
      -WorkingDirectory $projectRoot `
      -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $logRoot 'worker-dashboard.stdout.log') `
      -RedirectStandardError (Join-Path $logRoot 'worker-dashboard.stderr.log') | Out-Null
  }
} finally {
  if ($null -eq $previousConfig) { Remove-Item Env:LIVENOTE_MULTI_CONFIG -ErrorAction SilentlyContinue }
  else { Set-Item Env:LIVENOTE_MULTI_CONFIG $previousConfig }
}

Start-Process 'http://127.0.0.1:8765/worker'
Write-Output "LiveNote multi-server automation started. PID=$($supervisor.Id)"
Write-Output 'Sources: local Server + ECS cloud. Task inboxes and status are isolated by source.'

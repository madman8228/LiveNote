param(
  [string]$ConfigPath = (Join-Path $PSScriptRoot 'livenote-multi.json'),
  [string]$EnvPath = (Join-Path $PSScriptRoot 'livenote-local.env'),
  [string]$Python = 'python'
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

& $Python (Join-Path $projectRoot 'tools\livenote_multi_automation.py') --config $configFullPath --python $Python --validate-only
if ($LASTEXITCODE -ne 0) { throw '多服务配置校验失败。' }

$existing = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | Where-Object {
  ([string]$_.CommandLine) -match '(?i)tools[\\/]livenote_(worker|codex_bridge)\.py'
})
if ($existing.Count -gt 0) {
  $pids = ($existing | Select-Object -ExpandProperty ProcessId) -join ', '
  throw "检测到已有单服务 Worker 或 Codex Bridge（PID: $pids）。请先停止旧自动处理，再启动多服务模式。"
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
    ([string]$_.CommandLine) -match '(?i)tools[\\/]livenote_worker_dashboard\.py'
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
Write-Output '来源：本地 Server + ECS 云端；任务目录和状态会按来源隔离。'

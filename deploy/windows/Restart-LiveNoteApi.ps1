param(
  [int]$Port = 8000,
  [string]$Python = 'python',
  [switch]$ConfirmRestart
)

if (-not $ConfirmRestart) {
  throw '重启 API 会终止占用该端口的进程。请确认目标是 LiveNote API 后，再附加 -ConfirmRestart 执行。'
}

$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if ($listeners.Count -eq 0) {
  & (Join-Path $PSScriptRoot 'Start-LiveNoteApi.ps1') -Port $Port -Python $Python
  exit $LASTEXITCODE
}

$processIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
$processes = foreach ($processId in $processIds) {
  $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId"
  if ($null -eq $process) {
    throw "无法读取端口 $Port 的进程信息（PID: $processId），为安全起见不执行重启。"
  }
  $process
}

$unexpected = @($processes | Where-Object {
  $commandLine = [string]$_.CommandLine
  $commandLine -notmatch '(?i)(^|[\\/\s])server[\\/]main\.py([\s"]|$)'
})
if ($unexpected.Count -gt 0) {
  $details = ($unexpected | ForEach-Object { "PID $($_.ProcessId): $($_.CommandLine)" }) -join '; '
  throw "端口 $Port 包含无法确认属于 LiveNote 的进程，已拒绝重启：$details"
}

foreach ($processId in $processIds) {
  Stop-Process -Id $processId -Force -ErrorAction Stop
}

for ($attempt = 0; $attempt -lt 20; $attempt++) {
  if (@(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue).Count -eq 0) { break }
  Start-Sleep -Milliseconds 250
}

if (@(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue).Count -gt 0) {
  throw "端口 $Port 在停止旧进程后仍被占用，未启动新 API。"
}

& (Join-Path $PSScriptRoot 'Start-LiveNoteApi.ps1') -Port $Port -Python $Python
exit $LASTEXITCODE

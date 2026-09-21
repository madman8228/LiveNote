param(
  [int]$Port = 8000,
  [string]$Python = 'python',
  [switch]$ConfirmRestart
)

if (-not $ConfirmRestart) {
  throw 'Restarting the API terminates the process using this port. Confirm it is the LiveNote API and pass -ConfirmRestart.'
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
    throw "Cannot inspect the process on port $Port (PID: $processId); restart aborted for safety."
  }
  $process
}

$unexpected = @($processes | Where-Object {
  $commandLine = [string]$_.CommandLine
  $commandLine -notmatch '(?i)(^|[\\/\s])server[\\/]main\.py([\s"]|$)' -and
  $commandLine -notmatch '(?i)uvicorn\s+server\.main:app([\s"]|$)'
})
if ($unexpected.Count -gt 0) {
  $details = ($unexpected | ForEach-Object { "PID $($_.ProcessId): $($_.CommandLine)" }) -join '; '
  throw "Port $Port contains a process that cannot be confirmed as LiveNote; restart rejected: $details"
}

foreach ($processId in $processIds) {
  Stop-Process -Id $processId -Force -ErrorAction Stop
}

for ($attempt = 0; $attempt -lt 20; $attempt++) {
  if (@(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue).Count -eq 0) { break }
  Start-Sleep -Milliseconds 250
}

if (@(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue).Count -gt 0) {
  throw "Port $Port is still occupied after stopping the old process; the new API was not started."
}

& (Join-Path $PSScriptRoot 'Start-LiveNoteApi.ps1') -Port $Port -Python $Python
exit $LASTEXITCODE

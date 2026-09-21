param(
  [int]$Port = 8000,
  [string]$Python = 'python'
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
  $pids = ($listeners | Select-Object -ExpandProperty OwningProcess -Unique) -join ', '
  throw "Port $Port is already in use (PID: $pids). Confirm the old API process before restarting; this script will not terminate it automatically."
}

$logRoot = Join-Path $projectRoot '.runtime-logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$stdoutPath = Join-Path $logRoot 'api.stdout.log'
$stderrPath = Join-Path $logRoot 'api.stderr.log'
$transcriberStdoutPath = Join-Path $logRoot 'transcriber.stdout.log'
$transcriberStderrPath = Join-Path $logRoot 'transcriber.stderr.log'

$previousPort = $env:PORT
$env:PORT = [string]$Port
try {
  $process = Start-Process -FilePath $Python `
    -ArgumentList @('server/main.py') `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru
} finally {
  if ($null -eq $previousPort) { Remove-Item Env:PORT -ErrorAction SilentlyContinue }
  else { $env:PORT = $previousPort }
}

Write-Output "LiveNote API started. PID=$($process.Id), port=$Port"
Write-Output "stdout: $stdoutPath"
Write-Output "stderr: $stderrPath"

$transcriber = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | Where-Object {
  ([string]$_.CommandLine) -match '(?i)tools[\\/]livenote_transcriber\.py[\s"].*\bwatch\b'
})
if ($transcriber.Count -eq 0) {
  $processor = Start-Process -FilePath $Python `
    -ArgumentList @('tools/livenote_transcriber.py', 'watch') `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $transcriberStdoutPath `
    -RedirectStandardError $transcriberStderrPath `
    -PassThru
  Write-Output "LiveNote transcriber started. PID=$($processor.Id)"
  Write-Output "transcriber stdout: $transcriberStdoutPath"
  Write-Output "transcriber stderr: $transcriberStderrPath"
} else {
  Write-Output "LiveNote transcriber already running. PID=$($transcriber | Select-Object -ExpandProperty ProcessId -First 1)"
}

param(
  [int]$Port = 8000,
  [string]$Python = 'python'
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
  $pids = ($listeners | Select-Object -ExpandProperty OwningProcess -Unique) -join ', '
  throw "端口 $Port 已被占用（PID: $pids）。请先确认旧 API 进程，再决定是否手动重启；脚本不会自动终止它。"
}

$logRoot = Join-Path $projectRoot '.runtime-logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$stdoutPath = Join-Path $logRoot 'api.stdout.log'
$stderrPath = Join-Path $logRoot 'api.stderr.log'

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

Write-Output "LiveNote API 已启动，PID=$($process.Id)，端口=$Port"
Write-Output "标准输出：$stdoutPath"
Write-Output "错误输出：$stderrPath"

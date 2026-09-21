param(
  [int]$ApiPort = 8000,
  [int]$MobilePort = 5173,
  [int]$ControlPort = 4173,
  [string]$Python = 'python',
  [string]$Node = 'node'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$logRoot = Join-Path $projectRoot '.runtime-logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

function Get-ListeningProcessIds {
  param([int]$Port)

  @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique)
}

function Wait-ForPort {
  param(
    [int]$Port,
    [string]$ServiceName
  )

  for ($attempt = 0; $attempt -lt 30; $attempt++) {
    $listeners = @(Get-ListeningProcessIds -Port $Port)
    if ($listeners.Count -gt 0) {
      return $listeners
    }
    Start-Sleep -Milliseconds 250
  }

  throw "$ServiceName did not start on port $Port. Check .runtime-logs."
}

function Start-IfMissing {
  param(
    [int]$Port,
    [string]$ServiceName,
    [string]$FilePath,
    [string[]]$ArgumentList,
    [string]$StdoutFile,
    [string]$StderrFile
  )

  $existing = @(Get-ListeningProcessIds -Port $Port)
  if ($existing.Count -gt 0) {
    Write-Output "$ServiceName already running. PID=$($existing -join ', ')"
    return $existing
  }

  $process = Start-Process -FilePath $FilePath `
    -ArgumentList $ArgumentList `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logRoot $StdoutFile) `
    -RedirectStandardError (Join-Path $logRoot $StderrFile) `
    -PassThru

  $listeners = @(Wait-ForPort -Port $Port -ServiceName $ServiceName)
  Write-Output "$ServiceName started. PID=$($listeners -join ', ')"
  return $listeners
}

$nodeCommand = Get-Command $Node -ErrorAction SilentlyContinue
if ($null -eq $nodeCommand) {
  throw "Cannot find Node.js command '$Node'. Install Node.js or pass -Node with its full path."
}

$apiListeners = @(Get-ListeningProcessIds -Port $ApiPort)
if ($apiListeners.Count -eq 0) {
  & (Join-Path $PSScriptRoot 'Start-LiveNoteApi.ps1') -Port $ApiPort -Python $Python
  $apiListeners = @(Wait-ForPort -Port $ApiPort -ServiceName 'LiveNote API')
} else {
  Write-Output "LiveNote API already running. PID=$($apiListeners -join ', ')"
}

Start-IfMissing `
  -Port $MobilePort `
  -ServiceName 'LiveNote mobile HTTPS page' `
  -FilePath $nodeCommand.Source `
  -ArgumentList @('node_modules/vite/bin/vite.js', '--config', 'vite.config.ts', '--host', '0.0.0.0', '--port', [string]$MobilePort) `
  -StdoutFile 'mobile.stdout.log' `
  -StderrFile 'mobile.stderr.log' | Out-Null

Start-IfMissing `
  -Port $ControlPort `
  -ServiceName 'LiveNote control console' `
  -FilePath $nodeCommand.Source `
  -ArgumentList @('node_modules/vite/bin/vite.js', '--config', 'vite.control.config.ts') `
  -StdoutFile 'control.stdout.log' `
  -StderrFile 'control.stderr.log' | Out-Null

$lanAddresses = @(Get-NetIPConfiguration |
  Where-Object { $null -ne $_.IPv4DefaultGateway -and $null -ne $_.IPv4Address } |
  ForEach-Object { $_.IPv4Address.IPAddress } |
  Where-Object { $_ -notmatch '^(127\.|169\.254\.)' } |
  Select-Object -Unique)

Write-Output ''
Write-Output 'LiveNote is ready.'
Write-Output "PC control console: http://127.0.0.1:$ControlPort/?mode=control"
foreach ($address in $lanAddresses) {
  Write-Output "Phone: https://$address`:$MobilePort/"
}
Write-Output 'The phone and computer must be connected to the same Wi-Fi. The development HTTPS certificate may require one-time confirmation on the phone.'

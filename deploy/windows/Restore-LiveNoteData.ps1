param(
  [Parameter(Mandatory=$true)][string]$Backup,
  [string]$DataDirectory = '',
  [string]$DatabasePath = '',
  [string]$Python = 'python',
  [switch]$VerifyOnly,
  [switch]$Replace
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$arguments = @((Join-Path $projectRoot 'server\restore.py'), '--backup', [System.IO.Path]::GetFullPath($Backup))
if ($DataDirectory) { $arguments += @('--data-dir', [System.IO.Path]::GetFullPath($DataDirectory)) }
if ($DatabasePath) { $arguments += @('--db-path', [System.IO.Path]::GetFullPath($DatabasePath)) }
if ($VerifyOnly) { $arguments += '--verify-only' }
if ($Replace) { $arguments += '--replace' }

& $Python @arguments
if ($LASTEXITCODE -ne 0) { throw "LiveNote restore operation failed (exit code: $LASTEXITCODE)." }

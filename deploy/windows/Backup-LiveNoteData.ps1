param(
  [Parameter(Mandatory = $true)]
  [string]$Destination,
  [string]$Python = 'python'
)

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$destinationPath = [System.IO.Path]::GetFullPath($Destination)

& $Python (Join-Path $projectRoot 'server\backup.py') --destination $destinationPath
if ($LASTEXITCODE -ne 0) {
  throw "LiveNote data backup failed (exit code: $LASTEXITCODE)."
}

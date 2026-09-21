param(
  [int]$ApiPort = 8000,
  [string]$ApiKey = '',
  [switch]$SkipApi,
  [switch]$SkipAudit
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $projectRoot
try {
  Write-Output '== LiveNote release checks =='

  Write-Output '[1/6] Frontend build'
  & npm run build
  if ($LASTEXITCODE -ne 0) { throw "Frontend build failed (exit code: $LASTEXITCODE)." }

  Write-Output '[2/6] Backend tests'
  & python -m unittest discover -s server/tests -p 'test_*.py'
  if ($LASTEXITCODE -ne 0) { throw "Backend tests failed (exit code: $LASTEXITCODE)." }

  Write-Output '[3/6] Python compile check'
  & python -m compileall -q server
  if ($LASTEXITCODE -ne 0) { throw "Python compile check failed (exit code: $LASTEXITCODE)." }

  Write-Output '[4/6] Python dependency check'
  & python -m pip check
  if ($LASTEXITCODE -ne 0) { throw "Python dependency check failed (exit code: $LASTEXITCODE)." }

  Write-Output '[5/6] Git whitespace check'
  & git diff --check
  if ($LASTEXITCODE -ne 0) { throw "Git whitespace check failed (exit code: $LASTEXITCODE)." }

  if (-not $SkipAudit) {
    Write-Output '[6/6] npm security audit'
    & npm audit --registry=https://registry.npmjs.org --audit-level=high
    if ($LASTEXITCODE -ne 0) { throw "npm security audit failed (exit code: $LASTEXITCODE)." }
  } else {
    Write-Output '[6/6] npm security audit skipped'
  }

  if (-not $SkipApi) {
    Write-Output "[API] Checking port $ApiPort"
    $checkParameters = @{ Port = $ApiPort }
    if ($ApiKey) { $checkParameters['ApiKey'] = $ApiKey }
    & (Join-Path $PSScriptRoot 'Check-LiveNoteApi.ps1') @checkParameters
  } else {
    Write-Output '[API] check skipped'
  }

  Write-Output 'All selected LiveNote release checks passed.'
} finally {
  Pop-Location
}

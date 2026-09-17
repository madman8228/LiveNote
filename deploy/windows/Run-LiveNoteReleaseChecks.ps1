param(
  [int]$ApiPort = 8000,
  [string]$ApiKey = '',
  [switch]$SkipApi,
  [switch]$SkipAudit,
  [switch]$RequireProcessing,
  [switch]$RequireLlm
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $projectRoot
try {
  Write-Output '== LiveNote release checks =='

  Write-Output '[1/6] Frontend build'
  & npm run build
  if ($LASTEXITCODE -ne 0) { throw "前端构建失败，退出码：$LASTEXITCODE" }

  Write-Output '[2/6] Backend tests'
  & python -m unittest discover -s server/tests -p 'test_*.py'
  if ($LASTEXITCODE -ne 0) { throw "后端测试失败，退出码：$LASTEXITCODE" }

  Write-Output '[3/6] Python compile check'
  & python -m compileall -q server
  if ($LASTEXITCODE -ne 0) { throw "Python 编译检查失败，退出码：$LASTEXITCODE" }

  Write-Output '[4/6] Python dependency check'
  & python -m pip check
  if ($LASTEXITCODE -ne 0) { throw "Python 依赖检查失败，退出码：$LASTEXITCODE" }

  Write-Output '[5/6] Git whitespace check'
  & git diff --check
  if ($LASTEXITCODE -ne 0) { throw "Git 空白检查失败，退出码：$LASTEXITCODE" }

  if (-not $SkipAudit) {
    Write-Output '[6/6] npm security audit'
    & npm audit --registry=https://registry.npmjs.org --audit-level=high
    if ($LASTEXITCODE -ne 0) { throw "npm 安全检查失败，退出码：$LASTEXITCODE" }
  } else {
    Write-Output '[6/6] npm security audit skipped'
  }

  if (-not $SkipApi) {
    Write-Output "[API] Checking port $ApiPort"
    $checkParameters = @{ Port = $ApiPort }
    if ($ApiKey) { $checkParameters['ApiKey'] = $ApiKey }
    if ($RequireProcessing) { $checkParameters['RequireProcessing'] = $true }
    if ($RequireLlm) { $checkParameters['RequireLlm'] = $true }
    & (Join-Path $PSScriptRoot 'Check-LiveNoteApi.ps1') @checkParameters
  } else {
    Write-Output '[API] check skipped'
  }

  Write-Output '所有选定的 LiveNote 发布检查已通过。'
} finally {
  Pop-Location
}

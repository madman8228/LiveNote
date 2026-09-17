param(
  [int]$Port = 8000
)

$url = "http://127.0.0.1:$Port/api/v1/health"
try {
  $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
  $health = $response.Content | ConvertFrom-Json
} catch {
  throw "无法访问 $url：$($_.Exception.Message)"
}

$health | ConvertTo-Json -Depth 6
if ($health.storageSchema -ne 2 -or $null -eq $health.capabilities) {
  throw '当前 API 仍是旧版本：缺少 storageSchema=2 或 capabilities。请确认运行的是当前 server/main.py。'
}

Write-Output 'API 版本检查通过。'

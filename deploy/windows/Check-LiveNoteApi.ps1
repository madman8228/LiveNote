param(
  [int]$Port = 8000,
  [string]$ApiKey = ''
)

$url = "http://127.0.0.1:$Port/api/v1/health"
try {
  $requestHeaders = @{}
  if ($ApiKey) { $requestHeaders['X-API-Key'] = $ApiKey }
  $response = Invoke-WebRequest -Uri $url -Headers $requestHeaders -UseBasicParsing -TimeoutSec 5
  $health = $response.Content | ConvertFrom-Json
} catch {
  throw "Cannot access ${url}: $($_.Exception.Message)"
}

$health | ConvertTo-Json -Depth 6
if ($health.storageSchema -lt 5 -or $null -eq $health.capabilities) {
  throw 'The API is an old version: storageSchema>=5 and capabilities are required. Check server/main.py.'
}

if (-not $health.capabilities.manualProcessing) {
  throw 'The API version is correct, but manual PC processing is not enabled.'
}

Write-Output 'API health check passed.'

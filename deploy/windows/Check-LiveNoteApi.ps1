param(
  [int]$Port = 8000,
  [string]$ApiKey = '',
  [switch]$RequireProcessing,
  [switch]$RequireLlm
)

$url = "http://127.0.0.1:$Port/api/v1/health"
try {
  $requestHeaders = @{}
  if ($ApiKey) { $requestHeaders['X-API-Key'] = $ApiKey }
  $response = Invoke-WebRequest -Uri $url -Headers $requestHeaders -UseBasicParsing -TimeoutSec 5
  $health = $response.Content | ConvertFrom-Json
} catch {
  throw "无法访问 $url：$($_.Exception.Message)"
}

$health | ConvertTo-Json -Depth 6
if ($health.storageSchema -ne 2 -or $null -eq $health.capabilities) {
  throw '当前 API 仍是旧版本：缺少 storageSchema=2 或 capabilities。请确认运行的是当前 server/main.py。'
}

if ($RequireProcessing) {
  if (-not $health.capabilities.ffmpeg -or -not $health.capabilities.ffprobe) {
    throw 'API 版本正确，但 FFmpeg/FFprobe 未就绪，不能执行音频重建。'
  }
  if (-not $health.capabilities.asrModelCached) {
    throw "API 版本正确，但 Whisper 模型 $($health.capabilities.asrModel) 未缓存，不能执行 ASR。"
  }
}

if ($RequireLlm -and -not $health.capabilities.llmConfigured) {
  throw 'API 版本正确，但 LLM 尚未配置，不能生成语义总结。'
}

Write-Output 'API 版本检查通过。'

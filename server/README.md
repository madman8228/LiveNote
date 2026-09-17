# LiveNote API

```powershell
python -m pip install -r server/requirements.txt
python server/main.py
```

依赖中包含 `openai-whisper` 和 `torch`。Whisper 模型文件仍需单独准备到
`LIVENOTE_WHISPER_CACHE`，不会在录音过程中临时下载；生产部署前应先用短音频验证模型可加载。

默认监听 `0.0.0.0:8000`，SQLite 在 `server/livenote.sqlite3`，音频二进制文件在 `server/data/`。

可选环境变量：

- `LIVENOTE_MAX_CHUNK_BYTES`：单个 Chunk 最大字节数，默认 25 MB。
- `LIVENOTE_UPLOAD_DURATION_TOLERANCE_MS`：整场时长与最后一个已上传 Chunk 的允许差值，默认 15 秒。
- `LIVENOTE_CORS_ORIGINS`：逗号分隔的允许来源；默认允许本机 Vite HTTPS 地址。
- `LIVENOTE_API_KEY`：设置后，`/api/v1/*` 请求必须携带 `X-API-Key`；不设置时保持本地开发兼容。
- `LIVENOTE_ENV`：设置为 `production` 后，缺少 API Key 或 CORS 白名单会阻止服务启动。

生产部署不要依赖空 API Key 或默认 CORS。请复制项目根目录的 `.env.example` 和本目录的 `.env.example`，在服务管理器中设置真实的 `LIVENOTE_API_KEY`、前端 HTTPS 来源白名单，以及独立可备份的 `LIVENOTE_DATA_DIR`、`LIVENOTE_DB_PATH`。当前后台任务队列是单进程实现，API 必须使用 `--workers 1`。

访问 `/api/v1/health` 会返回 `storageSchema` 和处理能力状态，可确认 FFmpeg、默认 Whisper 模型缓存和 LLM 是否已准备好；手机设置页也会显示这些状态。

## 诊断资料

页面可以上传自动生成的 `snapshot.json`，并可附加图片、日志、文本或 JSON 文件：

```text
POST /api/v1/diagnostics
multipart/form-data
```

诊断资料保存到 `server/data/diagnostics/{diagnosticId}/`，默认单个文件最大 10 MB，可通过 `LIVENOTE_MAX_DIAGNOSTIC_BYTES` 调整。诊断快照只包含页面状态、错误、生命周期和 Session/Segment/Chunk 摘要，不包含录音 Blob；上传接口仍受 `LIVENOTE_API_KEY` 保护。

## M6 本地音频重建

本地电脑需要安装 FFmpeg，并确保 `ffmpeg` 和 `ffprobe` 在 PATH 中。也可以通过 `LIVENOTE_FFMPEG`、`LIVENOTE_FFPROBE` 指定可执行文件路径。

上传完成后，可以先重建单个 Segment：

```text
POST /api/v1/sessions/{sessionId}/segments/{segmentId}/reconstruct
GET  /api/v1/sessions/{sessionId}/segments/{segmentId}/audio
```

重建整场 Session：

```text
POST /api/v1/sessions/{sessionId}/reconstruct
GET  /api/v1/sessions/{sessionId}/audio
```

原始 Chunk 保留在 `server/data/sessions/`，重建后的 WebM 文件写入 `server/data/reconstructed/`。音频重建结果会继续交给后台 ASR 和报告处理任务。

整场播放、ASR 和后台处理只接受已经通过 Segment/Session 收尾校验的完整上传；如果仍有 Pending Chunk，接口会返回 `409`，避免把服务器上的半截音频误当成整场录音。

## M7 本地 ASR

超过 `LIVENOTE_ASR_CHUNK_SECONDS` 的长音频会按固定时长切分，并保留 1 秒重叠后逐段转写，再合并为统一时间轴；默认每段 300 秒、重叠 1 秒，避免把整场 1～4 小时音频一次性载入内存。

本机如果已经准备好 `openai-whisper`、PyTorch 和模型缓存，可以调用：

```text
POST /api/v1/sessions/{sessionId}/transcribe
Content-Type: application/json

{"model":"tiny","language":"zh"}
```

不传 `model` 时默认使用 `LIVENOTE_WHISPER_MODEL`，默认值为 `medium`。模型缓存目录默认是 `~/.cache/whisper`，也可以通过 `LIVENOTE_WHISPER_CACHE` 指定。接口会先重建 Session，再生成带 `startMs`、`endMs`、`text` 的逐字稿，并保存到：

```text
server/data/processed/sessions/{sessionId}/transcript.json
```

读取已生成逐字稿：

```text
GET /api/v1/sessions/{sessionId}/transcript
```

当前 ASR 使用 CPU 或本机可用的 CUDA 自动选择；还没有说话人识别和实时字幕。长音频会分段转写，避免一次性载入整场音频。

ASR 开始前会用 FFmpeg 检查输入音量，并将原始 WebM 临时转换为 16kHz、单声道、PCM WAV，应用温和的语音频段滤波和响度标准化后再交给 Whisper；原始重建音频不会被覆盖。明显接近静音时任务会失败并提示重新使用 `Speech` 配置录音，避免 Whisper 对低音量噪声生成重复幻觉。转写时关闭跨片段文本上下文累积，以降低长录音中的重复扩散。

## M8 结构化报告草稿

配置 LLM 后，报告除了关键知识点，还会返回 `knowledgeStructure`，每个节点包含 `title` 和 `points`，手机端会在总结阅读卡中按层级展示。

在已有逐字稿后，可以生成不依赖 LLM 的结构化报告草稿：

```text
POST /api/v1/sessions/{sessionId}/report
GET  /api/v1/sessions/{sessionId}/report
```

报告会整合 Session 元数据、ASR 时间轴和本地标记，分为重点、疑问、灵感、待办等部分。输出保存到：

```text
server/data/processed/sessions/{sessionId}/report.json
```

未配置 LLM 时，`summaryStatus` 为 `NOT_CONFIGURED`，报告会包含本地逐字稿、用户标记和基于 ASR 时间片的“本地抽取式草稿”。抽取式草稿只重排原文，不补写事实，也不等同于语义总结；配置 LLM 后才会生成真正的主题、知识点、知识结构和问答总结。

如果配置 `LIVENOTE_LLM_BASE_URL` 和 `LIVENOTE_LLM_MODEL`，报告生成时会调用 OpenAI 兼容的 `/chat/completions` 接口，输出主题、概览、关键知识点、问答、待办事项和实体列表。`LIVENOTE_LLM_API_KEY` 对本地 Ollama 等服务可省略，远程服务通常需要填写。未配置地址或模型时仍保存本地标记/逐字稿草稿，并将 `summaryStatus` 标记为 `NOT_CONFIGURED`；模型调用失败不会丢失 ASR 和本地草稿。

## 总结阅读卡

报告生成后，主流程由前端展示可切换背景的总结阅读卡，适合手机直接截图分享。服务器只返回结构化文字和时间轴，不生成图片；没有配置 LLM 时会明确显示为本地抽取式草稿。

## 服务端 Session 清理

服务端提供 `DELETE /api/v1/sessions/{sessionId}`，会删除该 Session 的数据库记录、Chunk 文件、重建音频、ASR/报告结果和处理任务状态。正在处理的 Session 会返回 `409`，避免后台任务和清理操作竞争。该接口受 API Key 保护；手机端服务器在线时，单个 Session 删除会同步清理服务器副本。

## M9 后台处理任务

长音频不建议让手机页面一直等待同步请求。现在可以创建一个本地后台处理任务：

```text
POST /api/v1/sessions/{sessionId}/process
Content-Type: application/json

{"model":"base","language":"zh"}
```

接口会立即返回任务编号，然后轮询：

```text
GET /api/v1/jobs/{jobId}
```

页面刷新后，可用 `GET /api/v1/sessions/{sessionId}/processing` 找回该 Session 最近一次处理任务；如果任务仍在排队或执行中，手机端会继续显示当前阶段。

任务状态包括 `QUEUED`、`RUNNING`、`COMPLETED`、`FAILED`，阶段包括 `RECONSTRUCTING`、`ASR`、`REPORT` 和 `DONE`。任务状态文件保存在 `server/data/processed/jobs/`。当前使用单进程、单并发队列，适合本地电脑验证；服务重启后，`QUEUED` 或 `RUNNING` 任务会自动重新排队，同一 Session 的重复提交会返回原任务编号。正式部署时仍应保持单 worker，或改用外部任务队列。

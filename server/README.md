# LiveNote API

```powershell
python -m pip install -r server/requirements.txt
python server/main.py
```

本机正式流程由独立的 `tools/livenote_transcriber.py` 使用缓存的 Whisper 自动转写，服务器只保存检查点、逐字稿和任务状态；总结由当前 Codex 聊天读取 `/summary-input` 后生成并回传。不会调用 OpenAI API。
`server/requirements.txt` 包含 Whisper/PyTorch；模型文件需要由部署提前准备，服务不会静默下载大型模型。

默认监听 `0.0.0.0:8000`，SQLite 在 `server/livenote.sqlite3`，音频二进制文件在 `server/data/`。

可选环境变量：

- `LIVENOTE_MAX_CHUNK_BYTES`：单个 Chunk 最大字节数，默认 25 MB。
- `LIVENOTE_UPLOAD_DURATION_TOLERANCE_MS`：整场时长与最后一个已上传 Chunk 的允许差值，默认 15 秒。
- `LIVENOTE_CORS_ORIGINS`：逗号分隔的允许来源；默认允许本机 Vite HTTPS 地址。
- `LIVENOTE_API_KEY`：设置后，`/api/v1/*` 请求必须携带 `X-API-Key`；不设置时保持本地开发兼容。
- `LIVENOTE_ENV`：设置为 `production` 后，缺少 API Key 或 CORS 白名单会阻止服务启动。
- `LIVENOTE_ADMIN_USERNAME` / `LIVENOTE_ADMIN_PASSWORD`：生产环境预先初始化 PC 管理控制台账号和密码；本地开发首次打开管理页时也可以直接完成初始化，账号哈希保存在 SQLite；旧版 `LIVENOTE_ADMIN_TOKEN` 可在迁移期间保留。

生产部署不要依赖空 API Key 或默认 CORS。请复制项目根目录的 `.env.example` 和本目录的 `.env.example`，在服务管理器中设置真实的 `LIVENOTE_API_KEY`、前端 HTTPS 来源白名单，以及独立可备份的 `LIVENOTE_DATA_DIR`、`LIVENOTE_DB_PATH`。当前后台任务队列是单进程实现，API 必须使用 `--workers 1`。

访问 `/api/v1/health` 会返回 `storageSchema` 和服务器能力状态，可确认 FFmpeg、FFprobe
和人工处理模式是否可用；手机设置页也会显示这些状态。

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

原始 Chunk 保留在 `server/data/sessions/`，重建后的 WebM 文件写入 `server/data/reconstructed/`。
音频重建结果会作为本机自动转写器的输入。

整场播放和任务处理只接受已经通过 Segment/Session 收尾校验的完整上传；如果仍有 Pending
Chunk，接口会返回 `409`，避免把服务器上的半截音频误当成整场录音。

### 上传期间增量识别

服务器收到连续的 4 个 30 秒 Chunk 后，会在单独的持久化 Worker 中尝试建立一个增量处理窗口，
并把已经提交的部分逐字稿保存到 `processed/sessions/<session-id>/transcript-live.json`。
原始 MediaRecorder Chunk 不是独立媒体文件，服务不会直接识别单个 `.bin`，而是先按连续 index
追加前缀暂存文件；缺片或增长中的 WebM 暂时无法解码时会等待并记录原因。录音结束后的完整处理
仍以全部 Chunk 为准，增量逐字稿不能直接发布。

可调参数：`LIVENOTE_LIVE_PROCESSING_ENABLED`（默认开启）、`LIVENOTE_LIVE_WINDOW_CHUNKS`
（2–4，默认 4）、`LIVENOTE_LIVE_WINDOW_OVERLAP_SECONDS`（默认 2）和
`LIVENOTE_LIVE_POLL_SECONDS`（默认 2）。增量状态可通过设备权限访问
`GET /api/v1/sessions/{sessionId}/live-processing`，部分文字通过
`GET /api/v1/sessions/{sessionId}/live-transcript` 读取。

## 电脑端自动处理任务

录音上传完成后，服务器会为 Session 创建 `processing_tasks` 任务。本机启动脚本会同时启动
API 和 `livenote_transcriber.py watch`；浏览器关闭不影响转写。管理员不需要领取文件，
也不需要选择结果 JSON。只有当用户在当前 Codex 聊天发起“总结这条录音”时，才进入总结阶段。

```text
READY
  → 本机后台自动重建并分段转写
  → TRANSCRIBED
  → 当前 Codex 聊天读取逐字稿并生成总结
  → REVIEW
  → 管理员发布
  → COMPLETED
```

控制台地址为 `http://127.0.0.1:4173/?mode=control`。本机模式直接读取服务器已保存的音频并分段落盘；
云端 Worker 仍保留为异机兼容入口。
云端模式下，家庭 PC 可以在控制台“设置”中填写 Worker 凭证并启动浏览器 Worker，
选择任务目录后自动领取和保存音频，也可以继续使用命令行 Worker 做异机轮询。
每段有独立的 source hash、模型、语言、运行代次和状态；服务重启后会复用已完成分段，不会从头重跑整场录音。
任务卡只展示“排队中、转写中、待总结、总结中、待发布、已发布”和真正需要处理的异常。

如果转写进程重启或中途失败，已完成分段会保留，后台会从失败分段继续；失败会明确标记为“失败”并保留重试入口，不会无限停留在“处理中”。

Codex 读取逐字稿使用：

```powershell
python tools/livenote_transcriber.py summary-prepare task-xxxx
```

提交时使用包含 `runId`、`generation`、`sourceHash` 和 `result` 的总结文件：

```powershell
python tools/livenote_transcriber.py summary-submit task-xxxx summary-result.json
```

回传结果使用结构化 JSON，最小格式见项目根目录 README 的 `knowledge.json` 示例；本地旧版 `result-local` 接口仅作为兼容入口保留。
服务器保存结果版本，管理员确认后才更新手机端可见的发布指针；手机不会看到未发布草稿。

## 总结阅读卡

发布后，主流程由前端展示可切换背景的总结阅读卡，适合手机直接截图分享。服务器只返回结构化文字，
不生成图片。

## 服务端 Session 清理

服务端提供 `DELETE /api/v1/sessions/{sessionId}`，会删除该 Session 的数据库记录、Chunk 文件、
重建音频、处理结果和处理任务状态。正在处理的 Session 会返回 `409`，避免任务和清理操作竞争。
该接口受设备身份或管理员权限保护；手机端服务器在线时，单个 Session 删除会同步清理服务器副本。

## 数据备份

可以在暂停录音上传后执行本地备份：

```powershell
python server/backup.py --destination D:\LiveNoteBackups
```

备份目录包含 SQLite 数据库、`data/` 下的音频和处理结果，以及 `manifest.json`。备份工具使用 SQLite 原生 backup API；数据库和文件目录仍不是同一时刻快照，生产环境应在维护窗口执行，或使用文件系统快照。备份目录不要放在 `LIVENOTE_DATA_DIR` 内部。

恢复前可先校验备份：

```powershell
python server/restore.py --backup D:\LiveNoteBackups\livenote-... --verify-only
```

恢复到新目录时指定 `--data-dir` 和 `--db-path`；默认不覆盖已有目标。确认替换时使用 `--replace`，旧目标会保留为 `before-restore` 副本，便于回滚。恢复前应暂停 API 和录音上传。

旧的 `/transcribe`、`/report`、`/process`、`/jobs` 和 `/processing` HTTP 入口已停用，
会返回 410。它们不属于当前手机录音到知识总结的正式链路。

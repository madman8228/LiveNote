# LiveNote 部署模板

这些文件是生产部署模板，不包含真实域名、证书路径或密钥。

## 推荐结构

```text
/opt/livenote/app/       LiveNote 代码和 server/
/opt/livenote/web/       npm run build 后的 dist/
/var/lib/livenote/       SQLite、Chunk、重建音频、ASR 结果
```

API 使用一个 worker：

```bash
uvicorn main:app --app-dir /opt/livenote/app/server --host 127.0.0.1 --port 8000 --workers 1
```

Nginx 对外提供 HTTPS，并把 `/api/` 代理到本机 8000 端口。请先配置环境变量，再启动服务；不要把真实 API Key 写入仓库。

## 2C2G ECS 存储模式

如果 ECS 只有 2 核 2 GB，使用 `server/requirements-storage.txt`，并设置：

```text
LIVENOTE_PROCESSING_MODE=storage
LIVENOTE_LOCAL_PULL_ENABLED=0
LIVENOTE_LIVE_PROCESSING_ENABLED=0
```

这种模式下 ECS 只负责 HTTPS、SQLite、原始 Chunk、任务状态、逐字稿和最终音频文件，
不安装 FFmpeg、Whisper、PyTorch，也不在服务器上做 ASR。安装完整 `server/requirements.txt`
或开启服务器端自动处理会违背这个部署约束。

本地电脑运行 `tools/livenote_worker.py process --watch`：它会自动领取任务、按 SHA-256
校验下载 Chunk，在本地用 FFmpeg 重建整场音频，用本机 Whisper 转写，再回传逐字稿和可播放
音频。回传完成后，现有 Codex 总结、管理员审核发布、手机读取结果的流程不变。

如果希望本地 Codex 自动生成总结，再启动另一个本地进程：

```powershell
python tools/livenote_codex_bridge.py watch
```

它只从 ECS 读取 `TRANSCRIBED/SUMMARIZING` 任务的逐字稿，调用本机 `codex exec`，再把结构化
总结回传 ECS。管理员仍在 ECS Web 页面审核并发布；也可以用 `once --task-id task-...` 只处理
一条指定任务。

Windows 也可以一次启动本地 Worker 和 Codex Bridge：

```powershell
powershell -ExecutionPolicy Bypass -File deploy/windows/Start-LiveNoteStorageAutomation.ps1 `
  -ServerUrl https://你的域名/api/v1 `
  -ApiKey 你的APIKey `
  -WorkerToken 你的WorkerToken
```

Nginx 可以直接参考 [`nginx/livenote.conf.example`](./nginx/livenote.conf.example)。它包含：

- HTTPS 和 HTTP → HTTPS 跳转
- Vue/Vite history fallback 到 `index.html`
- `/api/` 反向代理到 FastAPI
- 30 MB 上传上限，关闭上传请求缓冲，避免音频 Chunk 被代理截断
- 长达 1 小时的 API 超时，覆盖音频重建和处理轮询

建议把服务端配置保存为 `/etc/livenote/livenote.env`，至少包含：

```text
LIVENOTE_ENV=production
LIVENOTE_CORS_ORIGINS=https://你的域名
LIVENOTE_API_KEY=随机长密钥
LIVENOTE_ADMIN_USERNAME=管理员账号
LIVENOTE_ADMIN_PASSWORD=独立的管理员密码
# 旧版部署迁移期间可暂时保留 LIVENOTE_ADMIN_TOKEN
LIVENOTE_WORKER_TOKEN=独立的 Worker 密钥
LIVENOTE_DATA_DIR=/var/lib/livenote/data
LIVENOTE_DB_PATH=/var/lib/livenote/livenote.sqlite3
```

当前版本不在服务器上运行 Whisper、ASR 或 LLM。音频由电脑端领取后交给
本地 Whisper 和 Codex Bridge 自动处理，再通过控制台回传结果。密钥文件应限制为
API 服务用户可读，不能提交到 Git。

Windows 启动脚本会自动打开本地处理页面
`http://127.0.0.1:8765/worker`，用于查看 Worker、ECS 连接、当前阶段、进度和失败信息。

部署后的最低检查顺序：

1. 浏览器访问 `https://你的域名/`。
2. 确认浏览器能力检测和麦克风授权正常。
3. 录制 30 秒，确认 Chunk 上传到 API。
4. 在 PC 控制台完成“领取到本机 → 开始处理 → 回传结果 → 发布”。
5. 手机刷新会话，确认只显示已发布的知识总结。
6. 再进行长时间录音。

Windows 本地可先运行 `deploy\windows\Run-LiveNoteReleaseChecks.ps1`，一次完成构建、后端测试和编译检查。

正式运行前还应设置定期备份。Windows 本机可在暂停上传后执行 `python server/backup.py --destination D:\LiveNoteBackups`；Linux 环境建议由定时任务调用同一脚本，并把备份目录放在独立磁盘或远程备份位置。

备份可用 `python server/restore.py --backup <备份目录> --verify-only` 校验；恢复到新目录时指定 `--data-dir` 和 `--db-path`，替换现有数据必须显式加入 `--replace`。

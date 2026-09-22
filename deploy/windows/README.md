# Windows 本机 API 操作

这些脚本只管理当前项目启动的 API，不会自动终止已经占用 8000 端口的进程。

## 日常一键启动本地自动处理

如果 ECS 只负责保存上传文件，Whisper 和 Codex 在这台电脑上运行，第一次只需要：

1. 把 `livenote-local.env.example` 复制为 `livenote-local.env`。
2. 在新文件中填写 ECS 地址、LiveNote API Key 和 Worker Token。
3. 以后直接双击 `Start-LiveNoteStorageAutomation.cmd`。

这个入口会在后台启动本地 Worker、Codex Bridge 和本地处理页面，不启动 Android，也不会重启已经运行的 API。
启动后会自动打开 `http://127.0.0.1:8765/worker`，页面显示 ECS 连接、下载 Chunk、拼接录音、本地识别、总结回传和失败信息。
运行日志写入项目目录下的 `.runtime-logs`。关闭页面不会停止处理；网页仍然用于查看状态、审核和发布。
Worker 和本地总结默认每 5 分钟向 ECS 轮询一次；处理中的租约续期也按 5 分钟执行。需要调整时可设置 `LIVENOTE_WORKER_POLL_SECONDS`、`LIVENOTE_CODEX_POLL_SECONDS` 或 `LIVENOTE_WORKER_HEARTBEAT_SECONDS`。

## 一键发布检查

执行完整的电脑端回归检查：

```powershell
.\deploy\windows\Run-LiveNoteReleaseChecks.ps1
```

如果当前还没有最新 API，先跳过 API 检查；其余构建、测试和依赖检查仍会执行：

```powershell
.\deploy\windows\Run-LiveNoteReleaseChecks.ps1 -SkipApi
```

## 检查当前 API

```powershell
.\deploy\windows\Check-LiveNoteApi.ps1
```

检查会确认当前 API 版本、存储结构、FFmpeg/FFprobe 和本机自动转写能力已启用。

生产环境启用了 API Key 时，把 Key 只通过参数临时传入，不要写入仓库：

```powershell
.\deploy\windows\Check-LiveNoteApi.ps1 -ApiKey $env:LIVENOTE_API_KEY
```

如果返回的 JSON 没有 `storageSchema: 5` 和 `capabilities`，说明 8000 仍运行旧服务。

## 启动当前代码

启动完整的本机开发闭环（API、后台转写器、手机 HTTPS 页面和电脑控制台）：

```powershell
.\deploy\windows\Start-LiveNote.ps1
```

脚本会复用已经运行的服务，不会重复启动；手机地址会在输出中显示为
`https://电脑局域网IP:5173/`。手机和电脑必须连接同一 Wi-Fi，开发环境的 HTTPS
证书首次访问时需要在手机浏览器中确认继续访问。

确认 8000 没有被占用后：

```powershell
.\deploy\windows\Start-LiveNoteApi.ps1
```

脚本会在项目目录下创建 `.runtime-logs`，并把输出写入 `api.stdout.log` 和 `api.stderr.log`。

如果 8000 已被占用，脚本只显示 PID 并退出。确认确实是旧 LiveNote API 后，再由操作者手动停止该 PID，然后重新执行启动脚本。

也可以使用带有显式确认开关的重启脚本。它只会终止命令行中明确包含 `server/main.py` 的进程；如果端口属于其他程序，脚本会拒绝操作：

```powershell
.\deploy\windows\Restart-LiveNoteApi.ps1 -ConfirmRestart
```

没有 `-ConfirmRestart` 时，脚本不会停止任何进程。

## Windows 备份

暂停录音上传后，可把数据库、音频和处理结果备份到独立目录：

```powershell
.\deploy\windows\Backup-LiveNoteData.ps1 -Destination D:\LiveNoteBackups
```

备份目录不要放在 `server\data` 内部；脚本会拒绝这种路径。

恢复前先只校验备份：

```powershell
.\deploy\windows\Restore-LiveNoteData.ps1 -Backup D:\LiveNoteBackups\livenote-... -VerifyOnly
```

恢复到新的空目录时，不需要覆盖开关：

```powershell
.\deploy\windows\Restore-LiveNoteData.ps1 `
  -Backup D:\LiveNoteBackups\livenote-... `
  -DataDirectory D:\LiveNoteRestore\data `
  -DatabasePath D:\LiveNoteRestore\livenote.sqlite3
```

如果确认替换现有目标，必须显式加 `-Replace`。旧数据库和数据目录会保留为
`before-restore` 副本，便于回滚；恢复前仍建议暂停 API 和录音上传。

# Windows 本机 API 操作

这些脚本只管理当前项目启动的 API，不会自动终止已经占用 8000 端口的进程。

## 检查当前 API

```powershell
.\deploy\windows\Check-LiveNoteApi.ps1
```

如果要在发布前同时确认音频重建和 ASR 已就绪：

```powershell
.\deploy\windows\Check-LiveNoteApi.ps1 -RequireProcessing
```

如果还要求必须能生成语义总结：

```powershell
.\deploy\windows\Check-LiveNoteApi.ps1 -RequireProcessing -RequireLlm -ApiKey $env:LIVENOTE_API_KEY
```

生产环境启用了 API Key 时，把 Key 只通过参数临时传入，不要写入仓库：

```powershell
.\deploy\windows\Check-LiveNoteApi.ps1 -ApiKey $env:LIVENOTE_API_KEY
```

如果返回的 JSON 没有 `storageSchema: 2` 和 `capabilities`，说明 8000 仍运行旧服务。

## 启动当前代码

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

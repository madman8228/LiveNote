# Windows 本机 API 操作

这些脚本只管理当前项目启动的 API，不会自动终止已经占用 8000 端口的进程。

## 检查当前 API

```powershell
.\deploy\windows\Check-LiveNoteApi.ps1
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

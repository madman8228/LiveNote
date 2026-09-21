# LiveNote 发布验收清单

这份清单区分“电脑端已验证”和“必须在手机/正式服务器验证”，避免把本地测试结果误当成生产保证。

## 电脑端已验证

- [x] Vue 3 + TypeScript + Vite 构建通过：`npm run build`
- [x] 录音架构保持单个连续 `MediaRecorder`，逻辑 Chunk 不执行每块 `stop/start`
- [x] IndexedDB `Session → Segment → Chunk` 持久化代码和恢复流程
- [x] Chunk SHA256、幂等上传、断点补传状态和旧 API 兼容检测
- [x] 多 Segment WebM 重建、单 Segment 下载、整场下载
- [x] FFmpeg/FFprobe 音频重建和完整性校验
- [x] 电脑端人工处理任务、结果版本和管理员发布
- [x] 管理台会话标题编辑和用户归属分配
- [x] Session 删除、诊断资料上传、API Key 保护
- [x] SQLite + 音频/结果目录本地备份、完整性校验和安全恢复工具
- [x] PWA manifest、最小 Service Worker 和安装引导
- [x] 后端回归测试：45 项全部通过
- [x] `python -m py_compile server/main.py tools/livenote_worker.py` 通过

## 当前运行环境状态

- `5173`：当前 Vite HTTPS 开发服务，可供手机访问
- `8000`：本地 API；最近的后端改动需要在没有进行中录音时重启后才生效
- `4173`：PC 内容处理台

重启当前 API 后必须执行：

```powershell
.\deploy\windows\Check-LiveNoteApi.ps1
```

通过标准：返回 `storageSchema: 4` 或更高版本，并包含 `capabilities.manualProcessing: true`。

## 手机端必须验证

使用 Android Chrome，访问：

```text
https://电脑局域网IP:5173/
```

按顺序完成：

1. 录音 2 分钟，确认 Chunk 持续增加、时间连续、停止后最后 Chunk 存在。
2. 录音中刷新页面，选择“继续录音”，确认同一 Session 新增 Segment，累计时间不归零。
3. 录音中断开网络，确认录音继续、本地 Chunk 和 Pending 持续增加；恢复网络后只恢复上传，不停止录音。
4. 录音完成后等待 `上传 = 总数`，播放整场并下载整场音频。
5. 在 PC 控制台完成“领取到本机 → 开始处理 → 读取本地 `knowledge.json` → 发布”。
6. 手机刷新会话，确认只显示已发布的知识总结和可切换背景的阅读卡。
7. 在正式 HTTPS 环境测试 PWA 安装、前台长录音和 Wake Lock；锁屏/切后台只记录风险，不宣称 Web 一定能继续录音。

## 仍需外部条件

- 电脑端人工处理仍需要 ChatGPT/Codex 生成结构化 `knowledge.json`；当前不使用本地模型或 OpenAI API。
- 云端部署还需要 Worker 常驻领取任务；本地 PC 模式可以完全通过控制台按钮操作。
- 必须提供正式域名、HTTPS 证书、数据目录权限和异地备份位置，并在正式环境完成一次恢复演练。
- 说话人识别、云对象存储、自动化 AI 调度和生产级外部任务队列仍属于后续范围。

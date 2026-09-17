# LiveNote 发布验收清单

这份清单区分“电脑端已验证”和“必须在手机/正式服务器验证”，避免把本地测试结果误当成生产保证。

## 电脑端已验证

- [x] Vue 3 + TypeScript + Vite 构建通过：`npm run build`
- [x] 录音架构保持单个连续 `MediaRecorder`，逻辑 Chunk 不执行每块 `stop/start`
- [x] IndexedDB `Session → Segment → Chunk` 持久化代码和恢复流程
- [x] Chunk SHA256、幂等上传、断点补传状态和旧 API 兼容检测
- [x] 多 Segment WebM 重建、单 Segment 下载、整场下载
- [x] 本地 FFmpeg 预处理、Whisper ASR、长音频分段和时间轴
- [x] 无 LLM 时明确返回本地抽取式草稿
- [x] OpenAI-compatible LLM 请求、长文本分段合并和 JSON 归一化测试
- [x] Session 删除、诊断资料上传、API Key 保护
- [x] SQLite + 音频/结果目录本地备份、完整性校验和安全恢复工具
- [x] PWA manifest、最小 Service Worker 和安装引导
- [x] 后端回归测试：31 项全部通过
- [x] `python -m compileall -q server`、`python -m pip check` 通过
- [x] npm 高危漏洞检查：0

## 当前运行环境状态

- `5173`：当前 Vite HTTPS 开发服务，可供手机访问
- `8000`：当前仍是旧 API 进程；健康接口缺少 `storageSchema=2` 和 `capabilities`
- `4173`：旧静态页面服务，不作为验收入口

重启当前 API 后必须执行：

```powershell
.\deploy\windows\Check-LiveNoteApi.ps1
```

通过标准：返回 `storageSchema: 2`，并包含 `capabilities`。

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
5. 点击“本地 ASR + 生成草稿/总结”，确认任务经历重建、ASR、报告阶段。
6. 检查逐字稿时间轴、知识结构和可切换背景的阅读卡。
7. 在正式 HTTPS 环境测试 PWA 安装、前台长录音和 Wake Lock；锁屏/切后台只记录风险，不宣称 Web 一定能继续录音。

## 仍需外部条件

- 必须明确授权后重启旧 8000 API。
- 必须配置 `LIVENOTE_LLM_BASE_URL`、`LIVENOTE_LLM_MODEL`，以及远程服务所需的 `LIVENOTE_LLM_API_KEY`，才能得到真正语义总结。
- 必须提供正式域名、HTTPS 证书、数据目录权限和异地备份位置，并在正式环境完成一次恢复演练。
- 说话人识别、账号体系、云对象存储和生产级外部任务队列仍属于后续范围。

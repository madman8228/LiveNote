# LiveNote

当前版本新增：多 Segment 服务器重建使用连续 Opus 重编码；长文本总结采用分段总结后合并；服务器端回归测试位于 `server/tests/`。

LiveNote 是面向 Android Chrome 的直播录音 PWA 原型。本阶段已完成 M0～M3，并实现 M4～M9：连续 MediaRecorder 录音、IndexedDB 本地持久化、Wake Lock、页面生命周期风险记录、SHA256 校验、异步上传队列、FastAPI + SQLite + 本地文件存储，以及本地 FFmpeg/Whisper/结构化报告的后台处理任务。

当前已加入本地 FFmpeg 音频重建接口、本地 Whisper ASR 接口、可选 OpenAI-compatible LLM 语义总结和结构化报告接口，但仍不包含说话人识别、实时字幕、用户账号、云对象存储和复杂 PWA 逻辑。录音、IndexedDB 和上传队列彼此独立：服务器或网络失败不会停止录音，也不会删除本地 Blob。

## 当前完成度与未完成事项

已经可以继续验证的完整链路是：Android Chrome/PWA 录音 → IndexedDB 本地保存 → 断网后继续录音 → 网络恢复补传 → 服务器校验并重建整场音频 → 本地电脑后台 Whisper 转写 → 手机端查看逐字稿和结构化报告草稿。

仍未完成或不能宣称“已验证”的事项：

- 真实 Android Chrome 的 60 分钟以上长录音、刷新恢复、断网恢复和锁屏风险，需要手机端实测；电脑端回归测试不能替代它。
- 当前 8000 端口如果还是旧 API 进程，必须重启后才能使用最新的完整上传收尾和 ASR 处理接口；设置页会显示“API 需重启”。
- 语义总结需要配置 OpenAI-compatible LLM 或本地 Ollama。未配置时返回的是 ASR 加用户标记的本地草稿，不是语义模型总结。
- Speech 与 Raw-ish 的最终选择仍应以相同音源的人工听感和 ASR 结果共同决定；当前默认使用 Speech。
- 说话人识别、用户账号、云对象存储、生产级任务队列、备份恢复和正式域名 HTTPS 尚未实现。

下一轮建议按这个顺序验收：先重启最新 API 并用手机完成一段短录音上传/重建，再做 10 分钟和 60 分钟长录音，最后配置 LLM 做一段真实中文内容的“转写 → 知识点 → 知识结构 → 分享阅读卡”验收。不要在 ASR 质量和手机稳定性尚未通过前扩展说话人识别或复杂部署。

本地处理采用单进程后台任务队列：页面点击“本地 ASR + 生成报告”后立即获得任务编号，电脑后台依次执行音频重建、ASR 和内容分析，页面轮询任务状态并在完成后读取报告。完成后手机端显示可切换背景的总结阅读卡，用户可以直接截图分享；当前不要求服务器生成图片，也不引入 Redis、Celery 或其他复杂基础设施。

页面刷新后，会按 Session 恢复最近一次处理任务的状态；如果任务仍在 ASR 或报告阶段，手机端会继续显示处理中，而不是误显示为未处理。处理完成后优先显示已保存的报告。

开始整场播放、ASR 或报告处理前，页面会先确认本场所有 Chunk 已上传；服务器也会再次校验 Segment 的连续 index 和完成状态，未完成上传的 Session 不会被当作完整音频处理。

## 启动

```bash
npm install
npm run dev
```

另开一个终端启动 API：

```bash
python -m pip install -r server/requirements.txt
python server/main.py
```

FastAPI 默认监听 `http://0.0.0.0:8000`。Vite 开发服务器会把前端 `/api` 请求代理到该地址，手机仍然只需要访问 HTTPS 前端地址。

桌面浏览器可访问 Vite 输出的本机地址。手机访问时，手机和电脑连接同一网络，使用电脑局域网 IP 加端口的 HTTPS 地址，例如 `https://192.168.1.20:5173`。

开发服务器使用 Vite 的开发期自签名证书。手机第一次打开 HTTPS 地址时，Chrome 可能显示证书警告，需要进入“高级”并继续访问该地址；如果手机不允许继续访问，需要改用可信 HTTPS 隧道或把开发证书安装到手机。不要使用局域网 HTTP 地址测试录音：Android Chrome 的 `getUserMedia` 和 Wake Lock 通常要求安全上下文。首次点击“开始录音”时还需要授予麦克风权限。

## M1 测试方法

1. 在手机 B 打开页面，确认 `getUserMedia`、`MediaRecorder` 和至少一种 MIME 类型显示“可用”。
2. 使用手机 A 准备同一段约 5–10 分钟的多人中文讲话内容，并保持手机 B 的位置、距离和音量不变。
3. 在手机 B 点击“开始对比”，页面会按 `Browser Default` → `Speech` → `Raw-ish` 固定推进。
4. 每组只需要在手机 A 开始播放时点击“开始录音”，播放结束时点击“停止”；页面会自动记录实际设置、MIME 类型、时长和文件大小。
5. 三组完成后，在“对比结果”区域直接回放并分别下载三份录音。

页面会自动完成流程管理和资料整理，但不会凭 Blob 大小或音量给出“音质胜负”。清晰度、噪声、泵动和多人讲话分离效果仍需要人工听感判断；未来如果需要 ASR 准确率，再把相同三份文件送入后续 ASR 测试。

日常录音默认使用 `Speech` 配置。`Raw-ish` 仍保留在对比测试中，但关闭自动增益后可能导致输入音量过低，不适合作为 ASR 默认配置。

## M2 + M3 验收

录音开始后，一个 `MediaRecorder` 会持续运行，并用约 10 秒的 `start(timeslice)` 请求周期性 `dataavailable`。每个有效 Blob 会先写入 IndexedDB，写入成功后才更新页面上的已保存 Chunk 计数。Chunk 的 `wallClockMs` 来自事件发生时刻，`elapsedMs` 使用当前页面的 `performance.now()` 单调计时，并在 Session 恢复时从已保存时长继续。10 秒只是浏览器的分片请求值，不代表事件一定严格每 10 秒触发。

页面刷新后，如果发现 `RECORDING`、`PAUSED` 或 `FINALIZING` Session，会显示恢复界面。选择继续会保留原 Session 并创建下一个 Segment；选择结束并保存会保留已有 Chunk，并把未正常结束的 Segment 标记为 `INTERRUPTED`。

“Session → Segment → Chunk”调试区域可以查看层级和每个 Chunk 的 index、大小、相对时间、创建时间、上传状态。单 Segment 点击“按序重组并验证”会从 IndexedDB 读取 Chunk 并通过 MediaSource 顺序追加；多 Segment 的整场播放会优先请求服务器 FFmpeg 重建文件，避免把多个独立 WebM 初始化头直接追加后只播放第一个 Segment。Chunk 不被当成独立音频文件播放。

每个 Segment 还保存 `startElapsedMs`，表示它在整场 Session 单调录音时间轴中的起点。这样刷新、断开后续录时，旧 Segment 的时长不会重新从零估算；旧版本没有该字段的数据会按 0 兼容读取。

录音引擎通过 `RecorderEngine` 接口与 `MediaRecorderEngine` 分离；IndexedDB 通过 `SessionStore`、`SegmentStore`、`ChunkStore`、`MarkerStore` 和 `LifecycleStore` 独立管理。Chunk 写入后计算 SHA256，再由 UploadQueue 异步上传。

录音开始时会尝试申请浏览器的持久化存储权限，并在设置页显示结果；这只是降低 IndexedDB 被系统回收的风险，不能替代服务器上传和定期清理。

### 建议验收顺序

1. 连续录音 10 分钟，确认 Chunk 持续增加且 IndexedDB 字节数增长。
2. 连续录音至少 60 分钟，观察页面是否卡顿、Chunk 是否继续保存。
3. 正常停止，确认最后一个 Chunk 和 Segment 状态为 `COMPLETED`。
4. 录音几分钟后刷新，确认出现恢复界面；继续后创建 Segment #2。
5. 在调试区域对完整 Segment 执行重组并回放，检查分片边界是否有缺音、重复或明显空洞。

录音时页面会显示 Wake Lock、网络、服务器、上传队列和生命周期状态。Wake Lock 失败不会停止录音；页面切到后台、系统释放锁或浏览器不支持时，会显示风险提示。网络或服务器不可用时，页面会暂停上传重试，但 MediaRecorder 和 IndexedDB 仍继续工作，页面会明确显示“录音继续本地保存”。

如果浏览器自身的 MediaRecorder 发生错误，页面会停止当前录音、保存已经成功写入的 Chunk，并把 Session 标记为可恢复状态；网络或上传错误不会触发这条路径，也不会停止录音。

开发期通过电脑局域网地址访问时，如果 Android Chrome 因 Wi‑Fi 切换而重新加载或丢弃页面，原来的 MediaRecorder 无法跨页面继续，这是浏览器生命周期限制，不是上传失败。生产构建会注册一个最小离线 App Shell Service Worker，帮助页面在短暂断网后重新打开；已经保存到 IndexedDB 的 Chunk 不会丢失，但重新加载后仍需要按照恢复流程创建新的 Segment。实际长时间录音应使用稳定的 HTTPS 部署地址，并保持页面前台。

当前已经实现基础 PWA：Manifest、图标、生产环境 Service Worker、standalone 模式检测和 Android Chrome 安装引导。开发服务器虽然使用 HTTPS，但不会注册生产 Service Worker，因此开发地址不作为完整 PWA 安装验收环境；需要先构建并部署到正式 HTTPS 地址。安装后的 PWA 仍受 Android Chrome 页面生命周期限制，不等同于原生后台录音服务。

页面通过三种信号判断安装状态：`display-mode: standalone`、Android Chrome 的 `appinstalled` 事件，以及本地安装记录。Chrome 是否允许直接弹出安装提示由 `beforeinstallprompt` 事件决定，网页不能强制安装；如果没有该事件，设置页会显示手动路径：“Chrome 菜单 → 安装应用 / 添加到主屏幕”。

## 手机端界面结构

手机端现在按三个主入口组织：

- `录音`：只保留开始/停止、时长、音量、本地保存、服务器状态和快速标记。
- `会话`：按标题、日期或 Session ID 搜索，可按录音中、已完成、需恢复、处理中筛选；点击会话可播放整场录音、修改标题、启动 ASR/内容处理，并在“高级诊断”中查看 Segment/Chunk。
- `设置`：集中放置录音配置、音质对比、浏览器能力、实际麦克风设置、连接状态、错误诊断和本地数据清理。

日常使用只需要停留在“录音”页。音质对比、浏览器能力和 Chunk 重组属于诊断工具，不再占用录音主页面的滚动空间。

## 生产部署准备

生产环境建议让前端和 API 使用同一个 HTTPS 域名：前端静态文件由 Nginx 或其他 Web 服务器提供，并把 `/api/` 反向代理到 FastAPI 的 `8000` 端口。这样前端继续使用 `/api/v1`，不需要额外跨域配置。也可以设置 `VITE_API_BASE_URL` 指向独立 API 域名，但必须同时设置 API 的 `LIVENOTE_CORS_ORIGINS`。

构建前端：

```bash
npm run build
```

把 `dist/` 发布到 HTTPS 静态站点。API 使用单 worker 启动，因为当前本地处理队列是单进程队列：

```bash
uvicorn main:app --app-dir server --host 0.0.0.0 --port 8000 --workers 1
```

复制 `.env.example` 和 `server/.env.example` 为部署环境配置，并至少设置真实的 `LIVENOTE_API_KEY`、前端对应的 `VITE_API_KEY`、HTTPS 来源白名单，以及独立且可备份的 `LIVENOTE_DATA_DIR` / `LIVENOTE_DB_PATH`。部署前先访问 `/api/v1/health`，再用一段短录音验证上传、重建和后台处理。

Nginx 和 systemd 示例位于 `deploy/`。它们只提供模板，不包含真实证书、域名和密钥；正式部署仍需先完成 HTTPS 证书、目录权限、SQLite/音频目录备份策略和短录音验收。

遇到无法描述的错误时，页面底部“错误诊断资料”可以下载诊断 JSON，或上传诊断 JSON 加截图/日志。上传内容不包含音频 Blob；返回的 `diagnosticId` 可用于在服务器 `data/diagnostics/` 中定位资料。

## M4 + M5 结构

- `src/lifecycle/WakeLockManager.ts`：申请、释放和重新申请 screen Wake Lock。
- `src/lifecycle/PageLifecycleManager.ts`：记录 visibility、pagehide、pageshow、freeze、resume。
- `src/upload/UploadQueue.ts`：单并发、PENDING/FAILED 重试、网络恢复和状态校准。
- `src/upload/ApiClient.ts`：客户端 API 与 Chunk Header 校验字段。
- `src/storage/sha256.ts`：Web Crypto SHA256。
- `server/main.py`：FastAPI API、SQLite metadata、本地 Chunk 文件保存和幂等 PUT。

UploadQueue 的 Chunk 状态流转为：

```text
PENDING → UPLOADING → UPLOADED
              ↓
            FAILED → 按退避策略重试
```

服务器以 `segmentId + chunkIndex` 为唯一键。重复上传时，SHA256 相同返回成功且 `already_exists=true`；SHA256 不同返回 409，不会创建副本。Session/Segment 收尾会携带预期 Chunk 数量，服务器会校验数量和 Segment index 是否连续；本地还有待上传或失败 Chunk 时不会标记服务器完成，网络恢复后上传队列会再次尝试收尾。

## 本地回归测试

服务器端提供无需真实录音的回归测试：

```bash
python -m unittest discover -s server/tests -p "test_*.py" -v
```

多 Segment 服务器重建会将独立 WebM Segment 重新编码为连续 Opus 文件；长文本总结会自动分段后合并。生产环境请设置 `LIVENOTE_ENV=production`、`LIVENOTE_API_KEY` 和明确的 `LIVENOTE_CORS_ORIGINS`，否则 API 不应对公网开放。更新 `server/` 代码或依赖后必须重启 8000 端口的 API 进程；运行中的 Python 进程不会自动加载这些改动。

前端会检查 `/api/v1/health` 是否返回 `storageSchema` 和处理能力。如果 8000 仍是旧进程，设置页会显示“API 需重启”，录音和本地保存仍可继续，但不会允许调用 ASR/报告处理。

## 测试记录模板

| 项目 | Browser Default | Speech | Raw-ish |
| --- | --- | --- | --- |
| 日期 / 测试编号 |  |  |  |
| Android 机型 / Chrome 版本 |  |  |  |
| 播放手机 / 音箱 / 距离 |  |  |  |
| 页面来源（HTTPS / 其他） |  |  |  |
| `sampleRate` |  |  |  |
| `channelCount` |  |  |  |
| `noiseSuppression` |  |  |  |
| `autoGainControl` |  |  |  |
| `echoCancellation` |  |  |  |
| `deviceId`（可选） |  |  |  |
| 实际 MIME 类型 |  |  |  |
| 录音时长 |  |  |  |
| Blob 大小 |  |  |  |
| 人工听感：清晰度 |  |  |  |
| 人工听感：噪声 / 泵动 |  |  |  |
| 人工听感：音量稳定性 |  |  |  |
| 备注 |  |  |  |

# LiveNote

LiveNote 是面向 Android Chrome 的直播录音 PWA 原型。本阶段已完成 M0～M3，并实现 M4 + M5：连续 MediaRecorder 录音、IndexedDB 本地持久化、Wake Lock、页面生命周期风险记录、SHA256 校验、异步上传队列，以及 FastAPI + SQLite + 本地文件存储服务端。

当前明确不包含 FFmpeg、音频重建、ASR、说话人识别、LLM 总结、实时字幕、用户账号、云对象存储和复杂 PWA 逻辑。录音、IndexedDB 和上传队列彼此独立：服务器或网络失败不会停止录音，也不会删除本地 Blob。

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

## M2 + M3 验收

录音开始后，一个 `MediaRecorder` 会持续运行，并用 `start(30000)` 请求周期性 `dataavailable`。每个有效 Blob 会先写入 IndexedDB，写入成功后才更新页面上的已保存 Chunk 计数。Chunk 的 `wallClockMs` 来自事件发生时刻，`elapsedMs` 使用当前页面的 `performance.now()` 单调计时，并在 Session 恢复时从已保存时长继续。

页面刷新后，如果发现 `RECORDING`、`PAUSED` 或 `FINALIZING` Session，会显示恢复界面。选择继续会保留原 Session 并创建下一个 Segment；选择结束并保存会保留已有 Chunk，并把未正常结束的 Segment 标记为 `INTERRUPTED`。

“Session → Segment → Chunk”调试区域可以查看层级和每个 Chunk 的 index、大小、相对时间、创建时间、上传状态。点击“按序重组并验证”会从 IndexedDB 读取某个 Segment 的所有 Chunk，按 index 通过 MediaSource 顺序追加并检查 index 是否连续。Chunk 不被当成独立音频文件播放。

录音引擎通过 `RecorderEngine` 接口与 `MediaRecorderEngine` 分离；IndexedDB 通过 `SessionStore`、`SegmentStore`、`ChunkStore`、`MarkerStore` 和 `LifecycleStore` 独立管理。Chunk 写入后计算 SHA256，再由 UploadQueue 异步上传。

### 建议验收顺序

1. 连续录音 10 分钟，确认 Chunk 持续增加且 IndexedDB 字节数增长。
2. 连续录音至少 60 分钟，观察页面是否卡顿、Chunk 是否继续保存。
3. 正常停止，确认最后一个 Chunk 和 Segment 状态为 `COMPLETED`。
4. 录音几分钟后刷新，确认出现恢复界面；继续后创建 Segment #2。
5. 在调试区域对完整 Segment 执行重组并回放，检查分片边界是否有缺音、重复或明显空洞。

录音时页面会显示 Wake Lock、服务器、上传队列和生命周期状态。Wake Lock 失败不会停止录音；页面切到后台、系统释放锁或浏览器不支持时，会显示风险提示。服务器不可用时，页面会明确显示“录音仍在本地继续保存”。

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

服务器以 `segmentId + chunkIndex` 为唯一键。重复上传时，SHA256 相同返回成功且 `already_exists=true`；SHA256 不同返回 409，不会创建副本。

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

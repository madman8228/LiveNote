# LiveNote

LiveNote 是面向 Android Chrome 的直播录音 PWA。手机端只负责稳定录音、保存并自动上传；电脑端自动转写，当前 Codex 聊天读取文字并生成知识总结；管理员最后审核并发布知识卡。

当前产品流程使用本机缓存的 Whisper/ASR，不调用 OpenAI API：

```text
手机 PWA 录音
    ↓
手机 IndexedDB 保存
    ↓
自动上传到 LiveNote 服务器
    ↓
连续收到约 2 分钟音频后，服务器开始增量转写并保存中间进度
    ↓
服务器生成待处理任务
    ↓
电脑端后台自动分段转写并保存检查点
    ↓
当前 Codex 对话读取逐字稿并生成总结
    ↓
总结 JSON 回传服务器
    ↓
手机查询并显示知识卡
```

## 用户日常使用

手机用户不需要下载 `.webm`，不需要把音频附加到 ChatGPT，也不需要输入提示词：

1. 打开 LiveNote PWA，点击“开始录音”。
2. 直播结束后点击“停止并保存”。
3. 等待“本场上传”完成；录音会自动进入服务器待处理队列。
4. 稍后打开“会话”并刷新；即使手机本地录音已清理，只要服务器上仍保留该用户的会话，也能看到电脑端返回的知识总结。
5. 打开知识卡阅读页，编辑内容、更换背景并截图发布。

知识卡的编辑内容只保存在当前手机的本机草稿中，不会覆盖服务器原稿；在编辑页可随时选择“恢复服务器原稿”。

录音、上传和电脑端处理相互独立。服务器或网络暂时不可用时，手机仍会继续把已经录到的 Chunk 保存到 IndexedDB，并在恢复后补传。

## 电脑端处理流程

电脑端是内部控制流程，不暴露给手机用户：

1. 手机上传完成后，本机后台自动识别，管理员不需要领取音频。
2. 在当前 Codex 对话中说“总结这条录音”，读取已保存逐字稿并生成结构化总结。
3. 总结回传后进入“待发布”，管理员查看草稿和原录音，确认后发布；手机下一次刷新或自动轮询时查看结果。

任务页会每 15 秒自动刷新任务状态；本机转写在后台进行，管理员只需要审核和发布。历史任务和会话超过首屏数量时，可通过页面底部“加载更多”继续查看。

当前本地 PC 服务器启动时会自动运行转写器：打开 `http://127.0.0.1:4173/?mode=control` 只需查看状态、审核和发布，日常不需要打开命令行。
如果服务器在云端，仍可在“设置”中填写 `LIVENOTE_WORKER_TOKEN`，让异机 Worker 领取并下载音频；本机模式不显示领取操作。Chrome 不支持目录选择时会退回到浏览器下载。

命令行桥接工具仍保留，适合异机 Worker；本机自动转写使用：

```bash
python tools/livenote_worker.py list
python tools/livenote_worker.py pull --limit 3
python tools/livenote_worker.py watch --limit 3
python tools/livenote_transcriber.py watch
```

Codex 需要总结时，先读取任务逐字稿：

```bash
python tools/livenote_transcriber.py summary-prepare task-xxxx
```

`knowledge.json` 的最小格式是：

```json
{
  "version": 1,
  "result": {
    "title": "主题",
    "overview": "核心结论",
    "knowledgeStructure": [{"title": "知识结构", "points": ["知识点"]}],
    "keyPoints": ["关键知识点"],
    "questions": [{"question": "问题", "answer": "回答"}],
    "actionItems": ["行动建议"]
  }
}
```

Worker 若启用了鉴权，需要设置 `LIVENOTE_WORKER_TOKEN`；PC 管理控制台首次打开时直接设置管理员账号和密码，之后用账号密码登录，登录会话只保存在当前浏览器中。生产部署也可以通过 `LIVENOTE_ADMIN_USERNAME` 和 `LIVENOTE_ADMIN_PASSWORD` 预先初始化。旧部署仍可暂时使用 `LIVENOTE_ADMIN_TOKEN`，迁移后可以移除。手机首次使用时，由管理员创建用户和一次性配对码，手机在“设置 → 手机身份”绑定一次，之后录音、上传和结果查询都自动带上设备身份。默认要求先完成配对才能开始新的录音，避免产生无法确认归属的 Session；历史未绑定 Session 应由管理员备份后清理。

这套流程使用的是 LiveNote 自有服务器接口和本机 Whisper，不是 OpenAI API。当前总结仍由一次 Codex 聊天发起，不承诺无人值守的语义总结；之后可在不改变手机端流程的前提下替换总结服务。

## 当前已完成

- Vue 3 + Vite + TypeScript 项目骨架。
- Android Chrome 麦克风能力检测和三种 Audio Profile。
- 一个连续 MediaRecorder；使用 `timeslice` 产生逻辑 Chunk，不对每个 Chunk stop/start。
- Session → Segment → Chunk 数据关系。
- IndexedDB 本地持久化、刷新恢复、Wake Lock、页面生命周期记录。
- 异步上传队列、Chunk 校验、断网后补传。
- FastAPI + SQLite + 本地文件存储。
- 整场音频服务器重组和播放验证。
- `processing_tasks` 任务表、任务领取/状态更新/结果回传/手机结果查询接口。
- PC 内容处理台：自动转写状态、草稿查看、原录音试听、发布和失败重试。
- 管理台会话管理：搜索、修改标题、分配所属用户、结束遗留录音和删除会话。
- 用户/设备配对、设备身份鉴权、服务器会话同步和已发布结果缓存。
- 手机知识卡阅读页：更换背景、截图使用，以及只保存在本机的二次编辑草稿。
- 手机端移除“下载音频给 ChatGPT”等错误入口。

## 当前还在建设

- 一次完整的真实闭环验收：手机上传 → 本机自动转写 → Codex 总结回传 → 发布 → 手机查看。
- 真实 90 分钟自然内容的质量验收；当前已完成工程长度方案，尚无该项授权样本。
- 手机端总结草稿的多用户正式隔离验收。
- Android Chrome 10 分钟/60 分钟长录音、刷新、断网、切后台和 Wake Lock 的最终验收。
- 云服务器 HTTPS、正式密钥、备份恢复、监控和多用户生产隔离。

## 启动

安装前端依赖并启动开发页：

```bash
npm install
npm run dev
```

另开一个终端启动本地 API：

```bash
python -m pip install -r server/requirements.txt
$env:LIVENOTE_ADMIN_USERNAME='admin'
$env:LIVENOTE_ADMIN_PASSWORD='replace-with-your-admin-password'
$env:LIVENOTE_WORKER_TOKEN='livenote-local-worker'
python server/main.py
```

FastAPI 默认监听 `http://0.0.0.0:8000`。Vite 开发服务器会把前端 `/api` 请求代理到该地址。手机和电脑连接同一 Wi-Fi 后，使用电脑局域网 IP 加端口访问，例如：

```text
https://192.168.1.20:5173
```

电脑上的管理控制台也可以使用不需要麦克风权限的 HTTP 地址打开：

```text
http://127.0.0.1:4173/?mode=control
```

它由 `vite.control.config.ts` 提供；手机录音页面默认使用 HTTPS 的 5173 端口。

正式 PWA 需要 HTTPS。开发环境的自签名证书可能需要在手机浏览器中手动允许继续访问。

## 服务器任务接口

当前已提供：

- `GET /api/v1/tasks?status=READY`：查询待处理任务（异机兼容入口）。
- `POST /api/v1/tasks/{task_id}/claim`：电脑端领取任务。
- `POST /api/v1/tasks/{task_id}/status`：更新处理状态。
- `GET /api/v1/tasks/{task_id}/audio`：电脑端取得整场音频。
- `POST /api/v1/tasks/{task_id}/result`：回传结构化总结草稿，进入 `REVIEW`。
- `GET /api/v1/tasks/{task_id}/summary-input`：读取指定转写运行的文字和版本信息。
- `POST /api/v1/tasks/{task_id}/summary-result`：提交基于指定转写版本生成的 Codex 总结，进入 `REVIEW`。
- `POST /api/v1/admin/tasks/{task_id}/result-local`：从本机 `worker-inbox/<task-id>/knowledge.json` 读取结果并进入 `REVIEW`。
- `POST /api/v1/admin/tasks/{task_id}/publish`：管理员发布指定草稿。
- `GET /api/v1/sessions/{session_id}/result`：手机查询总结结果。

服务器源码中仍保留旧的报告模块用于兼容；新的正式流程使用可恢复的本机 Whisper 分段转写。模型不会由服务静默下载，旧部署脚本仍可引用 `server/requirements-legacy-ai.txt`。

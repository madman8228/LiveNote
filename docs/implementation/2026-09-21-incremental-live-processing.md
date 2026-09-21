# LiveNote 实时上传增量识别与最终合并交接方案

- Status: implementation-complete; external validation pending
- Updated: 2026-09-21
- Branch/worktree: master / D:\06-project\LiveNote
- Base commit and relevant uncommitted changes: 当前工作树已有大量前端、服务端、部署、诊断和文档修改，并存在多个未跟踪目录；执行时必须保留，不能 reset、checkout 覆盖或清理。
- Planner: Astra planning phase
- Executor: GPT-5.6 Luna
- Related plan: `docs/implementation/2026-09-19-long-recording-processing.md` 是旧的完整上传处理方案，本方案只新增实时上传期间的增量识别，不替换现有最终处理与总结流程。

## Objective

用户录音仍按手机端 30 秒 Chunk 自动保存和上传。服务器收到连续的一批 Chunk 后即可开始建立处理窗口、转写并保存部分逐字稿；后续 Chunk 到达后继续处理，录音结束再把全部窗口按时间顺序去重、校正边界并生成唯一的最终逐字稿，之后沿用现有总结/审核/发布链路。

用户不需要手动拼文件、选择中间结果、提交 JSON 或等待整小时录音结束才看到第一批识别结果。窗口处理必须可恢复、幂等、可观测，失败只影响当前窗口并可重试；最终结果不能被不完整的临时结果覆盖。

## Current state and evidence

- `src/App.vue` 当前使用 `CHUNK_TIMESLICE_MS = 30_000`，`MediaRecorderEngine` 持续录音并把 `dataavailable` 产生的 Blob 依次写入 IndexedDB。
- `src/audio/MediaRecorderEngine.ts` 的每次 `start()` 获取一条 MediaStream；`stop()` 会结束当前 recorder，`dispose()` 会停止 tracks。当前 Segment 主要对应开始/暂停/恢复或最终停止，不是每 30 秒一个独立媒体文件。
- `src/upload/UploadQueue.ts` 会先上传 Chunk，只有 Segment 的全部 Chunk 上传后才调用 `completeSegment`，只有整场 Chunk 都上传后才调用 `completeSession`。
- `server/main.py` 的 Chunk 接口会原子保存 `sessions/<session>/segment_<index>/chunks/<index>.bin`，上传成功后登记并唤醒增量 Worker；`complete_session` 仍只在完整性校验通过后创建最终 `processing_tasks`。
- `server/reconstruction.py` 当前按字节顺序拼接 Chunk，再用 FFmpeg 校验/重封装。原始 MediaRecorder Chunk 不能假定为各自独立可解码媒体，直接对单个 `.bin` 调 FFmpeg 是不稳定的。
- `server/processing_pipeline.py` 当前只处理已完成 Session，先重建整场音频，再以 300 秒 ASR 窗口转写并持久化 `processing_runs`/`processing_parts`。
- `server/asr.py` 已有 `transcribe_range`，但其输入需要能够被 FFmpeg 稳定读取的音频文件；当前 ASR 读取整场或已重建音频，不适合未经准备就读取任意上传 Chunk。
- `server/jobs.py` 的线程池任务仍负责整场最终处理，不能直接作为实时入口；管理员自动处理路径现在会在增量窗口未排空时持久化等待状态，由增量 Worker 排空后自动续跑。
- 工作树是脏的，已有播放诊断、播放状态、任务处理等用户修改；本方案不重写或回滚这些内容。

## Assumptions and decisions

1. 保持当前 30 秒上传间隔和音频码率，不通过降低码率换实时性。
2. 不把每个 `.bin` 当作独立音频。第一版使用“连续 Chunk 前缀暂存文件 + 窗口级转写”：只在 Segment 的 Chunk index 连续且达到批量阈值时推进，暂存文件每次以原子方式更新。
3. 第一版默认每 4 个 Chunk（约 120 秒）形成一个可提交处理窗口；允许通过环境变量调整为 2–4 个 Chunk，但不得小于 2 个。窗口保留 2–3 秒边界重叠，最终合并时按核心范围去重。
4. 增量窗口是临时中间产物，不改变 `processing_tasks` 的最终状态语义。录音期间任务显示为“实时识别中/已识别至 xx:xx”，但不得显示已完成或可发布。
5. 增量处理使用与现有最终处理相同的 ASR 模型、语言和质量校验配置；不新增未经授权的外部 ASR 服务，不复制录音到其他目录作为用户操作步骤。
6. 服务重启、Worker 崩溃或网络中断后，依据数据库 checkpoint 和已上传 Chunk 重新计算下一个连续窗口；不依赖内存队列，也不重复提交已完成窗口。
7. 录音结束后的最终处理仍以完整 Segment/Session 为唯一事实来源，重新校验完整性并执行最终整场合并。临时逐字稿只作为进度和恢复依据，不能直接变成发布版本。
8. 第一版不在录音过程中调用语义总结模型。录音期间只提供窗口转写和可选的本地预览；最终整场逐字稿稳定后，沿用现有总结流程生成主题、知识点、问答和行动建议。
9. 若增长中的 WebM 前缀无法被 FFmpeg 稳定读取，窗口保持 `WAITING_FOR_DECODABLE_PREFIX` 并记录明确原因；不能用猜测的时长或空结果推进。需要改为客户端独立 Segment 轮换时，另开方案评审，不在本轮隐式改变录音引擎。

## Scope

- 服务端为录音中的 Session 创建/维护增量处理状态。
- Chunk 上传成功后登记可处理水位；连续前缀达到阈值后由单 Worker 处理窗口。
- 原子维护每个 Segment 的前缀暂存文件，窗口转写结果、输入哈希、范围和状态持久化。
- 增量逐字稿读取接口与现有任务/历史页状态接入，展示已经识别的时间范围和最近错误。
- Session 完成后复用窗口结果或重新执行最终完整处理，合并重叠文本并写入现有 `transcript.json`；最终版本带完整源哈希和生成代次。
- 失败、重试、重启恢复、缺片等待、重复 Chunk、旧 Worker 覆盖保护和删除竞争测试。
- 必要的 README、AGENTS/产品流程说明及运行参数文档更新。

## Out of scope

- 不改变录音码率、30 秒 Chunk 间隔、主要录音 UI 或播放器实现。
- 不让手机端逐个选择/提交临时结果，不要求用户手工拼接或上传录音文件。
- 不把临时窗口结果直接发布，不自动发布知识卡片。
- 不在本轮引入实时流式 Whisper、常驻 FFmpeg 解码管道、外部云 ASR 或新的供应商。
- 不修改既有播放诊断方案，不处理其他会话或跨用户数据。
- 不部署、重启生产服务、不提交或推送 Git。

## Contracts and data changes

### 增量状态

在 `server/migrations.py` 增加幂等表，字段名称可按现有命名风格实现，但必须保留以下语义：

- `live_processing_runs`：`id`、`session_id`、`model`、`language`、`pipeline_version`、`status`、`claimed_by`、`lease_expires_at`、`last_contiguous_chunk`、`processed_until_ms`、`source_prefix_hash`、`heartbeat_at`、`error_code`、`error_message`、`created_at`、`updated_at`。
- `live_processing_windows`：`id`、`run_id`、`session_id`、`segment_id`、`window_index`、`model`、`language`、`pipeline_version`、`input_start_chunk`、`input_end_chunk`、`start_ms`、`end_ms`、`core_start_ms`、`core_end_ms`、`input_hash`、`status`、`attempts`、`transcript_json`、`artifact_path`、`artifact_hash`、`started_at`、`completed_at`、`error_code`、`error_message`、`prepare_duration_ms`、`asr_duration_ms`、`peak_staging_bytes`。唯一键为 `run_id/segment_id/window_index`。

状态至少包括：`WAITING_FOR_CHUNKS`、`READY`、`CLAIMED`、`DECODING`、`TRANSCRIBING`、`COMPLETED`、`WAITING_FOR_DECODABLE_PREFIX`、`FAILED`、`FINALIZED`。只有校验通过的 `COMPLETED` 窗口计入已识别水位。

每个窗口必须记录：实际输入 Chunk 范围、非重叠核心时间范围、输入前缀哈希、ASR 模型/语言/版本、生成时间、重试次数和结果文件哈希。结果 JSON 使用现有 `segments[{startMs,endMs,text}]` 结构，时间统一为整场毫秒。

### 上传与调度

- `upload_chunk` 在短事务提交 Chunk 后，只登记/唤醒增量队列；不在请求线程内执行 FFmpeg 或 Whisper，也不持有数据库锁等待识别。
- 调度依据数据库中每个 Segment 的连续 Chunk 前缀计算水位，不能只按总数量判断，不能跳过缺失 index。
- 同一会话同一时间只允许一个增量 Worker；租约过期可恢复，旧 Worker 的结果提交必须被 generation/lease 校验拒绝。
- `complete_segment`/`complete_session` 仍是最终完整性证明；收到最终收尾请求后先排空可处理窗口，再进入最终处理，不能将尚未上传的 Chunk 视为静音。

### 音频暂存与窗口处理

- 暂存目录限定在 `DATA_DIR/processed/live/<session_id>/segment_<index>/`，使用唯一临时文件和原子替换；禁止把原始录音正文写入日志。
- 只追加已经连续且校验通过的 Chunk。追加前核对数据库中的 size、SHA-256 和文件路径；缺片时不推进。
- 追加后的前缀必须通过 FFmpeg 可读性检查，并记录实际可解码范围。无法读到稳定范围时进入等待/重试，不生成空转写。
- 窗口使用 `transcribe_range` 或等价的有界音频准备逻辑；结果只保留核心区间，重叠区用于边界上下文，不重复计入最终文本。
- 处理完成后原子写入窗口 JSON，随后在同一数据库事务中更新窗口状态和水位；恢复时以数据库状态和文件哈希为准。

### 最终合并

- 最终流程重新确认所有 Segment 已完成、Chunk index 连续、Session 时长与最后 Chunk 水位一致。
- 优先读取本代次所有已完成窗口；对缺失或源哈希不匹配窗口补处理。最终仍需要整场源哈希，用于防止旧临时结果覆盖新的完整录音。
- 按 `startMs/endMs` 排序，使用核心范围裁剪重叠；相邻重复文本要有确定规则和测试，不能用简单字符串全局替换误删正常重复句。
- 输出现有 `processed/sessions/<session_id>/transcript.json`，保留 `runId`、`generation`、`sourceHash`、窗口参数和 `liveProcessing` 元数据。只有最终转写完成后才允许进入现有总结/审核状态。

### API 与界面

- 增加面向所属用户/管理员的只读状态字段：`liveProcessing.status`、`processedDurationMs`、`totalDurationMs`、`completedWindows`、`totalWindows`、`lastError`、`canRetry`。
- 增加“读取当前增量逐字稿”接口，只返回指定 Session 的已提交窗口合并文本；未完成部分明确标为“尚未识别”，不伪造完整结果。
- 历史 Item 只有一个主状态；录音中显示真实进度，网络断开显示上传/等待原因，失败显示重试，完成后回到现有最终处理状态。不要把内部表名、租约、路径或 JSON 选择暴露给普通用户。
- 页面刷新只读取当前会话状态，不枚举其他用户或其他会话的录音正文。

## Implementation steps

- [x] 0. 重新核对 `AGENTS.md`、`PRODUCT.md`、本方案和当前 diff；建立隔离的短录音基线，记录现有完整上传/最终 ASR 行为，不清理用户改动。
- [x] 1. 新增迁移与 store 层：表、索引、租约、幂等状态推进、输入哈希和旧代次保护；迁移前沿用现有备份机制。
- [x] 2. 新增 `server/live_processing.py`：连续水位计算、前缀暂存、窗口生成、ASR 调用、窗口 checkpoint、重试与恢复；已用隔离测试覆盖边界。
- [x] 3. 在服务 lifespan 启动单个有限 Worker；Chunk 上传成功只唤醒 Worker；API 退出时停止/释放线程。服务仍保持单进程队列约束。
- [x] 4. 接入 `complete_session` 的增量运行登记；最终完整任务仍使用完整源校验，暂存逐字稿不覆盖最终 `transcript.json`。
- [x] 5. 增加状态/逐字稿 API，并在 `src/upload/ApiClient.ts`、`src/App.vue` 接入数据模型；录音主路径不增加手工按钮。
- [x] 6. 增加最终窗口合并、重叠重复文本去重和完整处理完成后的 `FINALIZED` 隔离；临时结果不进入发布。
- [x] 7. 已更新 README、server README、AGENTS.md、PRODUCT.md，说明参数、状态、失败恢复和“临时结果不可发布”边界。
- [x] 8. 完成单元、接口、文件恢复、并发/重试、真实短录音端到端验证；服务端真实短录音已拿到窗口提前识别证据。Android Chrome 真机验收单独保留为外部项，且按用户要求不启动 Android。

## Validation

- [x] `npm run build`。
- [x] `python -m unittest discover -s server/tests`，并新增增量处理测试。
- [x] `git diff --check`，输出仅有工作树既有的换行转换提示，没有内容错误。
- [x] 迁移幂等、唯一窗口、重复 Chunk、缺片不推进、乱序到达后补齐自动推进、重复唤醒不重复 ASR。
- [x] Worker 租约、失败重试、输入哈希、旧 Worker 失去租约后的提交拒绝和 `FINALIZED` 状态保护；补充了新 Worker 扫描并接管过期 `CLAIMED` 运行的重启回归；真实生产服务重启/FFmpeg 超时仍需部署环境验证。
- [x] 前缀暂存文件原子替换；窗口结果和输入哈希持久化，源变化不会复用旧窗口结果。
- [x] 连续 2、4、5 个 30 秒 Chunk 的窗口边界；最后不足整批的尾窗口只在 Session 收尾后处理。
- [x] 重叠区相同中文句子只出现一次；最终合并保留不同文本，时间戳按整场毫秒排序。
- [x] 自动化回归覆盖录音仍为 `RECORDING` 时的首窗处理、过期租约接管、删除后增量目录清理，以及不同用户读取增量状态的归属隔离。
- [x] 服务端真实短录音：使用真实语音 WebM 拆分的 4 个 Chunk，在 Session 仍为 `RECORDING` 时运行真实 Whisper；首窗完成、`transcript-live.json` 生成、Session 未被提前收尾且未创建最终任务。
- [ ] Android Chrome 真机短录音：需要真实设备上传第 4 个 Chunk 后确认手机端仍录音时看到首批识别；当前环境没有可连接的 Android Chrome 设备，不能用桌面合成音频替代。
- [x] 浏览器级离线/恢复状态：离线时显示“录音继续、本地保存、上传暂停”，恢复在线后状态回到在线；使用隔离临时服务和浏览器配对身份验证。
- [x] 桌面 Chromium 真实 MediaRecorder + IndexedDB 离线补传：隔离临时 API 下，断网录音期间页面显示 `上传 0 / 2`，恢复网络后自动推进到 `3 / 3`，停止录音后为 `4 / 4`；服务端最终确认同一 Session `COMPLETED`、收到 4 个 Chunk、任务状态为 `READY`。
- [x] 删除、用户归属、播放诊断和其他会话隔离回归；测试只使用明确指定的测试 Session。
- [x] 记录处理窗口耗时、ASR 耗时、暂存磁盘峰值、重试次数和识别水位；不承诺固定剩余时间。

## Acceptance criteria

- [x] 30 秒 Chunk 到达后，达到默认 4 个 Chunk 阈值即可自动开始处理，不必等整场录音结束；服务端真实 API E2E 已证明 Session 仍为 `RECORDING` 时首窗完成。
- [x] 缺片、乱序、重复上传、过期租约/Worker 恢复和损坏 Chunk 都有持久化状态与回归覆盖；真实生产服务重启仍需部署环境验证。
- [x] 最终逐字稿只由完整上传源生成，窗口中间结果不会覆盖已发布结果，也不会提前进入发布状态。
- [x] 用户可在历史页看到真实上传/识别水位，失败状态提供重试入口，不需要管理目录、文件或任务内部状态。
- [x] 现有最终 ASR、总结、审核、发布、播放和手机结果同步回归通过：真实服务端短录音覆盖最终 ASR；新增最终逐字稿→总结提交→审核发布→手机读取闭环；播放回归套件全部通过。
- [x] 服务端真实端到端证据证明第一个窗口在 Session 结束前完成；Android Chrome 真机体验仍保留为单独外部验收项。

## Risks and rollback

- 当前 MediaRecorder Chunk 可能依赖前序初始化和最终容器信息；若增长前缀无法稳定被 FFmpeg 解码，第一版必须停在等待并报告，不以空结果冒充成功。后续可单独评审“客户端自动轮换独立 Segment”或常驻解码器。
- 每次窗口可能需要读取增长中的前缀，长录音存在重复解码成本；必须测量后再决定是否引入常驻解码器或调整窗口大小，不能未经数据把复杂度推入生产。
- 增量 ASR 结果可能因边界上下文发生修正；最终合并必须能替换临时窗口，不把中间文本当事实版本。
- ASR 处理慢于上传时，队列必须按会话串行并显示积压，不无限创建线程或占满磁盘。
- 回退只关闭增量 Worker/入口并保留已上传 Chunk、窗口 checkpoint 和最终处理能力；不删除录音、不覆盖播放诊断、不用旧数据库备份覆盖新数据。

## Execution notes

- 2026-09-21：完成现状核对并建立本交接方案。确认当前客户端 30 秒 Chunk、服务端仅在完整 Session 收尾后创建处理任务、原始 Chunk 不保证独立可解码；本轮尚未修改产品代码、未启动 Worker、未部署。
- 2026-09-21：Luna 执行阶段新增 `server/live_processing.py`、增量迁移表、API lifespan Worker、Chunk 唤醒、所属会话状态/增量逐字稿/失败重试接口、手机端状态展示和隔离测试。窗口默认 4 个 30 秒 Chunk，尾窗口在 Session 完成后处理；最终完整处理完成后将增量运行标记为 `FINALIZED`。服务端全量回归现为 76 项通过，`npm run build` 通过；存储 schema 已升至 9，窗口逐条记录模型、语言、pipeline 版本及处理指标；尚未用真实手机音频证明增长中的 WebM 前缀可被 FFmpeg 稳定读取，也未部署或重启生产服务。
- 2026-09-21：使用 FFmpeg 生成 12 秒 WebM 并按字节切成 4 个前缀做增长实验：累计 1/2/3 个前缀分别可解码约 3.21/6.15/9.09 秒，FFmpeg 仅报告“文件提前结束”；4 个前缀完整解码 12 秒。随后用缓存的 `tiny` Whisper 实际调用 `transcribe_range` 读取增长前缀，返回有界结果且未抛出格式错误；合成内容是纯正弦波，所以文字为空，这是符合预期的质量结果。该证据支持“增长前缀技术可读”，但不是 Android Chrome MediaRecorder 真机证据，仍保留真机验收项。
- 2026-09-21：进一步用 Windows Speech 生成英文语音、编码为 WebM、截取半个增长前缀，并用缓存的 `tiny` Whisper 实际识别；`transcribe_range` 返回 1 段非空文字（`This is a live building.`）。这证明本地 FFmpeg→ASR 链可处理非完整增长前缀，但仍不是 Android Chrome MediaRecorder 真机证据。
- 2026-09-21：在隔离数据目录中完成一次真实服务端增量 E2E：4 个语音 WebM Chunk 入库后直接运行增量 Worker，状态变为 `COMPLETED`，`processedDurationMs=6000`，并生成 `processed/live/<session>/.../window-00000.json` 与 `transcript-live.json`；随后运行现有最终处理流程，最终任务变为 `TRANSCRIBED`，最终逐字稿写入成功，增量状态变为 `FINALIZED`，确认旧 Worker 不能覆盖最终结果。该实验使用本地缓存 Whisper `tiny` 模型，证明服务端窗口、持久化、最终隔离链路可运行，但仍不是录音尚未结束时的 Android Chrome 真机证据。
- 2026-09-21：补齐增量 checkpoint 的 `last_contiguous_chunk` 更新，并新增过期租约接管、录音中首窗、删除增量目录、跨用户状态读取隔离回归；窗口表新增 `model`、`language`、`pipeline_version` 字段并提供 schema 7→8 幂等迁移。全量测试通过 66 项，前端构建和差异检查通过。
- 2026-09-21：在隔离目录完成“录音仍在进行中”的真实服务端短录音验证：通过 FastAPI 上传接口写入 4 个真实语音 WebM Chunk，Session 保持 `RECORDING`，实际 Whisper 首窗处理成功，`completedWindows=1`、`processedDurationMs=6000`、`live-transcript` 接口返回 200 且有 1 段非空文字、最终 `processing_tasks` 数量仍为 0。该证据闭合了服务端提前识别链路，但不替代 Android Chrome 真机验收。
- 2026-09-21：检查本机 Android 调试设备列表，当前无可连接设备；因此保留 Android Chrome 真机验收为外部条件，不虚报完成。
- 2026-09-21：进一步检查本机已安装的 MuMu Android 配置：实例 0 未启动，实例 1 尝试启动后退出，`adb devices -l` 始终为空；当前没有可用于 Chrome 真机录音验收的 Android 设备或稳定模拟器，因此不以桌面 Chromium 代替。
- 2026-09-21：用户明确要求不要启动 Android；确认两个 MuMu 实例当前均为停止状态。后续不再启动 Android 或模拟器，Android 真机项保留为未执行的外部验收，不影响桌面端和服务端实现继续推进。
- 2026-09-21：增量窗口新增 `prepare_duration_ms`、`asr_duration_ms`、`peak_staging_bytes` 指标，状态接口同时返回最近窗口耗时、ASR 耗时、暂存峰值和连续 Chunk 水位；schema 8→9 迁移及回归测试通过。随后补充多 Segment 窗口总数计算和 `FINALIZED` 迟到 Worker 拒绝测试；服务端全量测试达到 68 项。
- 2026-09-21：进一步加入租约归属校验和续租：窗口提交前必须仍由当前 Worker 持有未过期租约，租约转移后旧 Worker 的 ASR 结果会被丢弃；新增并发回归测试。不可解码窗口在输入未变化时不再忙循环，只有新 Chunk 或明确点击重试才会再次送入 ASR；错误状态不会被收尾逻辑覆盖，显式重试会清理旧租约。
- 2026-09-21：发现并修复 Worker 轮询遗漏过期 `CLAIMED` 运行的问题：底层接管逻辑虽存在，但崩溃后旧租约记录不会被扫描。轮询现将过期 `CLAIMED` 纳入 durable queue；新增 Worker 启动后接管过期租约回归测试。随后新增最终逐字稿→总结提交→审核发布→手机结果读取闭环测试，确认最终结果不会被增量临时稿覆盖。
- 2026-09-21：运行 `npm run test:playback`，播放运行时诊断、播放可用性、播放 UI 状态、进度条回退、Build ID 配置和诊断上传测试全部通过；与服务端会话删除/用户归属测试共同覆盖指定会话隔离。
- 2026-09-21：使用隔离的临时 API 和真实配对流程完成浏览器级离线恢复烟测：绑定临时用户后打开设置页，触发 `offline` 事件看到“服务器离线”“离线（录音继续）”“网络已断开，上传暂停；录音继续本地保存”，触发 `online` 后恢复“服务器在线”。未使用真实录音内容，故不把它当作 IndexedDB Chunk 补传验收。
- 2026-09-21：在同一隔离临时 API 上使用桌面 Chromium 的真实 `MediaRecorder` 录音流完成 IndexedDB 断网补传 E2E：断网等待 32 秒期间本地队列累计 2 个 Chunk 且上传数为 0；恢复网络 8 秒内自动补传至 3 / 3；停止录音后补传至 4 / 4。服务端查询确认该 Session `COMPLETED`、`chunkCount=4`、`taskStatus=READY`。这闭合了浏览器端录音队列恢复链路，但不替代 Android Chrome 真机验收。
- 2026-09-21：修复 Worker 重启接管过期 `CLAIMED` 运行后，服务端全量回归 72 项通过；`npm run build`、`npm run test:playback` 和 `git diff --check` 均通过（后者仅报告工作树既有的 LF/CRLF 转换提示）。
- 2026-09-21：只读检查当前 8000 端口运行服务：健康接口返回 `storageSchema=6`，而本工作树已迁移到 schema 9；现有进程未重启、未替换，生产验证仍待明确维护窗口和部署动作。
- 2026-09-21：修复前端兼容性门槛过低的问题：原先 `storageSchema >= 4` 会把当前 8000 端口的旧 schema 6 服务误判为可用；现在要求 schema 9，旧服务会明确进入“需要重启”状态并暂停上传。新增兼容性回归测试通过，播放回归套件随之通过。
- 2026-09-21：并发审计发现两个实现偏差并修复：Worker 处理完一个窗口后若还有已到达窗口会继续排空，不再等待下一次上传；最终处理 Worker 领取任务前会等待当前 Session 的可处理增量窗口排空，避免最终 ASR 与临时窗口争抢，显式关闭增量功能时不启用该门槛。新增多窗口排空和最终任务领取门控回归；全量服务端测试达到 76 项，前端构建和播放/兼容性回归均通过。
- 2026-09-21：继续审计管理员自动处理路径，发现其使用 `server/jobs.py` 的独立线程池，之前未受实时窗口排空门控保护，存在提前整段处理的竞态。现已补齐：自动处理请求在实时窗口未排空时持久化为 `PROCESSING/LIVE_DRAIN`，实时 Worker 排空后自动续跑；新增接口与自动恢复回归测试。Android 仍按用户要求不启动。
- 当前实现阶段的代码与本地自动化验证已完成；剩余验收集中在真实 Android Chrome 短录音、生产重启/故障恢复和线上运行指标采集。未部署、未重启生产服务、未提交或推送 Git。

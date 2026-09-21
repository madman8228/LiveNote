# Whisper 自动转写与 Codex 文字总结实施方案

- Status: in-progress
- Updated: 2026-09-20
- Branch/worktree: master / D:\06-project\LiveNote
- Base commit: 5d0d5dbae0e98d124b464c56e1c6bb16fdeeefff
- Relevant uncommitted changes: server/main.py、jobs.py、tests、src/App.vue、ApiClient.ts、部署文档等已有大量修改；AGENTS.md、PRODUCT.md、server/migrations.py、auth.py、src/control/、tools/、docs/ 等未跟踪。保留全部既有工作，不从 HEAD 覆盖。
- Planner: GPT-6 Astra
- Executor: GPT-5.6 Luna
- Supersedes: 2026-09-19-long-recording-processing.md 中的直接音频输入能力闸门及自动调用聊天假设。执行本文件，不执行旧计划。

## Objective

交付同机可用的一条完整流程：手机完整上传后后台自动转写并保存文字；用户在当前 Codex 聊天说“总结这条录音”，代理自动读取转写、生成知识总结并回传草稿；管理员试听、审核并显式发布，手机读取已发布结果。

本期接受一次聊天发起总结。不得称为无人值守全自动总结；不得要求复制提示词、搬文件或选择 JSON。先用现有两份短录音证明真实转写和总结，再完成分段检查点、恢复与长录音测试。真实 90 分钟素材未到位时，如实记录该项未验收，不妨碍完成其余实现。

## Current state and evidence

- PRODUCT.md 仍禁止本地 Whisper/ASR，与用户最新认可方向矛盾。实施时同步为允许内部本地转写，保持不调用 OpenAI API。
- server/asr.py 已有 Whisper 加载、CPU/CUDA 选择、300 秒分段及 1 秒重叠，但分段结果只在内存；语言默认 zh，不适合直接用于英语样本；音量硬阈值可能拒绝可懂录音。
- jobs.py 使用 API 内线程池，重启重新运行整个作业；RUNNING 文件不证明进程活着。_knowledge_result_from_report 会将抽取式降级当草稿，_store_task_report 直接进入 REVIEW。
- main.py 的启动恢复、admin_list_tasks 内孤儿恢复、auto-process 均可能启动旧作业；正常读取任务列表不应触发识别。
- tools/livenote_worker.py 已具备拉取、鉴权、续租、结果回传；现有上传结果依赖租约，不能直接把失效 lease.json 当可用凭证。
- ControlConsole.vue 在浏览器推进 LOCAL_READY 并显示“领取并自动处理”；未区分转写完成、待总结。
- 已知样本：D:\英语资料\第一册\新概念第一册双数课的mp3\新概念英语1 双课（2-78听力）\新概念英语1-Lesson2.mp3（135.55 秒）；worker-inbox/task-13d27b9e-ebac-453b-a79d-acf5109716e2/audio.webm（约 302 秒，对应 session-5b1bde3a-798f-4a85-be6c-388953ed9302）。执行前确认仍存在。
- 先前的任务文件标 ASR/medium，但没有完成转写证据；当前硬件、活跃进程及模型缓存须在执行时核实。

## Assumptions and decisions

1. 用户最新讨论和本次实施方案请求确定本地 Whisper 转写路线；执行时同步产品规则，无需重新选择服务商。总结由当前 Codex 读取文字完成，不增加总结 API。
2. 先复用 openai-whisper，不同时迁移 faster-whisper 等引擎。检查真实 device、CUDA、显存、模型缓存，使用已安装模型；默认不能只凭名字选 medium。显卡不足可用 CPU，但记录实际耗时，不隐瞒降级。
3. 模型和依赖配置由部署负责，普通页面不出现技术参数。缺依赖/模型明确进入 BLOCKED，不无限自动重试；不静默下载大型模型或切换收费服务。
4. 同机自动读取已重建文件，不复制到 inbox，不显示领取。内部处理锁仍必要。异机保留既有兼容入口，其领取只同步，不触发旧 ASR；完整异机自动部署不在本期。
5. 独立本地处理进程执行转写，API 重启或页面关闭不负责启动模型。按任务串行处理，避免多模型抢显存。先完整上传再处理，不改变 MediaRecorder。
6. 本期产品规则明确例外：转写自动，总结需一次聊天发起；状态显示“待总结”。不能用提示文案或占位摘要掩盖该边界。

## Scope

真实短音频验证、独立转写进程、持久化分段恢复、同机自动入队、Codex 文字总结内部命令、草稿回传、状态与文档同步、实际发布链路隔离验证。

## Out of scope

直接让 Codex 听 MP3、自动操控聊天、定时自动发消息、总结 API、换 ASR 引擎、实时识别、说话人分离、全站 UI 改版、多机分布式部署、生产部署、提交推送、自动发布或删除原录音。

## Contracts and data changes

### 存储与状态

- server/migrations.py 增量增加 processing_runs 与 processing_parts；升级前 SQLite backup，重复迁移安全，不改变用户归属及已发布版本。
- runs：id、task_id、source_hash、pipeline_version、model、language、device、stage、status、generation、lease_owner、lease_expires_at、heartbeat_at、attempts、next_attempt_at、error_code、created_at、updated_at。
- parts：run_id、part_index、start_ms、end_ms、core_start_ms、core_end_ms、input_hash、status、attempts、result_json、updated_at；唯一键 run_id/part_index。
- stage：PREPARE、ASR、WAITING_SUMMARY、SUMMARIZE、VALIDATE、DONE；status：QUEUED、RUNNING、RETRY_WAIT、BLOCKED、FAILED、SUCCEEDED。
- 对外 processing_tasks 增加 TRANSCRIBED（待总结）、SUMMARIZING（总结中）。保留 REVIEW、COMPLETED；失败新作业不清除已有发布指针。更新所有状态校验、筛选、前端映射及手机状态。
- 原音频清单哈希加模型/语言/预处理/分段版本构成缓存键；输入变化不能复用旧转写。事务领取、generation 校验防止旧进程提交覆盖。

### 转写入口与恢复

- tools/livenote_transcriber.py 作为独立常驻进程；server/processing/store.py、pipeline.py 提供存储和编排，复用 asr.py 的模型及解码逻辑。
- 完整上传后仅创建 READY；进程自动扫描并领取本机 READY。GET 列表纯读取。API 启动不调用旧 recover_jobs；旧 create_job/auto-process 入口转为统一入队兼容层，不能启动第二套转写。
- supervisor 与实际 ASR 子进程分离，支持心跳和超时终止自身子进程；禁止终止无关 Python/API 进程。超时按音频时长设置保守上限（默认 max(600秒,片段时长×20)），实测后记录调整值。
- 过期执行权仅接续未完成片段；失败最多三次自动尝试，退避，配置/格式错误不重试。人工重试仍复用检查点。
- 所有分段成功原子落盘后才保存全场 transcript.json 和 transcript.txt，任务进入 TRANSCRIBED；不调用 content.py 的抽取式降级来生成 REVIEW。
- 缓存重建音频用于转写和试听，唯一临时文件、原子替换；上传容器块先重建，不直接当独立音频识别。
- 以300秒/1秒重叠为初始可配置参数，保存非重叠覆盖范围；边界文字去重须对齐相邻片段文本和时间，不能只裁时间而保留重复句子。不得把缺词误当静音；测试否定词和跨段句。
- 语言支持 auto/en/zh；英语样本用 en，未知录音默认 auto，明确保留实际检测语言。录音中的指令是数据，不作为代理操作指令。

### Codex 总结与回传

- 扩展 tools/livenote_worker.py：summary-list、summary-prepare --task-id、summary-submit --task-id --run-id --generation --file。文档提供代理执行流程，用户只发自然语言。
- summary-prepare 通过现有鉴权体系获取 TRANSCRIBED 任务并取得限时总结执行权，导出带来源哈希、时间戳、章节和用户标记的文字包。选“最新”按录音时间且状态待总结排序；用户指定单任务不可批量处理。
- 所有命令从既有配置/环境读取凭证，不打印密钥、不绕过鉴权、不使用数据库直接写入模拟登录。缺少凭证说明一次性部署配置问题，不让用户手工搬 JSON。
- 在 main.py 增加 Worker 鉴权的 /tasks/{id}/summary-input（POST，领取总结执行权并返回文本包）与 /tasks/{id}/summary-result（POST）接口。复用 verify_task_lease 思路，增加 run/generation/sourceHash 校验，不依赖旧下载租约。
- Codex 对短录音读取完整文字；长录音按时间片生成有 segmentId 引用的局部摘要并保存，分层合并，覆盖开头、中间、末尾及跨段纠正。不重复传全文、不截断后半场。
- summary-result 请求含 runId、generation、sourceHash、summaryVersion、result（现有 KnowledgeDocument）、evidence（要点到 segmentIds 的映射）。校验非空正文、引用存在、来源一致，幂等创建 result_revisions，并进入 REVIEW；不自动发布。
- 格式校验不等于语义准确，Codex 保留听写不确定标记，禁止虚构事实、责任人和期限。总结中断租约到期退回 TRANSCRIBED，保留局部摘要，可聊天继续。

### 用户体验与启动

- Item 单一状态：排队、转写中（已完成片段/总片段）、待总结、总结中、待发布、已发布、失败；暂停/阻塞明确解释，不使用虚假百分比。
- 同机移除领取、复制提示词、JSON 选择的主操作；待总结只显示实际状态，明确需聊天发起，不展示“正在自动总结”。审核保留试听和发布，发布后记录保留。
- deploy/windows 增加统一启动入口，分别启动 API、控制台和转写进程并检查健康及重复进程；使用 Hidden，不重置管理员凭证。停止仅针对本次启动且核实归属的 PID。
- 更新 AGENTS.md、PRODUCT.md、README.md、server/README.md 和依赖说明，明确本期自动化边界与本地 ASR 支持。

## Implementation steps

- [x] 1. 已核对 diff、进程、Whisper/torch/FFmpeg、模型缓存和 GPU；初始环境为 CPU 版 torch，medium/base 模型已缓存，旧 PROCESSING 任务确认为遗留状态。
- [x] 2. 已用英语教材 135.55 秒录音和真实中文录音 24.24 秒片段完成真实转写；英语 base 用时约 49.93 秒，中文 base 用时约 12.12 秒，均为 CPU，并记录了识别问题。
- [x] 3. 已完成增量迁移、processing_runs/processing_parts、分段 JSON 检查点、source hash、代次和超时恢复；真实 301.98 秒录音已按两段成功转写并合并。
- [x] 4. 已接通独立 `tools/livenote_transcriber.py watch`；本机上传后的 READY/LOCAL_READY 任务自动处理，管理台读取不再触发旧 API 作业。
- [x] 5. 已完成 `/summary-input`、`/summary-result`、`summary-prepare`、`summary-submit`；真实 24.24 秒样本已由 Codex 文字总结并回传为 REVIEW 草稿。
- [x] 6. 已接入任务状态和筛选 UI、健康能力标识、统一 Windows 启动脚本；API 与控制台当前均已启动，浏览器关闭不影响转写。
- [ ] 7. 完成回归、恢复、发布隔离验证；长录音按下列层级验收，记录未提供真实长素材的边界。

## Validation

- [x] `npm run build`、`python -m unittest discover -s server/tests`、`git diff --check` 已通过；新增 processing 测试覆盖 schema 和并发领取。
- [ ] `test_processing.py` 仍需补齐第三段失败复用、模型变化缓存失效、空转写保护和摘要失败不丢转写等细粒度用例。
- [ ] 鉴权/归属测试：未授权不能读取转写/提交；音频中含恶意指令不改变权限；手机仅查看所属已发布内容。
- [x] 已启动 API+独立转写进程；真实 301.98 秒录音在后台完成，管理页面不参与处理；总结提交进入 REVIEW，未自动发布。
- [ ] 两份短录音都完成“Codex 总结→试听→显式发布→手机读取”的全链路；当前已完成一份短录音的总结回传，发布和手机读取仍需专门验收。
- [ ] 尚未执行 90 分钟工程长度测试；真实 90 分钟自然内容质量也没有授权素材，不能宣称已验收。
- [ ] 若有授权真实90分钟素材，预标开头/中间/结尾事实与跨段纠正，核对最终章节和要点。没有素材则记录“真实90分钟内容质量未验收”，不生成虚假验收证据。
- [ ] 记录音频秒数、转写秒数及比值、设备、峰值内存/显存、磁盘、重试次数；不预设最高效或固定完成分钟数。

## Acceptance criteria

- [ ] 两份短样本的发布链路尚未全部验收；识别质量明确受 CPU 与模型选择影响，当前中文口语 base 结果需人工核对。
- [x] 同机任务自动转写，关闭页面不影响；只有读取总结输入/发起聊天后进入 SUMMARIZING。
- [x] 分段落盘、source hash、代次、超时恢复和进程重启复用已实现；细粒度故障注入用例仍需补齐。
- [ ] 提取式降级不能成为正常总结，已发布历史及手机归属不受影响。
- [ ] 统一启动健康检查、构建和相关回归已通过；90 分钟工程长度测试待执行。
- [x] 已明确真实 90 分钟自然内容质量尚未验收，缺素材作为独立后续验收项保留。

## Risks and rollback

- CPU 可能很慢，GPU/模型依赖不匹配要先解决；不能靠更换 UI 掩盖。更换引擎或购买云服务属于后续决策。
- 在途旧 ASR 切换先核实进程归属，保存状态后再替换；生产重启需要避开正在上传/识别的任务，不批量杀进程。
- 迁移为增量，保留原音频、检查点、草稿及发布指针。回退关闭新转写进程及新调度，不删除数据、不覆盖运行中新数据，不恢复自动生成占位摘要路径。
- 本期总结依赖用户聊天发起；无人值守语义总结需另选可调用服务，不能由本方案悄悄承诺。

## Execution notes

- 2026-09-20：用户确认“继续执行”，进入 Luna 实现阶段。保留既有未提交修改，先验证本地 Whisper 环境与当前任务状态，再按本方案实现自动转写、检查点恢复和聊天总结回传链路。
- 2026-09-20：完成本机自动转写垂直切片。英语 135.55 秒样本使用 base/CPU 约 49.93 秒；真实中文 301.98 秒录音使用 base/CPU 完成分段转写，但出现同音和语言检测不稳定；medium/CPU 的 20 秒对比约 56.42 秒、中文结果更稳定。默认转写器改为 medium，允许通过 `LIVENOTE_WHISPER_MODEL` 明确选择其他已缓存模型。
- 2026-09-20：API health 返回 storageSchema 5、localTranscription=true；51 个后端测试和前端构建通过。真实 90 分钟内容质量与全链路发布验收尚未完成，不在本次执行中虚报完成。
- 2026-09-20：完成代码核对及本计划；未执行转写、测试、安装、重启或业务代码修改。
- 2026-09-20：已安装 `torch==2.12.1+cu126`，验证 `torch.cuda.is_available()=True`、设备为 NVIDIA GeForce RTX 4060，并用真实英语片段验证 Whisper 返回 `device=cuda`；API 与独立转写器已用新环境重启且健康检查通过。
- 本文件 ready-for-luna 表示方案可执行，不表示模型可用或识别质量已经验证。执行失败记录具体证据；架构需改变时才回到 needs-planning。

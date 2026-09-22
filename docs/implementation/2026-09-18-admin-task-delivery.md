# LiveNote 管理台 v1 与受控任务交付

- Status: in-progress
- Updated: 2026-09-18
- Branch/worktree: master / D:\06-project\LiveNote
- Base commit and relevant uncommitted changes: 5d0d5dbae0e98d124b464c56e1c6bb16fdeeefff。已有未提交修改：.gitignore、README.md、server/main.py、server/tests/test_main_storage.py、src/App.vue、src/main.ts、src/styles.css、src/upload/ApiClient.ts；未跟踪 docs/、src/control/、tools/。这些均为当前工作基础，保留，不重置或覆盖。
- Planner: GPT-6 Astra
- Executor: GPT-5.6 Luna

## Objective

交付本地可运行的管理台 v1：管理员管理用户及其会话，在页面把完整上传的录音交给本地 PC Worker 拉取，恢复失败任务，预览加工结果并人工发布；所属手机查询、缓存和阅读已发布总结。手机用户无需下载音频或操作提示词。

这是管理与文件交付闭环，不声称已实现自动语音识别。真实音频加工能力另设证据门槛；即使能力不可用，也应完成并验证本计划内的软件流程，明确报告该门槛未通过。

## Current state and evidence

- src/main.ts 使用 ?mode=control 选择 ControlConsole.vue；当前只有任务列表、领取、开始处理，入口不是权限边界。
- server/main.py 的 processing_tasks 在 complete_session 校验上传完成后创建。claim 先读后写，无事务抢占保护；status 可任意修改且 workerId 可省略。
- tools/livenote_worker.py 只拉 READY。页面领取使其成为 CLAIMED，之后被跳过；下载使用 response.read() 全量入内存，下载后立即标 PROCESSING；领取失败时仍尝试标 FAILED。
- result 接口接受任意 dict，用户字段可覆盖服务器 sessionId/taskId/version；固定 knowledge.json 被覆盖，上传立即 COMPLETED，无发布或版本保护。
- 当前已使用独立管理员/Worker/设备凭证和服务器归属；手机结果会在前台同步并缓存到 IndexedDB，知识卡编辑稿也单独保存在本机，仍需继续做正式多用户验收。
- server/retention.py 独立 SQLite 连接未开启 foreign_keys，不能依赖 processing_tasks 的级联删除。
- public/sw.js 不缓存 /api，请保持该规则。当前 src/storage/db.ts 为 IndexedDB v3。
- 已查看相关父目录及仓库，未发现 AGENTS.md。此前报告 build 与 33 个后端测试通过，执行阶段须重新验证，不能沿用报告作为验收。

## Assumptions and decisions

1. 当前服务器与 PC Worker 在本机运行；服务器地址可配置，为以后云服务器保留现有 HTTP 文件桥接方式。本轮不部署云端。
2. 用户无需自行注册密码。管理员创建用户、签发一次性设备配对码；手机首次输入一次，以后自动识别。管理员不以设备随机 ID 或客户端 ownerId 判断身份。
3. 管理台和 Worker 是不同角色。管理台向服务器提交拉取意图；常驻 Worker 轮询执行并写入本机目录。浏览器本身不执行 shell，不把“领取”伪装成“已下载到 PC”。
4. 人工加工、人工发布保留。轮询只搬运文件/状态，不能自动调用聊天界面、OpenAI API 或本地模型。未具备音频识别能力时保持待处理，不能以文件元信息或虚构转写生成结果。
5. v1 仅四个导航：待办任务、录音与内容、用户、设置。默认待办；不建设统计大屏。使用现有配色和紧凑列表，长 ID 和 Chunk 诊断放详情。
6. 本轮手机端增加配对、结果同步/缓存和轻量知识卡编辑稿。编辑稿只保存在本机，不覆盖服务器原稿；完整富文本编辑仍不在范围内。

## Scope

用户/设备归属、管理员与 Worker 鉴权、任务并发领取/恢复、文件拉取、本地加工状态、总结版本/人工发布、管理台四页、手机自己的服务端会话列表与结果缓存、升级迁移及真实运行验证。

## Out of scope

OpenAI API、自动聊天机器人、安装或调用 ASR/LLM 模型、云部署、支付、邮箱/短信登录、完整角色权限系统、多设备同步编辑、富文本编辑、批量破坏性清理、录音引擎重写。不得顺手启动旧 process/transcribe 接口。

## Contracts and data changes

### 身份和迁移

- 增量迁移放 server/migrations.py，在 init_db 调用；迁移幂等，有版本记录。执行前用 SQLite backup API 备份数据库及受迁移影响的已有总结文件，写入隔离备份目录，不清空数据。
- users(id, display_name, status ACTIVE/DISABLED, created_at, updated_at)。devices(id, user_id, label, token_hash, revoked_at, last_seen_at)。pairing_codes(hash, user_id, expires_at, consumed_at)，单次消费事务，10 分钟有效。
- sessions 增加 owner_id nullable、device_id nullable。旧会话保持未分配；管理员显式按单场/明确选中集合分配，不能自动绑定首次登录者。既有 IndexedDB 会话保留；旧 Session ID 不授予所有权。
- 管理员环境变量 LIVENOTE_ADMIN_TOKEN、Worker 环境变量 LIVENOTE_WORKER_TOKEN，均为独立高熵凭证。管理页手动输入管理员凭证，保存在 sessionStorage；Worker 从环境读取，前端构建不含这些凭证。开发环境也不能空凭证放行管理接口。
- 手机 POST /api/v1/auth/pair {code,label} 返回随机设备 token（数据库仅保存 hash），GET /auth/me；Authorization: Bearer。设备 token 保存本机，撤销/停用后服务端立即拒绝。未配对时禁止开始新录音；已配对设备认证失效时暂停上传并提示重新配对。
- 管理员接口 /api/v1/admin/*，Worker 接口仍 /api/v1/tasks/*，各自服务端依赖校验角色。手机所有 session/segment/chunk/marker/audio/result/删除接口统一先验证 session 所属用户，服务端从身份写 owner_id，忽略或拒绝客户端伪造值。子资源还须匹配父 Session。
- 清查旧 report/transcript/jobs/diagnostics 等旁路：旧加工触发端点默认 410；历史读取受所属用户或管理员限制；诊断需已认证。共享 VITE_API_KEY 不再作为管理员或所有用户通行凭证，更新环境示例与启动校验。公共 health 只返回非敏感能力。
- 用户同一安装不提供随意账号切换。重新配对其他用户时若存在本地会话，阻止静默改归属；提示管理台处理归属，保留音频。后续账号切换另做。

### 管理 API

- GET/POST /admin/users；PATCH /admin/users/{id} 编辑昵称和启停；POST /admin/users/{id}/pairing-codes；GET /admin/users/{id}/devices；POST /admin/devices/{id}/revoke。
- GET /admin/sessions?ownerId=&query=&status=&offset=&limit=，含未分配筛选；GET /admin/sessions/{id} 含摘要/任务/结果版本；PATCH 更新标题或明确归属（归属变化记录操作，原用户后续请求无权访问）。
- GET /admin/tasks 同样分页过滤；POST /admin/tasks/{id}/request-pull {workerId} 保留作兼容/诊断接口；POST .../release、.../retry；GET /admin/workers 返回最近心跳与离线判断。
- GET /admin/tasks/{id}/results；POST .../publish {revisionId}。管理员查看草稿，手机只看发布指针。
- GET /sessions 列当前认证用户服务端会话（用于同用户另一设备查看结果）；必须分页并避免与本地 Session 合并时覆盖录音状态。
- 所有列表返回 {items,total,offset,limit}；保留旧 tasks 的 {tasks} 响应仅作为桥接器升级兼容，消费者同步更新。

### 任务协议

- processing_tasks 增加 requested_worker_id、lease_token_hash、lease_expires_at、downloaded_at、error_stage；任务状态固定 READY → CLAIMED → LOCAL_READY → PROCESSING → REVIEW → COMPLETED，失败 FAILED。READY_TO_UPLOAD 旧值迁移 REVIEW；旧 COMPLETED 有合法文件时导入历史已发布版本，无有效文件标 FAILED 并保留原因。旧 CLAIMED/PROCESSING 标恢复待确认，不自动当作运行中。
- Worker 使用自己的鉴权持续轮询 READY 队列并自动领取；管理台不再要求人工指定电脑。request-pull 仅保留作兼容/诊断能力，不属于正常操作流程。
- claim 在 BEGIN IMMEDIATE 内条件更新，返回一次 leaseToken；30 分钟租约、60 秒 heartbeat，所有 Worker 状态写入含 leaseToken。workerId 不是授权凭证。领取失败不能修改任务。服务器 Worker 凭证加租约双重校验，过期旧 Worker 写入 409。
- 状态转换端点保留 /tasks/{id}/status，但实现严格转换表：CLAIMED→LOCAL_READY，LOCAL_READY→PROCESSING，活动态→FAILED；禁止直接写 COMPLETED。结果上传产生 REVIEW，管理员发布才 COMPLETED。租约过期可回收未提交任务；REVIEW 不回收。人工 release 明确使旧租约失效。
- bridge 命令：list、pull（支持指定 task 与恢复同 Worker 已领取任务）、watch、start-processing、submit-result；upload-result 作为 submit-result 别名，不再自动发布。watch 不自动开始加工。
- Worker 增加心跳登记端点，watch 保持本机所属活动任务租约。退出后任务可恢复或释放；下载用流式 .part、Content-Length/服务端 SHA256 验证、原子 rename。重启后先校验完整文件，再提交 LOCAL_READY。任务目录严格验证 ID 并限制在 inbox 根，保存 manifest 含服务器来源/Session/任务/lease 信息，权限凭证不得出现在日志或提交到 Git。
- /tasks/{id}/audio 继续返回可播放整场文件；增加内容长度与 SHA256，未完整上传拒绝取音频。管理员不在浏览器保存音频；播放可以走受控服务端接口。

### 总结与发布

- 新增 result_revisions(id, task_id, session_id, version, content_hash, content_json, created_at)、sessions.published_revision_id，以及 audit_events(actor, action, target, at, details)。v1 总结 JSON 存 SQLite，保证原子性；发布后 knowledge.json 是兼容导出副本，查询以数据库为准。
- 严格 Pydantic KnowledgeDocument：title、overview、keyPoints:string[]、knowledgeStructure:{title,points[]}[]、questions:{question,answer,startMs?}[]、actionItems:string[]、confidenceNotes:string[]、可选 transcript:{text,segments:[{index,startMs,endMs,text}]}；验证类型/时间和体积（最多 5 MB）。sessionId/taskId/version/updatedAt 由服务器生成，不能被正文覆盖。至少有非空 title 和 overview 或 keyPoints。
- 同任务相同 content_hash 重试返回同 revision，版本号服务端递增。不同内容生成新草稿；发布需明确 revisionId，幂等；旧发布版在新加工失败期间仍可访问。
- GET /sessions/{id}/result 返回 {sessionId,taskId,status,version,updatedAt,result,revisionId}，只含最新已发布正文。无发布结果返回 result:null，状态与录音上传状态分开。结果响应 Cache-Control:no-store。
- IndexedDB 升级 v4 加 results（key=sessionId，ownerId、revisionId、原稿）及 resultDrafts（ownerId、sessionId、baseRevisionId、正文）。本轮仅实现原稿缓存和不覆盖 draft 的规则。离线保留已收到结果；认证失效隐藏云内容并停止查询，不删除本地录音。
- ResultSync.ts 前台可见时每 30 秒查询自己会话，进入会话/online/手动刷新触发；限制并发、避免重叠、离线退避、卸载取消。不要把轮询绑定到录音生命周期，不重新遍历 Blob。服务端列表与本地会话分开合并；云端仅结果会话也能打开知识阅读页。

## Implementation steps

- [x] 1. 建立基线：读本计划、git diff；备份/隔离测试数据。读取本仓库适用指令。规划阶段只新增本文件，执行阶段才改产品代码。
- [x] 2. server/migrations.py、auth.py 实现身份与幂等升级；main.py 接入认证依赖、归属检查，retention.py 启用外键并清理新增关联，保留既有录音上传语义。
- [ ] 3. server/task_service.py、result_service.py 承担事务/转换/版本发布；server/admin_routes.py 放管理端路由，main.py 连接实际入口。避免继续把所有实现追加在 main.py。
- [x] 4. tools/livenote_worker.py 修复恢复/下载/租约并实现 watch、提交草稿；提供本地启动说明。全部操作使用 LiveNote 自有 API。
- [x] 5. src/control/ 保留 `?mode=control` 兼容入口，使用动态 import；已提供任务、会话、用户、设置四个管理入口及真实 API 按钮。
- [x] 6. upload/ApiClient.ts、App.vue 接入手机设备凭证；未配对时禁止开始录音和上传；新增 IndexedDB v5 results/编辑稿缓存和 ResultSyncManager 前台结果轮询。编辑稿只保存在本机，不覆盖服务器原稿。
- [x] 7. 增加 API 归属/配对测试；README 修正文案和启动方式；已完成编译、后端测试和本地运行检查。浏览器交互仍需人工确认自签名 HTTPS。

## Validation

- [x] npm run build；python -m unittest discover -s server/tests；git diff --check 待最后执行一次。
- [ ] 升级测试使用临时 SQLite/数据目录：旧三层音频、旧结果保留；重复迁移无变化；未分配内容不可被普通设备读走。
- [ ] API 集成测试两个用户、管理员、Worker：跨用户读/写/删/子资源/结果全拒绝；伪造 ownerId、缺凭证、撤销设备拒绝；现有上传幂等与整场重建测试继续通过。
- [ ] 并发 claim 只有一个成功；丢失下载响应后恢复；领取冲突不标他人 FAILED；租约过期回收，旧租约不得提交；非法转换拒绝；Worker 下载失败及复原用临时目录和本地测试 HTTP 服务验证。
- [ ] 有效结果提交尚不可见，发布后所属手机可见；重复提交与发布幂等；正文保留字段注入拒绝；新版不覆盖旧版/编辑稿；删除后无孤立任务与版本。
- [x] 管理台实际页面本机模式闭环：领取隔离音频→落盘→开始处理→读取 `knowledge.json`→草稿预览→发布；浏览器控制台无错误，发布后结果接口返回 `COMPLETED`。
- [ ] 本地实际运行独立测试服务和 watch：管理页面请求拉取→本地音频落盘→LOCAL_READY→人工提交标注为“流程测试”的 fixture→REVIEW→人工发布→手机视口打开知识页→断网刷新仍可阅读。fixture 仅用隔离测试会话，不写进真实录音结果；浏览器检查控制台错误。
- [ ] 在不调用 API/本地模型前提下检查当前工具是否能实际接收并识别短音频；可用则验证少量原句并记录证据，不可用则记“真实音频加工能力未验证/不可用”。文件可读取、ffprobe 成功、fixture 回传均不等于转写成功。不得偷偷改用模型或声称全部业务已完成。

## Acceptance criteria

- [ ] 管理员通过真实页面创建用户、配对设备、搜索其会话、发起拉取、查看错误、预览版本并发布，所有按钮连接实际 API。
- [ ] 普通手机只访问自己的内容；未分配旧会话只能由管理员分配；录音不受服务器/身份/结果查询失败影响。
- [ ] 页面领取不会造成任务无法下载；PC 重启/下载中断可恢复；处理状态反映实际阶段；没有永远无人处理的隐式锁。
- [ ] 草稿不会提前出现在手机；发布后手机前台自动查询或刷新获得，离线保留；更新不覆盖编辑稿。
- [ ] 有运行入口、凭证设置说明和可重复的隔离端到端证据，非仅独立组件/单元测试。
- [ ] 最终明确区分软件交付验收与真实识别质量。真实音频能力未通过不阻止本管理台计划完成，但必须作为整体 LiveNote 内容加工尚未闭环的剩余阻碍报告。

## Risks and rollback

- 共享 Key 改角色凭证是兼容性变化；先在隔离服务验证，再更新本地进程，避免正在录音时重启前端或清 IndexedDB。旧前端应得到可理解的重新配对提示。
- 不承诺音频识别绝对准确；现有对话工具能力是待验证项，本轮无付费服务授权。
- 已下载到设备的历史内容不能通过服务端撤权远程收回；本轮限制联网访问并保护本机重新配对流程，不宣称远程擦除。
- 回退使用执行前数据库与相关结果备份、相应代码快照；不能对持续新增的数据直接覆盖旧备份。IndexedDB v4 增量兼容，禁止清库回退。
- 不删除真实文件、不批量重分配、不提交/推送/部署；测试仅用隔离目录。迁移碰到损坏结果要留原文件并记录错误。

## Execution notes

执行记录：2026-09-18

- API 已迁移到 schema 4；当前本地 API `http://127.0.0.1:8000` 正常，`/api/v1/health` 返回 FFmpeg/FFprobe 可用、Whisper medium 已缓存、LLM 未配置。
- 已实现管理员/Worker/手机设备三类身份、设备配对、Session 归属隔离、任务租约、严格状态转换、草稿 REVIEW 与管理员发布。
- 管理控制台已能管理任务、会话、用户/配对码和设置；手机只显示所属设备可见的已发布结果，并在前台每 30 秒查询、缓存到 IndexedDB。
- Worker 已支持 `pull`、`watch`、`start-processing`、`upload-result`/`submit-result`；音频使用 `.part`、长度/哈希校验、原子改名。
- `npm run build` 通过；后端 34 项测试通过；API 重启后 health 和 admin token 401/200 检查通过。
- 当前尚未满足完成条件：真实浏览器双角色端到端操作尚未在自签名 HTTPS 页面完成；管理台仍是 v1 紧凑单文件页面；手机端编辑稿的多用户隔离仍需验收；真实音频“ChatGPT 对话加工”能力仍需人工验证。
- 本轮后新增 `/admin/tasks/{id}/results`，控制台可查看 REVIEW 草稿的版本 JSON 并发布指定版本；发布路径已有后端回归测试。
- Worker 本轮增加 `manifest.json` 和本地音频 SHA256/大小校验：电脑重启后若文件完整则复用，否则重新下载；`watch` 会维持租约并同步本地 task 状态。API 下载接口记录 `downloaded_at`。
- 管理台删除会话修复：重新启动加载最新 DELETE 路由；删除失败时确认区保持展开并在原位置显示错误，删除成功后才从列表移除。已用 `session-single-valid-regression` 完成浏览器验证并确认服务端查询不到该测试会话。
- 2026-09-19 使用带合法 Opus/WebM 音频的临时 `E2E-TEST` 会话完成管理台真实页面闭环：领取后文件落到 `worker-inbox`，随后从页面进入处理中、读取本地 `knowledge.json`、打开草稿并发布；服务端核验任务为 `COMPLETED`、结果标题正确且浏览器错误日志为空。测试夹具及其任务目录已清理，未改动真实录音。
- 修复手机端服务器状态误报：上传队列此前只接受 `storageSchema === 2`，与当前 API schema 4 冲突，现改为接受 schema 4 及以上兼容版本。健康检查实测返回 schema 4 且 capabilities 正常。
- 会话列表状态说明优化：`录音已完成` 与音频上传状态分开显示；存在未上传块时直接显示 `待上传 N`，不再让“已完成”掩盖上传未完成。
- 管理台任务页修复“请求拉取无反应”：API 请求成功后仍保持 `READY` 是设计行为，页面现在显示“等待 Worker 领取”、目标 Worker 和“已请求”按钮状态，并补充请求→下载→处理→发布流程提示；同时统一任务列表的紧凑布局和操作反馈。
- 按用户反馈继续收紧管理台视觉：去掉冗余 eyebrow，统一管理台按钮为 32px 紧凑工具按钮，降低请求拉取按钮的亮度/高度并统一刷新、创建、发布操作的控件语言。
- 本地 PC 模式新增 `/admin/local-worker` 与 `/admin/tasks/{id}/pull-local`：管理台显示“领取到本机”，由当前 API 进程完成领取、整场重建、SHA256/大小记录和 `worker-inbox/<task-id>/` 落盘，任务直接进入 `LOCAL_READY`，不再要求用户切换命令行。
- 新增隔离的本机控制台闭环回归：验证实际音频文件落盘、`LOCAL_READY → PROCESSING → REVIEW → COMPLETED`、结果发布和手机结果接口读取；后端回归测试现为 45 项全部通过，并覆盖并发领取、过期租约恢复和本地 `knowledge.json` 自动回传。
- 修复整场重建输入错误：先将每个 Segment 的 Chunk 恢复为 Segment 文件，再按 Segment 顺序合并；任务下载接口和本机领取共用该流程。
- 云端模式仍保持边界：云端只能排队和等待家庭 PC Worker；浏览器按钮不能跨网络启动家庭 PC 或直接写入家庭 PC 磁盘。家庭 PC 需要一次性安装/启动 Worker，之后可自动领取。
- 不将本交付标记为 complete；下一轮应优先做隔离 fixture 的管理台→Worker→REVIEW→发布→手机阅读端到端验证，然后再补剩余管理细节。

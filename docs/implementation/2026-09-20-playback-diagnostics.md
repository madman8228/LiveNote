# 历史录音播放异常：手机现场诊断交接方案

- Status: in-progress
- Updated: 2026-09-20
- Branch/worktree: master / D:\06-project\LiveNote
- Base commit and relevant uncommitted changes: 5d0d5dbae0e98d124b464c56e1c6bb16fdeeefff。工作树已有大量修改，尤其 src/App.vue、src/upload/ApiClient.ts、src/storage/db.ts、src/styles.css、server/main.py、server/tests/test_main_storage.py、public/sw.js；docs/、tools/ 等未跟踪。执行前重新核对，保留全部既有工作。
- Planner: GPT-6 Astra
- Executor: GPT-5.6 Luna

## Objective

交付一个接入历史页真实播放器的诊断能力：用户正常播放并复现进度条跳动，点击一次“反馈播放问题”，系统自动保存并回传可分析的事件时间线。用户不打开开发者工具、不搬文件、不复制日志。能够区分播放位置异常、总时长变化、音源替换/播放器重建、缓冲或网络失败；不预先认定根因。

## Current state and evidence

- App.vue 的 restoreSessionForPlayback 在本地上传完整时优先下载服务器文件，catch 丢失失败原因后走 createSessionPlayback；重复点击准备会撤销旧 URL。
- SessionRecovery.ts 在追加前发布 MediaSource URL，设置 sequence，逐块追加，最后设置 duration/endOfStream；失败再切换 Blob。原生 audio 的 key 与 URL 绑定。MSE 通常需要先附着媒体元素才能触发 sourceopen，因此本期不得简单把 URL 回调移到追加结束后。
- 历史页用原生 audio controls，没有自定义 currentTime 计算。loadedmetadata 只更新旁边的时长文本；不能据此认定它修改了浏览器进度条。
- 上轮文件检查发现服务器完整样本时间戳无倒退，原始段通常无容器 duration；这些检查没有复现手机故障。分片不是独立文件不等于顺序 append 必然错误。此前“已定位根因”的结论已撤回。
- buildDiagnosticSnapshot(schemaVersion=2) 记录多条会话的静态状态，没有播放事件。ApiClient.uploadDiagnosticSnapshot 以 multipart POST /api/v1/diagnostics，返回 diagnosticId；服务器保存 DATA_DIR/diagnostics/<id>/snapshot.json 和 metadata.json。生产鉴权、10MB 默认限制已有实现。
- vite.config.ts hmr=false；public/sw.js 有资源缓存。构建成功不能证明手机已加载新代码。
- package.json 只有 dev/build/preview，无前端测试框架。DB_VERSION 当前为 5；运行数据库默认 server/livenote.sqlite3，不能误用 server/data/livenote.sqlite3。

## Assumptions and decisions

1. 本期仅诊断采集、现场反馈及证据分析工具；不替换播放方案、不改变码率/30秒切片、不重编码、不删除录音。
2. 默认只在播放准备开始后采集当前播放尝试，内存有界；不自动上传，用户点击反馈才持久化并授权补传。原生控件的 seeking 事件不能证明用户主动拖动，分类必须保留“未知”。
3. 用户步骤：更新后正常播放 → 复现 → 点“反馈播放问题”。可选描述“自行跳动/拖动后跳动/其他”，不强制填写、不添加提交确认层。
4. 反馈点击立即固定此前60秒（不足则全部）及准备摘要，并继续收集最多5秒后续事件后上传；不宣称记录故障后一分钟。页面隐藏或卸载时提交已采集部分并标记截断，不能依赖卸载异步请求完成。
5. 真机原始故障复现属于后续证据收集，不是本期诊断能力交付的阻塞条件。没有现场包不得宣布原故障解决。

## Scope

- 新增 src/diagnostics/PlaybackDiagnostics.ts：可注入时钟的有界采集器、序列化和媒体事件绑定/解绑。
- 新增 src/diagnostics/PlaybackFeedback.ts：冻结现场、持久化和有限补传；使用独立 IndexedDB 数据库 livenote-playback-diagnostics（v1），不迁移录音库。
- App.vue：准备过程与 audio ref 生命周期接入；播放器附近弱化反馈按钮及状态；src/styles.css 仅必要的触控/反馈样式。
- SessionRecovery.ts：增加可选诊断回调，只观察现有路径；ApiClient.ts：保留音频请求的结构化失败原因并接入既有反馈方法。
- vite.config.ts 与必要的类型声明：注入构建标识（短提交号+构建/开发服务启动时间，允许环境覆盖），快照必须来自实际加载的前端常量。
- tools/analyze_playback_diagnostic.py：读取明确指定的一个诊断包，输出证据时间线与候选异常，默认不遍历其他反馈。
- 独立测试文件、最小测试配置、诊断使用说明。服务端仅在既有接口契约测试需要时修改测试文件，预期无业务接口改动。

## Out of scope

统一服务器播放、音频缓存/Range 改造、后台重建队列、全站监控、无用户触发的遥测、内容转写或总结、生产部署、服务重启、提交推送。不要自动切换播放模式来做对照实验；获得现场证据后另定修复。

## Contracts and data changes

### 诊断包

独立 schemaVersion=1、kind=playback-diagnostic、clientReportId(UUID)、createdAt、buildId、sessionId、attemptId、captureStart/end、截断原因、用户可选描述、平台/浏览器版本、页面可见性与 online 状态。仅携带当前会话的段数/块数/上传计数/标称时长/MIME；不调用现有枚举所有会话的 buildDiagnosticSnapshot。

每条事件含 seq、相对 performance.now() 毫秒、type、attemptId、elementId、sourceId，以及必要字段。sourceId 是本地递增编号，禁止保存真实 Blob URL、完整页面地址、查询参数、鉴权头、token、录音字节、逐字稿、标题、其他会话或原始 HTTP 响应正文。NaN/Infinity 用 null 加明确状态表达，不在 JSON 中静默丢失含义。

- 准备摘要：本地上传完整判断、服务器请求开始/收到头/下载结束、HTTP status、耗时、字节数、错误类别（network/timeout/http/blob-read/unknown）、选定模式和每次回退原因。下载计时必须覆盖 response.blob，不能把收到响应头当完成。
- 媒体事件：loadstart、loadedmetadata、durationchange、loadeddata、canplay、play、playing、pause、waiting、stalled、seeking、seeked、ratechange、ended、emptied、abort、error；有界节流的 timeupdate/采样（活动时最多每秒2次，后台不追补）。
- 媒体状态：currentTime、duration及有限性、paused、seeking、ended、playbackRate、readyState、networkState、error.code、buffered/seekable 范围（最多8段并标记截断）。
- 应用事件：准备按钮点击、URL 分配/撤销/替换、audio mount/unmount、列表刷新、选中项/Tab变化、online/offline、visibility/pagehide。ref 重复执行不得伪造重新挂载；用元素身份判断。
- 用户交互证据：播放器 pointerdown/up/cancel、keydown 的安全类别；不采集任意输入文字或精确坐标。不通过 isTrusted 或单个 seeking 事件断言人为拖动，允许用户描述辅助判断。
- MSE：sourceopen/ended/close、实际 sourceBuffer.mode、mode设置失败、追加进度和 bufferedEnd、duration赋值前后、endOfStream、解析/超时错误。正常追加每秒最多一条汇总，失败即时记录块序号/段序号；不额外读取音频字节。

环形缓冲保留最近60秒，最多600条；准备摘要单独最多80条，避免长录音准备事件被挤掉。单包序列化上限256KiB，先裁旧采样，保留用户标记/错误/源切换和截断统计。诊断回调失败必须吞掉并计数，不能让播放进入回退路径。

### 反馈和恢复

状态 idle → collecting → uploading → sent，或 pending/error。点击立即保存冻结包；5秒收尾后更新包并上传。按钮覆盖准备中和准备失败场景，不依赖 audio 已存在或全局 errorMessage。

通过现有 uploadDiagnosticSnapshot 上传；description 固定含 playback、clientReportId、sessionId，避免依赖任意用户描述检索。保存返回 diagnosticId；成功后移除本机待发副本。同一客户端报告重复点击合并；响应丢失重试沿用 clientReportId，服务器可能有重复文件，分析器显示该标识，不承诺服务端 exactly-once。

本机最多3个待发报告、每个256KiB、TTL 24小时；只自动淘汰诊断数据。按绑定身份隔离，身份改变不上传原身份报告。网络恢复和下次启动补传已授权报告，最多3次自动尝试并退避；401/403不循环重试，提示反馈暂未发送。提供“重试反馈”；成功与待发状态在按钮旁显示，普通用户无需识别内部编号。IndexedDB失败时保留内存副本并允许即时上传，如实提示无法跨刷新保存。

### 只读分析

命令 python tools/analyze_playback_diagnostic.py --input <明确snapshot.json路径>。输出版本、尝试/模式、回退原因、用户标记附近事件；识别 currentTime 差值与单调时钟×速率不匹配、duration改变、source/element改变、缓冲空洞候选。暂停、后台采样缺口、主动或未知 seek、重播必须保留上下文；输出“证据/候选解释/缺失证据”，不能把阈值命中写成根因结论。无异常也应输出采集覆盖与限制。

## Implementation steps

- [x] 核对当前工作树和入口；本计划改 in-progress，记录前端版本与现有构建基线。
- [x] 建立采集器和有界序列化；新增 `test:playback` 命令并用可注入时钟验证事件上限、采样和冻结。
- [x] 接入真实 App.vue 播放准备与 audio ref；为 SessionRecovery 增加只读回调，记录真实失败/回退；未改变播放器 key、src 策略或 MSE 追加顺序。
- [x] 加入独立待发库和反馈入口，接入既有 POST；断网/身份变更/存储失败路径保留待发报告，不修改录音状态。
- [x] 注入可追溯 buildId，提供只读分析命令和单会话诊断说明，说明手机正常刷新加载新版（录音进行中不刷新）。
- [x] 完成自动化验证；真实浏览器/真机证据仍待采集，当前状态为“诊断功能完成，真实故障待采集”，不宣称进度条已修复。

## Validation

- [ ] `npm run test:playback` 已覆盖采样、事件上限、冻结和分析器回归；重复 ref、待发库、隐私字段白名单及真实媒体时间轴仍需补充接线/浏览器测试。
- [ ] 测试真实绑定入口而非仅采集器：media事件→快照→反馈上传；模拟服务器失败→MSE→Blob得到完整原因链；替换 audio 得到新elementId；暂停/seek/后台不误报确定根因。DOM模拟只能证明日志接线，不证明真实媒体时间轴。
- [ ] 待发库测试：断网保存、刷新恢复、online重试、身份隔离、TTL/容量、响应丢失沿用ID、存储失败；上传失败不修改录音状态。
- [x] `python -m unittest discover -s server/tests`：59 个测试通过，包含临时 DATA_DIR 下的 multipart 诊断落盘测试。
- [x] `python -m unittest discover -s tools/tests -p test_playback_diagnostic.py`：4 个分析器测试通过，覆盖位置跳变、duration变化、源替换和合法 seek。
- [x] `npm run build`；`git diff --check`：生产构建通过；diff 检查仅报告既有文件的 LF/CRLF 转换警告。
- [ ] 本地隔离浏览器：使用合成短音频和测试会话，真实播放/暂停/拖动，点击反馈，经实际本地诊断API落盘后运行分析器验证匹配；不得以仅mock上传替代端到端证据。不为测试读取其他用户录音。
- [ ] 360px视口按钮可触达、反馈状态可读；关闭详情/切Tab/重复准备无重复监听。长时间采集有界，采集不调用load/play/pause/seek或修改src。
- [ ] 现有服务不自动重启；需要运行新版时用隔离测试服务验证，交付注明手机加载新版的实际条件。遇到自签证书拦截不能绕过，记录阻塞和已验证范围。

## Acceptance criteria

- [ ] 历史页在准备、播放、失败时都能一次反馈；无强制文字/截图/导出步骤。
- [ ] 现场包含关联一致的session/attempt/source/element标识、实际版本、准备原因链和媒体时间线，能区分三类异常证据。
- [ ] 联网经实际接口保存并可由命令读取；离线报告保存后在恢复网络时补传，失败明确可重试。
- [ ] 不上传音频内容、密钥或其他会话；播放与录音业务行为不受采集器错误影响。
- [ ] 自动测试、实际浏览器接线及落盘闭环有证据；模拟故障只验采集能力，不作为手机原问题根因证明。
- [ ] 用户后续只需更新页面、复现、点击反馈；真实故障包到位后再开展定位与修复。本期完成不以用户已经复现为前提。

## Risks and rollback

- 采样会增加开销：缓冲放普通对象，不逐事件触发Vue响应；事件监听有统一dispose；定时器仅当前尝试活动期间运行。
- 原生控件及后台节流限制证据精度，未知保持未知；不擅自用自定义控件替换原生播放器。
- Dev关闭HMR、PWA缓存可能保留旧版，以包内buildId验明，不能建议清除手机录音存储。
- 回退只移除诊断接入与反馈UI、停止补传；不回滚整个工作树、不删除录音库，不恢复旧数据库覆盖新数据。

## Execution notes

- 2026-09-20：规划检查了产品/设计规则、工作树、播放器、MSE恢复、反馈API、录音库和缓存配置。本轮仅创建交接文档，未改产品代码、运行测试或部署。
- 之前的“MediaSource即根因”未通过手机复现验证，本方案不采用该结论作为实施前提。
- 2026-09-20：收到“继续执行”，进入 Luna 实施阶段。保留既有工作树，不切换分支、不重启服务。
- 2026-09-20：完成诊断采集器、历史播放器接线、独立待发库、buildId、单包分析器、使用说明和服务端落盘回归测试。验证结果：`npm run test:playback` 通过；`python -m unittest discover -s server/tests` 59 个通过；`npm run build` 通过；`git diff --check` 无内容错误但有既有换行警告。
- 2026-09-20：复核隐私边界后，将服务器/恢复错误从原始正文改为错误类别；反馈入口在播放准备中也可点击，未开始播放时会给出明确提示。
- 2026-09-20：尚未拿到真实手机故障包，也未宣布原进度条问题已修复。下一步只需在加载新版后复现一次并点击“反馈播放问题”，再用明确路径的单个 `snapshot.json` 分析；不读取其他会话。
- 2026-09-21：修复历史页远端专有录音无法点击“播放录音”的回归。根因是按钮只检查本地 segments/chunks，远端会话本地无 chunk 时被直接置为 disabled；现在按本地 chunk 或服务端 chunkCount 判断可播放，并让远端会话走服务器音频下载路径。新增回归测试先失败后通过，完整构建及 59 个服务端测试通过。
- 2026-09-21：根据现场反馈重做播放准备与反馈交互：准备阶段显示服务器提示或本地 `已完成/总数` 分片进度；准备完成后明确提示点击原生音频条播放；问题说明与“提交播放问题反馈”拆成独立、带标签的反馈区，避免把输入框误认为进度控件。
- 2026-09-21：修复播放器与准备状态错位：MediaSource 尚未完成追加时不再显示可操作的底部音频控件，也会先移除旧播放器；仅在追加完成并设置稳定 duration/endOfStream 后显示控件。新增 UI 状态回归测试通过。
- 2026-09-21：准备完成后自动尝试播放；若浏览器的自动播放策略拦截，则给出明确的原生播放键提示。播放状态与准备状态不再共用同一视觉状态。
- 2026-09-21：开始播放体验优化：录音正常结束或恢复结束后，后台仅对刚完成的本地会话生成 Blob 播放缓存；不遍历历史、不请求服务器重建、不自动播放。历史页打开该会话时直接复用缓存，录音重新开始时放弃发布后台结果。

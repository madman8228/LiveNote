<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiClient, ApiRequestError, type AdminResultRevision, type AdminTranscriptResponse, type AdminUser, type ProcessingTask, type ProcessingTaskStatus, type TranscriptResponse } from '../upload/ApiClient'

type View = 'tasks' | 'sessions' | 'users' | 'settings'
type TaskFilter = 'ACTIONABLE' | 'ALL' | 'READY' | 'PROCESSING' | 'TRANSCRIBED' | 'SUMMARIZING' | 'REVIEW' | 'COMPLETED' | 'FAILED'
type TaskGroup = { key: string; label: string; tasks: ProcessingTask[] }
type WorkerFileHandle = { createWritable(): Promise<{ write(data: Blob | string): Promise<void>; close(): Promise<void> }> }
type WorkerDirectoryHandle = { getDirectoryHandle(name: string, options?: { create?: boolean }): Promise<WorkerDirectoryHandle>; getFileHandle(name: string, options?: { create?: boolean }): Promise<WorkerFileHandle> }
type WorkerWindow = Window & { showDirectoryPicker?: () => Promise<WorkerDirectoryHandle> }
const view = ref<View>('tasks')
const tasks = ref<ProcessingTask[]>([])
const taskTotal = ref(0)
const taskLoadingMore = ref(false)
const taskFilter = ref<TaskFilter>('ALL')
const sessions = ref<Array<Record<string, unknown>>>([])
const sessionTotal = ref(0)
const sessionLoadingMore = ref(false)
const sessionOwnerId = ref('')
const bulkOwnerId = ref('')
const bulkAssignConfirming = ref(false)
const bulkAssigning = ref(false)
const users = ref<AdminUser[]>([])
const workerId = ref(localStorage.getItem('livenote-worker-id') || 'local-pc')
const adminUsername = ref(sessionStorage.getItem('livenote-admin-username') || 'admin')
const adminPassword = ref('')
const setupPassword = ref('')
const setupPasswordConfirm = ref('')
const adminSetupAvailable = ref(false)
const adminSession = ref(sessionStorage.getItem('livenote-admin-session') || '')
// Kept only so existing installations using the old shared token continue to work.
const adminToken = ref(sessionStorage.getItem('livenote-admin-token') || '')
const workerToken = ref(sessionStorage.getItem('livenote-worker-token') || '')
const userQuery = ref('')
const sessionQuery = ref('')
const sessionDateFrom = ref('')
const sessionDateTo = ref('')
const newUserName = ref('')
const loading = ref(false)
const message = ref('')
const error = ref('')
const selectedTaskId = ref<string | null>(null)
const selectedRevisions = ref<AdminResultRevision[]>([])
const selectedRevisionId = ref<string | null>(null)
const selectedTranscriptTaskId = ref<string | null>(null)
const selectedTranscript = ref<TranscriptResponse | null>(null)
const transcriptLoadingTaskId = ref<string | null>(null)
const transcriptError = ref('')
const originalAudioTaskId = ref<string | null>(null)
const originalAudioUrl = ref<string | null>(null)
const originalAudioLoadingTaskId = ref<string | null>(null)
const originalAudioError = ref('')
const publishingTaskId = ref<string | null>(null)
const pendingDeleteSessionId = ref<string | null>(null)
const deletingSessionId = ref<string | null>(null)
const interruptingSessionId = ref<string | null>(null)
const editingSessionId = ref<string | null>(null)
const editingSessionTitle = ref('')
const editingSessionOwnerId = ref('')
const savingSessionId = ref<string | null>(null)
const browserWorkerRunning = ref(false)
const browserWorkerBusy = ref(false)
const browserWorkerMessage = ref('')
const browserWorkerError = ref('')
let browserWorkerTimer: number | null = null
let taskRefreshTimer: number | null = null
let browserWorkerInbox: WorkerDirectoryHandle | null = null
const deleteError = ref('')
const requestingTaskId = ref<string | null>(null)
const summaryPromptTaskId = ref<string | null>(null)
const summaryPromptBusyTaskId = ref<string | null>(null)
const editingNoteTaskId = ref<string | null>(null)
const taskNoteDraft = ref('')
const savingNoteTaskId = ref<string | null>(null)
const localPullAvailable = ref(false)
const storageOnly = ref(false)
const bulkPulling = ref(false)
const apiBase = String(import.meta.env.VITE_API_BASE_URL || '/api/v1')
const adminAuthenticated = computed(() => Boolean(adminSession.value || adminToken.value))
const readyCount = computed(() => tasks.value.filter((task) => ['READY', 'FAILED', 'REVIEW'].includes(task.status)).length)
const readyTaskCount = computed(() => tasks.value.filter((task) => task.status === 'READY').length)
const visibleTasks = computed(() => tasks.value.filter((task) => {
  if (taskFilter.value === 'ACTIONABLE') return task.status !== 'COMPLETED'
  if (taskFilter.value === 'ALL') return true
  return task.status === taskFilter.value
}))
const collapsedTaskGroups = ref<Set<string>>(new Set())
const taskGroups = computed<TaskGroup[]>(() => {
  const groups = new Map<string, TaskGroup>()
  for (const task of visibleTasks.value) {
    const key = task.ownerId || 'UNASSIGNED'
    const group = groups.get(key)
    if (group) group.tasks.push(task)
    else groups.set(key, { key, label: task.ownerName || '未绑定用户', tasks: [task] })
  }
  return [...groups.values()]
})
function isTaskGroupExpanded(groupKey: string): boolean {
  return !collapsedTaskGroups.value.has(groupKey)
}
function toggleTaskGroup(groupKey: string): void {
  const next = new Set(collapsedTaskGroups.value)
  if (next.has(groupKey)) next.delete(groupKey)
  else next.add(groupKey)
  collapsedTaskGroups.value = next
}
const unassignedSessionCount = computed(() => sessions.value.filter((session) => !session.owner_id).length)

function mergeTaskUpdate(current: ProcessingTask, updated: ProcessingTask): ProcessingTask {
  return { ...updated, ownerId: updated.ownerId ?? current.ownerId, ownerName: updated.ownerName ?? current.ownerName, adminNote: updated.adminNote ?? current.adminNote }
}

async function autoProcessReadyTasks(items: ProcessingTask[]): Promise<void> {
  const readyItems = items.filter((task) => task.status === 'LOCAL_READY')
  if (!readyItems.length) return
  ApiClient.setAdminToken(adminToken.value)
  const updates = await Promise.all(readyItems.map(async (task) => {
    try {
      const result = await ApiClient.adminAutoProcessTask(task.id, task.claimedBy || workerId.value)
      return result.task
    } catch {
      return null
    }
  }))
  const updateMap = new Map(updates.filter((task): task is ProcessingTask => Boolean(task)).map((task) => [task.id, task]))
  if (updateMap.size) {
    tasks.value = tasks.value.map((task) => updateMap.has(task.id) ? mergeTaskUpdate(task, updateMap.get(task.id) as ProcessingTask) : task)
  }
}

function sessionDateBoundary(value: string, endOfDay = false): string {
  if (!value) return ''
  const date = new Date(`${value}T00:00:00`)
  if (Number.isNaN(date.getTime())) return ''
  if (endOfDay) date.setDate(date.getDate() + 1)
  return String(date.getTime())
}

type ReadableKnowledgeSection = { title: string; points: string[] }
type ReadableQuestion = { question: string; answer: string }

function resultRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function resultText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function resultTextList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => resultText(item)).filter(Boolean)
    : []
}

function resultReviewNotes(value: unknown): string[] {
  return resultTextList(value).filter((item) => !item.includes('当前使用本地自动提取草稿'))
}

function resultKnowledgeStructure(value: unknown): ReadableKnowledgeSection[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => {
    const section = resultRecord(item)
    return {
      title: resultText(section.title) || '知识要点',
      points: resultTextList(section.points),
    }
  }).filter((section) => section.points.length)
}

function resultQuestions(value: unknown): ReadableQuestion[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => {
    if (typeof item === 'string') return { question: item.trim(), answer: '' }
    const question = resultRecord(item)
    return {
      question: resultText(question.question),
      answer: resultText(question.answer),
    }
  }).filter((item) => item.question || item.answer)
}

const selectedPreviewResult = computed(() => {
  const revision = selectedRevisions.value.find((item) => item.id === selectedRevisionId.value)
  const document = resultRecord(revision?.result)
  const nestedSummary = resultRecord(document.summary)
  return Object.keys(nestedSummary).length ? nestedSummary : document
})

function controlError(cause: unknown, fallback: string): string {
  syncAdminAuthFromStorage()
  const raw = cause instanceof Error ? cause.message : fallback
  if (/管理员账号或密码错误/i.test(raw)) return '管理员账号或密码错误，请检查后再试。'
  if (/登录尝试过多/i.test(raw)) return '登录尝试过多，请稍后再试。'
  if (/管理员登录会话/i.test(raw)) return '管理员登录已失效，请重新登录。'
  if (/管理员账号已经设置|不能重复初始化/i.test(raw)) return '管理员账号已经设置，请直接登录。'
  if (/仅允许在本机执行/i.test(raw)) return '管理员初始化只能在运行服务器的电脑上完成。'
  if (/缺少有效 API Key/i.test(raw)) return '网页暂时没有连接到服务器，请刷新页面后重试。'
  if (/\(401\)|\b401\b|凭证无效|凭证尚未配置/i.test(raw)) return '管理员登录信息无效，请到“设置”重新登录。'
  if (/\(403\)|\b403\b|没有执行此操作的权限/i.test(raw)) return '当前管理员没有执行此操作的权限。'
  if (/\(409\)|\b409\b|状态刚刚发生变化/i.test(raw)) return '任务状态刚刚发生变化，请刷新后再试。'
  return raw
}

function syncAdminAuthFromStorage(): void {
  const storedSession = sessionStorage.getItem('livenote-admin-session') || ''
  const storedToken = sessionStorage.getItem('livenote-admin-token') || ''
  if (storedSession || storedToken) return
  if (!adminSession.value && !adminToken.value) return
  adminSession.value = ''
  adminToken.value = ''
  view.value = 'settings'
}

function isAdminAuthFailure(cause: unknown): boolean {
  const raw = cause instanceof Error ? cause.message : ''
  return (cause instanceof ApiRequestError && [401, 503].includes(cause.status)) || /管理员登录会话|凭证无效|凭证尚未配置|缺少管理员登录会话/i.test(raw)
}

function saveAdminSession(sessionToken: string): void {
  ApiClient.setAdminSession(sessionToken)
  sessionStorage.setItem('livenote-admin-username', adminUsername.value.trim())
  adminSession.value = sessionToken
}

async function setupAdminAndLogin(): Promise<void> {
  if (setupPassword.value !== setupPasswordConfirm.value) {
    error.value = '两次输入的密码不一致。'
    return
  }
  loading.value = true; error.value = ''; message.value = ''
  try {
    const result = await ApiClient.adminSetup(adminUsername.value, setupPassword.value)
    saveAdminSession(result.sessionToken)
    setupPassword.value = ''
    setupPasswordConfirm.value = ''
    adminSetupAvailable.value = false
    await refresh()
    message.value = '管理员账号已设置，当前已登录。'
  } catch (cause) {
    error.value = controlError(cause, '管理员初始化失败。')
  } finally {
    loading.value = false
  }
}

async function loginAdminAndRefresh(): Promise<void> {
  loading.value = true; error.value = ''; message.value = ''
  try {
    const result = await ApiClient.adminLogin(adminUsername.value, adminPassword.value)
    saveAdminSession(result.sessionToken)
    adminPassword.value = ''
    await refresh()
  } catch (cause) {
    error.value = controlError(cause, '管理员登录失败。')
  } finally {
    loading.value = false
  }
}

function logoutAdmin(): void {
  ApiClient.clearAdminSession()
  sessionStorage.removeItem('livenote-admin-token')
  adminSession.value = ''
  adminToken.value = ''
  adminPassword.value = ''
  setupPassword.value = ''
  setupPasswordConfirm.value = ''
  view.value = 'settings'
  error.value = ''
  message.value = '已退出管理员登录。'
}

function saveWorkerId(): void { workerId.value = workerId.value.trim() || 'local-pc'; localStorage.setItem('livenote-worker-id', workerId.value) }
function saveWorkerToken(): void { workerToken.value = workerToken.value.trim(); ApiClient.setWorkerToken(workerToken.value) }
async function refresh(): Promise<void> {
  loading.value = true; error.value = ''; message.value = ''
  try {
    try {
      const health = await ApiClient.health()
      storageOnly.value = health.capabilities?.storageOnly === true || health.capabilities?.processingMode === 'storage'
    } catch {
      storageOnly.value = false
    }
    if (!adminAuthenticated.value) {
      error.value = adminSetupAvailable.value ? '' : '尚未完成管理员登录，请在上方输入账号和密码，然后点击“登录并验证”。'
      return
    }
    ApiClient.setAdminToken(adminToken.value)
    try { localPullAvailable.value = !storageOnly.value && (await ApiClient.adminLocalWorker()).enabled } catch { localPullAvailable.value = false }
    if (view.value === 'tasks') {
      const result = await ApiClient.adminListTasks('ALL', 0, 200)
      tasks.value = result.items
      taskTotal.value = result.total
    }
    if (view.value === 'sessions') {
      const [sessionResult, userResult] = await Promise.all([
        ApiClient.adminListSessions(sessionQuery.value, '', 0, 100, sessionOwnerId.value, sessionDateBoundary(sessionDateFrom.value), sessionDateBoundary(sessionDateTo.value, true)),
        ApiClient.adminListUsers('', 0, 100),
      ])
      sessions.value = sessionResult.items
      sessionTotal.value = sessionResult.total
      users.value = userResult.items
    }
    if (view.value === 'users') users.value = (await ApiClient.adminListUsers(userQuery.value, 0, 100)).items
  } catch (cause) {
    if (isAdminAuthFailure(cause)) ApiClient.clearAdminAuth()
    error.value = controlError(cause, '无法读取服务器数据。')
  } finally { loading.value = false }
}
async function refreshTasksSilently(): Promise<void> {
  if (view.value !== 'tasks' || loading.value || requestingTaskId.value) return
  try {
    ApiClient.setAdminToken(adminToken.value)
    const result = await ApiClient.adminListTasks('ALL', 0, 200)
    const firstPageIds = new Set(result.items.map((task) => task.id))
    const nextTasks = [...result.items, ...tasks.value.filter((task) => !firstPageIds.has(task.id))]
    tasks.value = nextTasks
    taskTotal.value = result.total
  } catch (cause) {
    if (isAdminAuthFailure(cause)) {
      ApiClient.clearAdminAuth()
      syncAdminAuthFromStorage()
      error.value = controlError(cause, '管理员登录已失效，请重新登录。')
    }
    // Automatic polling must not replace the last visible task list or flash
    // an error while the operator is working. Manual refresh still reports errors.
  }
}
async function loadMoreTasks(): Promise<void> {
  if (taskLoadingMore.value || tasks.value.length >= taskTotal.value) return
  taskLoadingMore.value = true
  try {
    ApiClient.setAdminToken(adminToken.value)
    const result = await ApiClient.adminListTasks('ALL', tasks.value.length, 200)
    const existingIds = new Set(tasks.value.map((task) => task.id))
    tasks.value = [...tasks.value, ...result.items.filter((task) => !existingIds.has(task.id))]
    taskTotal.value = result.total
  } catch (cause) {
    error.value = controlError(cause, '加载更多任务失败。')
  } finally { taskLoadingMore.value = false }
}
async function loadMoreSessions(): Promise<void> {
  if (sessionLoadingMore.value || sessions.value.length >= sessionTotal.value) return
  sessionLoadingMore.value = true
  try {
    ApiClient.setAdminToken(adminToken.value)
    const result = await ApiClient.adminListSessions(sessionQuery.value, '', sessions.value.length, 100, sessionOwnerId.value, sessionDateBoundary(sessionDateFrom.value), sessionDateBoundary(sessionDateTo.value, true))
    const existingIds = new Set(sessions.value.map((session) => String(session.id)))
    sessions.value = [...sessions.value, ...result.items.filter((session) => !existingIds.has(String(session.id)))]
    sessionTotal.value = result.total
  } catch (cause) {
    error.value = controlError(cause, '加载更多会话失败。')
  } finally { sessionLoadingMore.value = false }
}
async function pullTaskToLocal(task: ProcessingTask): Promise<void> {
  if (requestingTaskId.value) return
  saveWorkerId(); ApiClient.setAdminToken(adminToken.value); error.value = ''; message.value = ''; requestingTaskId.value = task.id
  try {
    const result = await ApiClient.adminPullTaskLocal(task.id, workerId.value)
    let updatedTask = result.task
    let deferred = false
    if (updatedTask.status === 'LOCAL_READY') {
      const autoResult = await ApiClient.adminAutoProcessTask(task.id, workerId.value)
      updatedTask = autoResult.task
      deferred = Boolean(autoResult.deferred)
    }
    tasks.value = tasks.value.map((item) => item.id === task.id ? mergeTaskUpdate(item, updatedTask) : item)
    await refresh()
    message.value = deferred
      ? `已领取：${task.title || '未命名会话'}。实时识别收尾后会自动继续处理。`
      : `已领取并开始自动处理：${task.title || '未命名会话'}。完成后只需审核并发布。`
  } catch (cause) { error.value = controlError(cause, '领取或自动处理失败。') }
  finally { requestingTaskId.value = null }
}
async function pullAllReadyToLocal(): Promise<void> {
  if (!localPullAvailable.value || bulkPulling.value || readyTaskCount.value < 2) return
  saveWorkerId(); ApiClient.setAdminToken(adminToken.value)
  error.value = ''; message.value = ''; bulkPulling.value = true
  const candidates = tasks.value.filter((task) => task.status === 'READY')
  const failures: string[] = []
  let deferredCount = 0
  try {
    for (const task of candidates) {
      try {
        const result = await ApiClient.adminPullTaskLocal(task.id, workerId.value)
        let updatedTask = result.task
        if (updatedTask.status === 'LOCAL_READY') {
          const autoResult = await ApiClient.adminAutoProcessTask(task.id, workerId.value)
          updatedTask = autoResult.task
          if (autoResult.deferred) deferredCount += 1
        }
        tasks.value = tasks.value.map((item) => item.id === task.id ? mergeTaskUpdate(item, updatedTask) : item)
      } catch {
        failures.push(task.title || task.sessionId)
      }
    }
    await refresh()
    message.value = failures.length
      ? `已自动处理 ${candidates.length - failures.length} 个任务；${failures.length} 个任务失败，可单独重试。`
      : deferredCount
        ? `已领取 ${candidates.length} 个任务，其中 ${deferredCount} 个会在实时识别收尾后自动继续。`
        : `已领取并开始自动处理全部 ${candidates.length} 个待处理任务。`
    if (failures.length) error.value = `领取失败：${failures.join('、')}`
  } finally {
    bulkPulling.value = false
  }
}
async function retryTask(task: ProcessingTask): Promise<void> {
  if (requestingTaskId.value) return
  requestingTaskId.value = task.id; error.value = ''; message.value = ''
  try {
    ApiClient.setAdminToken(adminToken.value)
    await ApiClient.adminRetryTask(task.id)
    await refresh()
    message.value = '失败任务已重置，Worker 会自动领取。'
  } catch (cause) { error.value = controlError(cause, '重试任务失败。') }
  finally { requestingTaskId.value = null }
}
async function writeWorkerFile(directory: WorkerDirectoryHandle, name: string, content: Blob | string): Promise<void> {
  const file = await directory.getFileHandle(name, { create: true })
  const writable = await file.createWritable()
  await writable.write(content)
  await writable.close()
}
function downloadWorkerAudio(task: ProcessingTask, blob: Blob): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${task.id}-${task.sessionId}.webm`
  anchor.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
}
async function pollBrowserWorker(): Promise<void> {
  if (!browserWorkerRunning.value || browserWorkerBusy.value || !workerToken.value.trim()) return
  browserWorkerBusy.value = true
  browserWorkerError.value = ''
  try {
    const response = await ApiClient.workerListReady(workerToken.value.trim())
    const task = response.tasks.find((item) => !item.requestedWorkerId || item.requestedWorkerId === workerId.value)
    if (!task) {
      browserWorkerMessage.value = '电脑 Worker 已运行，暂时没有分配给本机的新任务。'
      return
    }
    const claimed = await ApiClient.workerClaimTask(task.id, workerId.value, workerToken.value.trim())
    try {
      const audio = await ApiClient.workerDownloadTaskAudio(task.id, workerId.value, claimed.leaseToken, workerToken.value.trim())
      if (browserWorkerInbox) {
        const taskDirectory = await browserWorkerInbox.getDirectoryHandle(task.id, { create: true })
        await writeWorkerFile(taskDirectory, 'audio.webm', audio)
        await writeWorkerFile(taskDirectory, 'task.json', JSON.stringify({ task: claimed.task, downloadedAt: Date.now() }, null, 2))
        browserWorkerMessage.value = `已自动领取并保存：${task.title || task.sessionId}。目录：${task.id}`
      } else {
        downloadWorkerAudio(task, audio)
        browserWorkerMessage.value = `已自动领取：${task.title || task.sessionId}。浏览器已开始下载音频，请保留下载文件。`
      }
      await ApiClient.workerUpdateTaskStatus(task.id, 'LOCAL_READY' as ProcessingTaskStatus, workerId.value, claimed.leaseToken, workerToken.value.trim())
      await refresh()
    } catch (cause) {
      await ApiClient.workerUpdateTaskStatus(task.id, 'FAILED' as ProcessingTaskStatus, workerId.value, claimed.leaseToken, workerToken.value.trim()).catch(() => undefined)
      throw cause
    }
  } catch (cause) {
    browserWorkerError.value = cause instanceof Error ? cause.message : '电脑 Worker 拉取失败。'
  } finally {
    browserWorkerBusy.value = false
  }
}
async function startBrowserWorker(): Promise<void> {
  if (browserWorkerRunning.value) return
  if (storageOnly.value) {
    browserWorkerError.value = '当前是 ECS 存储模式，请在本地运行 Worker 和 Codex Bridge。'
    return
  }
  saveWorkerId(); saveWorkerToken()
  if (!workerToken.value) {
    browserWorkerError.value = '请先填写 Worker 凭证。'
    return
  }
  browserWorkerError.value = ''
  browserWorkerMessage.value = ''
  browserWorkerInbox = null
  const picker = (window as WorkerWindow).showDirectoryPicker
  if (picker) {
    try {
      browserWorkerInbox = await picker()
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') return
      browserWorkerError.value = '无法打开电脑任务目录，将改用浏览器下载。'
    }
  }
  browserWorkerRunning.value = true
  browserWorkerMessage.value = browserWorkerInbox ? '电脑 Worker 已启动，正在监视服务器任务。' : '电脑 Worker 已启动，将通过浏览器下载任务音频。'
  await pollBrowserWorker()
  browserWorkerTimer = window.setInterval(() => { void pollBrowserWorker() }, 15_000)
}
function stopBrowserWorker(): void {
  browserWorkerRunning.value = false
  browserWorkerInbox = null
  if (browserWorkerTimer !== null) window.clearInterval(browserWorkerTimer)
  browserWorkerTimer = null
  browserWorkerMessage.value = '电脑 Worker 已停止。'
}
async function togglePreview(task: ProcessingTask): Promise<void> {
  if (selectedTaskId.value === task.id) {
    closePreview()
    return
  }
  clearOriginalAudio()
  selectedTaskId.value = task.id
  selectedRevisions.value = []
  selectedRevisionId.value = null
  try {
    ApiClient.setAdminToken(adminToken.value)
    const revisions = (await ApiClient.adminTaskResults(task.id)).items
    if (selectedTaskId.value !== task.id) return
    selectedRevisions.value = revisions
    selectedRevisionId.value = selectedRevisions.value[0]?.id ?? null
  } catch (cause) {
    if (selectedTaskId.value !== task.id) return
    closePreview()
    error.value = controlError(cause, '无法读取处理草稿。')
  }
}
function closePreview(): void {
  clearOriginalAudio()
  selectedTaskId.value = null
  selectedRevisions.value = []
  selectedRevisionId.value = null
}
async function toggleTranscript(task: ProcessingTask): Promise<void> {
  if (selectedTranscriptTaskId.value === task.id) {
    closeTranscript()
    return
  }
  selectedTranscriptTaskId.value = task.id
  selectedTranscript.value = null
  transcriptError.value = ''
  transcriptLoadingTaskId.value = task.id
  try {
    ApiClient.setAdminToken(adminToken.value)
    const response: AdminTranscriptResponse = await ApiClient.adminGetTaskTranscript(task.id)
    if (selectedTranscriptTaskId.value !== task.id) return
    selectedTranscript.value = response.transcript
  } catch (cause) {
    if (selectedTranscriptTaskId.value !== task.id) return
    transcriptError.value = controlError(cause, '无法读取识别文字。')
  } finally {
    if (transcriptLoadingTaskId.value === task.id) transcriptLoadingTaskId.value = null
  }
}
function closeTranscript(): void {
  selectedTranscriptTaskId.value = null
  selectedTranscript.value = null
  transcriptLoadingTaskId.value = null
  transcriptError.value = ''
}
function beginTaskNoteEdit(task: ProcessingTask): void {
  editingNoteTaskId.value = task.id
  taskNoteDraft.value = task.adminNote || ''
  error.value = ''
}
function cancelTaskNoteEdit(): void {
  editingNoteTaskId.value = null
  taskNoteDraft.value = ''
}
async function saveTaskNote(task: ProcessingTask): Promise<void> {
  if (savingNoteTaskId.value) return
  savingNoteTaskId.value = task.id
  error.value = ''
  try {
    ApiClient.setAdminToken(adminToken.value)
    const result = await ApiClient.adminUpdateTaskNote(task.id, taskNoteDraft.value)
    tasks.value = tasks.value.map((item) => item.id === task.id ? { ...item, adminNote: result.adminNote } : item)
    cancelTaskNoteEdit()
    message.value = '任务备注已保存。'
  } catch (cause) {
    error.value = controlError(cause, '任务备注保存失败。')
  } finally {
    savingNoteTaskId.value = null
  }
}
function clearOriginalAudio(): void {
  if (originalAudioUrl.value) URL.revokeObjectURL(originalAudioUrl.value)
  originalAudioTaskId.value = null
  originalAudioUrl.value = null
  originalAudioLoadingTaskId.value = null
  originalAudioError.value = ''
}
async function playOriginalAudio(task: ProcessingTask): Promise<void> {
  if (originalAudioLoadingTaskId.value === task.id) return
  if (originalAudioTaskId.value === task.id && originalAudioUrl.value) return
  clearOriginalAudio()
  originalAudioTaskId.value = task.id
  originalAudioLoadingTaskId.value = task.id
  try {
    ApiClient.setAdminToken(adminToken.value)
    const audio = await ApiClient.adminDownloadTaskAudio(task.id)
    if (selectedTaskId.value !== task.id) return
    originalAudioUrl.value = URL.createObjectURL(audio)
  } catch (cause) {
    if (selectedTaskId.value === task.id) originalAudioError.value = controlError(cause, '原录音暂不可用，请稍后再试。')
  } finally {
    if (originalAudioLoadingTaskId.value === task.id) originalAudioLoadingTaskId.value = null
  }
}
function tryPlayOriginalAudio(event: Event): void {
  const audio = event.target
  if (audio instanceof HTMLAudioElement) void audio.play().catch(() => undefined)
}
async function publishTask(task: ProcessingTask, revisionIdOverride: string | null = null): Promise<void> {
  if (publishingTaskId.value) return
  publishingTaskId.value = task.id
  try {
    ApiClient.setAdminToken(adminToken.value)
    let revisionId = revisionIdOverride
    if (!revisionId && selectedTaskId.value === task.id) revisionId = selectedRevisionId.value
    if (!revisionId) revisionId = (await ApiClient.adminTaskResults(task.id)).items[0]?.id ?? null
    if (!revisionId) throw new Error('没有可发布的总结草稿。')
    await ApiClient.adminPublish(task.id, revisionId)
    message.value = '审核已通过，手机将在下一次刷新时拉取。'
    await refresh()
  } catch (cause) { error.value = controlError(cause, '发布失败。') }
  finally { publishingTaskId.value = null }
}
async function createUser(): Promise<void> {
  if (!newUserName.value.trim()) return
  try {
    ApiClient.setAdminToken(adminToken.value)
    const created = await ApiClient.adminCreateUser(newUserName.value.trim())
    const pairing = await ApiClient.adminCreatePairingCode(created.id)
    newUserName.value = ''; message.value = `用户已创建，配对码：${pairing.code}（${new Date(pairing.expiresAt).toLocaleTimeString()} 前有效）`; await refresh()
  } catch (cause) { error.value = controlError(cause, '创建用户失败。') }
}
async function createPairing(user: AdminUser): Promise<void> {
  try { ApiClient.setAdminToken(adminToken.value); const pairing = await ApiClient.adminCreatePairingCode(user.id); message.value = `配对码：${pairing.code}（${new Date(pairing.expiresAt).toLocaleTimeString()} 前有效）` }
  catch (cause) { error.value = controlError(cause, '生成配对码失败。') }
}
function beginBulkAssignSessions(): void {
  if (sessionOwnerId.value !== 'UNASSIGNED' || !bulkOwnerId.value || bulkAssigning.value) return
  bulkAssignConfirming.value = true
}
function cancelBulkAssignSessions(): void {
  if (bulkAssigning.value) return
  bulkAssignConfirming.value = false
}
async function confirmBulkAssignSessions(): Promise<void> {
  if (!bulkOwnerId.value || bulkAssigning.value) return
  bulkAssigning.value = true
  error.value = ''
  message.value = ''
  try {
    ApiClient.setAdminToken(adminToken.value)
    const result = await ApiClient.adminAssignSessionsOwner(bulkOwnerId.value, true)
    bulkAssignConfirming.value = false
    await refresh()
    message.value = result.updatedCount ? `已将 ${result.updatedCount} 个未绑定 Session 归属到所选用户。` : '没有找到需要归属的未绑定 Session。'
  } catch (cause) {
    error.value = controlError(cause, '批量归属 Session 失败。')
  } finally {
    bulkAssigning.value = false
  }
}
function deleteSession(session: Record<string, unknown>): void {
  const sessionId = String(session.id || '')
  if (sessionId) {
    pendingDeleteSessionId.value = sessionId
    deleteError.value = ''
  }
}
function beginEditSession(session: Record<string, unknown>): void {
  const sessionId = String(session.id || '')
  if (!sessionId) return
  editingSessionId.value = sessionId
  editingSessionTitle.value = String(session.title || '')
  editingSessionOwnerId.value = String(session.owner_id || '')
  deleteError.value = ''
}
function cancelEditSession(): void {
  if (savingSessionId.value) return
  editingSessionId.value = null
  editingSessionTitle.value = ''
  editingSessionOwnerId.value = ''
}
async function saveSessionEdit(session: Record<string, unknown>): Promise<void> {
  const sessionId = String(session.id || '')
  const title = editingSessionTitle.value.trim()
  if (!sessionId || editingSessionId.value !== sessionId || savingSessionId.value || !title) {
    if (!title) error.value = '会话标题不能为空。'
    return
  }
  savingSessionId.value = sessionId
  error.value = ''
  try {
    ApiClient.setAdminToken(adminToken.value)
    const patch: { title: string; ownerId?: string } = { title }
    if (editingSessionOwnerId.value) patch.ownerId = editingSessionOwnerId.value
    await ApiClient.adminUpdateSession(sessionId, patch)
    await refresh()
    savingSessionId.value = null
    cancelEditSession()
    message.value = '会话信息已更新。'
  } catch (cause) {
    error.value = controlError(cause, '会话信息更新失败。')
  } finally {
    savingSessionId.value = null
  }
}
function cancelDeleteSession(): void {
  if (deletingSessionId.value) return
  pendingDeleteSessionId.value = null
  deleteError.value = ''
}
async function confirmDeleteSession(session: Record<string, unknown>): Promise<void> {
  const sessionId = String(session.id || '')
  if (!sessionId || pendingDeleteSessionId.value !== sessionId || deletingSessionId.value) return
  deletingSessionId.value = sessionId
  deleteError.value = ''
  error.value = ''
  try {
    ApiClient.setAdminToken(adminToken.value)
    const result = await ApiClient.adminDeleteSession(sessionId)
    await refresh()
    pendingDeleteSessionId.value = null
    message.value = `已删除会话，清理 ${result.segments} 段、${result.chunks} 个音频块。`
  } catch (cause) {
    deleteError.value = cause instanceof Error ? cause.message : '删除会话失败。'
  } finally {
    deletingSessionId.value = null
  }
}
async function interruptSession(session: Record<string, unknown>): Promise<void> {
  const sessionId = String(session.id || '')
  if (!sessionId || interruptingSessionId.value) return
  interruptingSessionId.value = sessionId
  error.value = ''
  message.value = ''
  try {
    ApiClient.setAdminToken(adminToken.value)
    const result = await ApiClient.adminInterruptSession(sessionId)
    await refresh()
    message.value = `已结束遗留录音，保留原有音频和 ${formatDuration(result.durationMs)} 录音数据。`
  } catch (cause) {
    error.value = controlError(cause, '结束遗留录音失败。')
  } finally {
    interruptingSessionId.value = null
  }
}
function formatDuration(ms: number | null): string { const totalSeconds = Math.floor(Math.max(0, ms || 0) / 1000); return `${Math.floor(totalSeconds / 60).toString().padStart(2, '0')}:${(totalSeconds % 60).toString().padStart(2, '0')}` }
function formatTranscriptTime(ms: number): string { const totalSeconds = Math.floor(Math.max(0, ms || 0) / 1000); return `${Math.floor(totalSeconds / 60).toString().padStart(2, '0')}:${(totalSeconds % 60).toString().padStart(2, '0')}` }
function statusLabel(status: string): string { return { READY: '排队中', CLAIMED: '已领取', LOCAL_READY: '已就绪', TRANSCRIBING: '正在识别', TRANSCRIBED: '已识别，待总结', SUMMARIZING: '正在生成总结', PROCESSING: '处理中', REVIEW: '待发布', READY_TO_UPLOAD: '待回传', COMPLETED: '已发布', FAILED: '失败' }[status] || status }
function taskStatusLabel(task: ProcessingTask): string {
  if (task.status === 'READY') return '等待本地 Worker 自动领取'
  return statusLabel(task.status)
}
function taskStatusNote(status: string): string {
  if (status === 'TRANSCRIBED') return '下一步：复制指令到 Codex'
  if (status === 'SUMMARIZING') return '正在生成总结'
  return '系统自动处理'
}
function summaryPrompt(task: ProcessingTask): string {
  const recordedAt = new Date(task.createdAt).toLocaleString('zh-CN')
  return [
    '请总结 LiveNote 中指定的这一条录音，不要处理其他会话。',
    `任务编号：${task.id}`,
    `用户：${task.ownerName || '未绑定用户'}`,
    `录音标题：${task.title || '未命名会话'}`,
    `录音时间：${recordedAt}`,
    `录音时长：${formatDuration(task.durationMs)}`,
    '',
    '请读取这条任务对应的已完成逐字稿，提取并整理：',
    '1. 主题',
    '2. 关键知识点',
    '3. 重要问答',
    '4. 行动建议',
    '',
    '不要读取或总结其他会话。完成后将总结结果回填到对应的 LiveNote 任务。',
  ].join('\n')
}
async function copySummaryPrompt(task: ProcessingTask): Promise<void> {
  if (summaryPromptBusyTaskId.value) return
  summaryPromptBusyTaskId.value = task.id
  error.value = ''
  try {
    await navigator.clipboard.writeText(summaryPrompt(task))
    summaryPromptTaskId.value = task.id
  } catch (cause) {
    error.value = controlError(cause, '总结指令复制失败，请重试。')
  } finally {
    summaryPromptBusyTaskId.value = null
  }
}
function canPreviewTask(task: ProcessingTask): boolean {
  return ['REVIEW', 'COMPLETED'].includes(task.status)
}
function canViewTranscript(task: ProcessingTask): boolean {
  return ['TRANSCRIBED', 'SUMMARIZING', 'REVIEW', 'COMPLETED'].includes(task.status)
}
function switchView(next: View): void { view.value = next; void refresh() }
async function initializeControl(): Promise<void> {
  try {
    adminSetupAvailable.value = (await ApiClient.adminAuthStatus()).setupAvailable
  } catch {
    adminSetupAvailable.value = false
  }
  if (adminSetupAvailable.value) {
    ApiClient.clearAdminSession()
    sessionStorage.removeItem('livenote-admin-token')
    adminSession.value = ''
    adminToken.value = ''
  }
  if (!adminAuthenticated.value) view.value = 'settings'
  await refresh()
  taskRefreshTimer = window.setInterval(() => { void refreshTasksSilently() }, 15_000)
}
onMounted(() => { void initializeControl() })
onBeforeUnmount(() => {
  clearOriginalAudio()
  stopBrowserWorker()
  if (taskRefreshTimer !== null) window.clearInterval(taskRefreshTimer)
  taskRefreshTimer = null
})
</script>

<template>
  <main class="control-console">
    <header class="control-header"><div><h1>内容处理台</h1><p>管理录音任务、电脑端处理和手机端发布。</p></div><button class="secondary-button" type="button" :disabled="loading" @click="refresh">{{ loading ? '刷新中…' : '刷新' }}</button></header>
    <nav class="control-tabs" aria-label="管理模块"><button v-for="item in ([['tasks', '任务'], ['users', '用户'], ['settings', '设置']] as Array<[View, string]>)" :key="item[0]" type="button" :class="{ active: view === item[0] }" @click="switchView(item[0])">{{ item[1] }}<small v-if="item[0] === 'tasks' && readyCount">({{ readyCount }})</small></button></nav>
    <section v-if="view === 'settings'" class="control-panel control-settings-panel">
      <div class="control-settings-heading">
        <div><h2>{{ adminAuthenticated ? '管理员访问' : adminSetupAvailable ? '首次设置管理员' : '管理员登录' }}</h2><p>{{ adminAuthenticated ? '当前浏览器已获得管理权限。' : adminSetupAvailable ? '第一次使用时，在这里设置以后登录管理台的账号和密码。' : '用管理员账号和密码进入电脑管理页面。' }}</p></div>
        <span v-if="adminAuthenticated" class="control-auth-badge"><i aria-hidden="true"></i>当前会话有效</span>
      </div>
      <div v-if="adminAuthenticated" class="control-admin-session">
        <div class="control-session-identity"><span class="control-session-label">当前账号</span><strong>{{ adminUsername }}</strong><small>仅对这台电脑的当前浏览器生效</small></div>
        <button class="control-logout-button" type="button" @click="logoutAdmin"><span>退出登录</span></button>
      </div>
      <div v-else-if="adminSetupAvailable" class="control-settings-primary control-admin-login">
        <label class="control-credential-field"><span>管理员账号</span><input v-model="adminUsername" type="text" autocomplete="username" placeholder="例如：admin" /></label>
        <label class="control-credential-field"><span>设置密码</span><input v-model="setupPassword" type="password" autocomplete="new-password" placeholder="至少 8 位" /></label>
        <label class="control-credential-field"><span>确认密码</span><input v-model="setupPasswordConfirm" type="password" autocomplete="new-password" placeholder="再次输入密码" @keyup.enter="setupAdminAndLogin" /></label>
        <div class="control-settings-actions"><button class="primary-button compact-button" type="button" :disabled="loading || !adminUsername.trim() || setupPassword.length < 8 || setupPassword !== setupPasswordConfirm" @click="setupAdminAndLogin">保存并进入管理台</button><span class="control-settings-hint">密码只保存为加密哈希</span></div>
      </div>
      <div v-else class="control-settings-primary control-admin-login">
        <label class="control-credential-field"><span>管理员账号</span><input v-model="adminUsername" type="text" autocomplete="username" placeholder="例如：admin" /></label>
        <label class="control-credential-field"><span>管理员密码</span><input v-model="adminPassword" type="password" autocomplete="current-password" placeholder="请输入管理员密码" @keyup.enter="loginAdminAndRefresh" /></label>
        <div class="control-settings-actions"><button class="primary-button compact-button" type="button" :disabled="loading || !adminUsername.trim() || !adminPassword" @click="loginAdminAndRefresh">登录并验证</button><span class="control-settings-hint">密码不会保存在浏览器中</span></div>
      </div>
      <details class="control-optional-settings">
        <summary>Worker 设置 <span>{{ localPullAvailable ? '本机模式' : '云端模式' }}</span></summary>
        <div class="control-optional-settings-body">
           <div class="control-mode-summary"><strong>当前模式</strong><p v-if="storageOnly">ECS 存储模式：本地电脑运行 Worker 下载并识别，Codex Bridge 自动生成总结；此页面只负责查看、审核和发布。</p><p v-else-if="localPullAvailable">本机模式：任务可以直接保存到这台电脑，不需要填写 Worker 凭证。</p><p v-else>云端模式：需要 Worker 凭证，才能让这台电脑自动领取服务器上的任务。</p></div>
           <div v-if="!localPullAvailable && !storageOnly" class="control-browser-worker"><label><span>Worker 凭证 <em>云端必填</em></span><input v-model="workerToken" type="password" autocomplete="off" placeholder="粘贴服务器的 LIVENOTE_WORKER_TOKEN" @change="saveWorkerToken" /></label><div class="control-worker-actions"><button v-if="!browserWorkerRunning" class="primary-button compact-button" type="button" @click="startBrowserWorker">启动电脑自动领取</button><button v-else class="secondary-button compact-button" type="button" @click="stopBrowserWorker">停止电脑自动领取</button><span v-if="browserWorkerRunning" class="control-status control-status-processing">运行中</span></div><p v-if="browserWorkerMessage" class="control-message">{{ browserWorkerMessage }}</p><p v-if="browserWorkerError" class="control-error">{{ browserWorkerError }}</p></div>
        </div>
      </details>
    </section>
    <p v-if="message" class="control-message">{{ message }}</p><p v-if="error" class="control-error">{{ error }}<button v-if="view !== 'settings' && /管理员凭证|管理员登录|请先登录/.test(error)" class="text-button compact-button control-error-action" type="button" @click="switchView('settings')">打开登录区域</button></p>
    <section v-if="view === 'tasks'" class="control-panel">
      <div class="control-section-heading">
        <div><h2>处理任务</h2><p>{{ visibleTasks.length }} 个符合筛选 · 共 {{ taskTotal || tasks.length }} 个任务</p></div>
        <div class="control-task-toolbar"><select v-model="taskFilter" class="control-task-filter" aria-label="任务筛选"><option value="ALL">全部记录</option><option value="ACTIONABLE">未完成</option><option value="READY">排队中</option><option value="PROCESSING">处理中</option><option value="TRANSCRIBED">待总结</option><option value="SUMMARIZING">总结中</option><option value="REVIEW">待发布</option><option value="FAILED">失败</option><option value="COMPLETED">历史记录</option></select><div class="control-flow"><span>自动识别</span><i>→</i><span>Codex 总结</span><i>→</i><span>发布</span></div></div>
      </div>
       <details v-if="storageOnly" class="control-processing-guide"><summary>ECS 存储模式处理说明</summary><ol><li>本地 Worker 自动从 ECS 下载并校验 Chunk，无需手动指定任务。</li><li>本地 Whisper 完成识别后，本地 Codex Bridge 自动生成总结。</li><li>这里查看总结，确认无误后审核并发布。</li></ol></details>
       <details v-else-if="!localPullAvailable" class="control-processing-guide"><summary>自动处理说明</summary><ol><li>任务进入队列后，本地 Worker 会自动领取，无需指定电脑。</li><li>Worker 完成识别后，Codex Bridge 会自动生成总结。</li><li>这里仅用于查看状态、审核和发布。</li></ol></details>
      <div v-if="visibleTasks.length" class="control-task-groups">
        <section v-for="group in taskGroups" :key="group.key" class="control-task-group">
          <header class="control-task-group-heading"><h3><button class="control-task-group-toggle" type="button" :aria-expanded="isTaskGroupExpanded(group.key)" :aria-controls="`task-group-${group.key}`" @click="toggleTaskGroup(group.key)"><span class="control-task-group-label"><svg class="control-task-group-chevron" :class="{ collapsed: !isTaskGroupExpanded(group.key) }" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg><span class="control-task-group-name">用户：{{ group.label }}</span></span><span class="control-task-group-count">{{ group.tasks.length }} 条任务<small>{{ isTaskGroupExpanded(group.key) ? '收起' : '展开' }}</small></span></button></h3></header>
          <div v-if="isTaskGroupExpanded(group.key)" :id="`task-group-${group.key}`" class="control-task-list">
        <article v-for="task in group.tasks" :key="task.id" class="control-task-row">
          <div class="control-task-main">
            <div class="control-task-title-line"><span class="control-task-title">{{ task.title || '未命名会话' }}</span><span v-if="task.sourceLabel" class="control-task-source">{{ task.sourceLabel }}</span><span class="control-task-duration">{{ formatDuration(task.durationMs) }}</span><span class="control-status control-task-inline-status" :class="'control-status-' + task.status.toLowerCase()">{{ taskStatusLabel(task) }}</span></div>
            <div class="control-task-meta-line"><span class="control-task-meta">{{ new Date(task.createdAt).toLocaleString() }}</span><div class="control-task-note"><button class="text-button compact-button" type="button" @click="beginTaskNoteEdit(task)">{{ task.adminNote ? '编辑备注' : '添加备注' }}</button><p v-if="task.adminNote" class="control-task-note-copy">备注：{{ task.adminNote }}</p></div></div>
            <div v-if="editingNoteTaskId === task.id" class="control-task-note-editor"><textarea v-model="taskNoteDraft" maxlength="2000" rows="3" placeholder="写下这条任务的处理备注，仅管理员可见"></textarea><div class="control-task-note-actions"><button class="primary-button compact-button" type="button" :disabled="savingNoteTaskId === task.id" @click="saveTaskNote(task)">{{ savingNoteTaskId === task.id ? '保存中…' : '保存备注' }}</button><button class="text-button compact-button" type="button" :disabled="savingNoteTaskId === task.id" @click="cancelTaskNoteEdit">取消</button></div></div>
            <div v-if="task.status === 'TRANSCRIBING' && task.progress" class="control-task-progress" :aria-label="`识别进度 ${task.progress.percent}%`">
              <div class="control-task-progress-label"><span>识别进度</span><strong>{{ task.progress.completedParts }}/{{ task.progress.totalParts }} 段 · {{ task.progress.percent }}%</strong></div>
              <div class="control-task-progress-track" role="progressbar" :aria-valuenow="task.progress.percent" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${task.progress.percent}%` }"></span></div>
            </div>
          </div>
          <div class="control-task-side">
            <div class="control-task-actions">
              <span v-if="task.status === 'REVIEW'" class="control-review-action-wrap"><button class="primary-button compact-button control-review-action" type="button" :aria-describedby="`review-tip-${task.id}`" aria-label="审核并发布，手机将自动拉取" :disabled="publishingTaskId === task.id" @click="publishTask(task)">{{ publishingTaskId === task.id ? '确认中…' : '审核并发布' }}</button><span :id="`review-tip-${task.id}`" class="control-review-tooltip" role="tooltip">审核通过后，手机将自动拉取</span></span>
              <template v-if="localPullAvailable && ['READY', 'TRANSCRIBING', 'TRANSCRIBED', 'SUMMARIZING'].includes(task.status)">
                <span v-if="task.status !== 'TRANSCRIBED'" class="control-task-auto-note">{{ taskStatusNote(task.status) }}</span>
                <button v-if="task.status === 'TRANSCRIBED'" class="primary-button compact-button" type="button" :aria-label="summaryPromptTaskId === task.id ? '已复制总结指令，请在 Codex 粘贴并发送' : '复制总结指令到 Codex'" :title="summaryPromptTaskId === task.id ? '已复制，请在 Codex 粘贴并发送' : '下一步：复制指令到 Codex'" :disabled="summaryPromptBusyTaskId === task.id" @click="copySummaryPrompt(task)">{{ summaryPromptBusyTaskId === task.id ? '复制中…' : summaryPromptTaskId === task.id ? '已复制，去 Codex 发送' : '复制总结指令' }}</button>
              </template>
              <button v-if="task.status === 'FAILED'" class="secondary-button compact-button" type="button" :disabled="requestingTaskId === task.id" @click="retryTask(task)">{{ requestingTaskId === task.id ? '重试中…' : '重试' }}</button>
              <button v-if="canViewTranscript(task)" class="secondary-button compact-button" type="button" :disabled="transcriptLoadingTaskId === task.id" @click="toggleTranscript(task)">{{ transcriptLoadingTaskId === task.id ? '读取中…' : selectedTranscriptTaskId === task.id ? '收起文字' : '查看文字' }}</button>
            </div>
          </div>
          <div v-if="canPreviewTask(task)" class="control-preview-disclosure" :class="{ open: selectedTaskId === task.id }">
            <button class="control-preview-trigger" type="button" :aria-label="selectedTaskId === task.id ? '收起知识预览' : '打开知识预览'" :aria-expanded="selectedTaskId === task.id" @click="togglePreview(task)">
              <span class="control-preview-trigger-copy"><strong>知识预览</strong></span><span class="control-preview-trigger-action"><svg class="control-preview-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg></span>
            </button>
            <div v-if="selectedTaskId === task.id" class="control-preview-panel">
              <section class="control-inline-preview">
                <p v-if="!selectedRevisions.length" class="control-preview-loading">正在读取总结…</p>
                <template v-if="selectedRevisions.length">
                  <div v-if="selectedRevisions.length > 1" class="control-preview-toolbar"><label class="control-revision-select">版本<select v-model="selectedRevisionId"><option v-for="revision in selectedRevisions" :key="revision.id" :value="revision.id">v{{ revision.version }} · {{ new Date(revision.createdAt).toLocaleString() }}</option></select></label><span class="control-preview-toolbar-note">选择版本查看内容，确认无误后让手机拉取</span></div>
                  <article class="control-readable-result"><h3>总结后的知识预览</h3><p v-if="resultText(selectedPreviewResult.overview)" class="control-readable-overview">{{ resultText(selectedPreviewResult.overview) }}</p><section v-if="resultKnowledgeStructure(selectedPreviewResult.knowledgeStructure).length" class="control-readable-section"><h4>知识结构</h4><div v-for="(section, index) in resultKnowledgeStructure(selectedPreviewResult.knowledgeStructure)" :key="`preview-structure-${index}`" class="control-readable-structure"><strong>{{ section.title }}</strong><ul class="control-readable-list"><li v-for="point in section.points" :key="point">{{ point }}</li></ul></div></section><section v-if="resultTextList(selectedPreviewResult.keyPoints).length" class="control-readable-section"><h4>关键知识点</h4><ul class="control-readable-list"><li v-for="point in resultTextList(selectedPreviewResult.keyPoints)" :key="point">{{ point }}</li></ul></section><section v-if="resultQuestions(selectedPreviewResult.questions).length" class="control-readable-section"><h4>重要问答</h4><div class="control-readable-questions"><p v-for="(item, index) in resultQuestions(selectedPreviewResult.questions)" :key="`preview-question-${index}`" class="control-readable-question"><strong v-if="item.question">问：</strong>{{ item.question }}<br v-if="item.question && item.answer" /><strong v-if="item.answer">答：</strong>{{ item.answer }}</p></div></section><section v-if="resultTextList(selectedPreviewResult.actionItems).length" class="control-readable-section"><h4>行动建议</h4><ul class="control-readable-list"><li v-for="item in resultTextList(selectedPreviewResult.actionItems)" :key="item">{{ item }}</li></ul></section><section class="control-readable-section control-readable-notes"><h4>发布前确认</h4><p>请先确认上方总结中的主题、知识点和行动建议与录音一致；确认无误后，点击任务行右侧“审核通过，待手机拉取”。</p><ul v-if="resultReviewNotes(selectedPreviewResult.confidenceNotes).length" class="control-readable-list"><li v-for="item in resultReviewNotes(selectedPreviewResult.confidenceNotes)" :key="item">{{ item }}</li></ul></section><p v-if="!resultText(selectedPreviewResult.overview) && !resultKnowledgeStructure(selectedPreviewResult.knowledgeStructure).length && !resultTextList(selectedPreviewResult.keyPoints).length && !resultQuestions(selectedPreviewResult.questions).length && !resultTextList(selectedPreviewResult.actionItems).length" class="empty-state">这份草稿没有可识别的正文内容。</p></article>
                  <div class="control-audio-preview"><div class="control-audio-preview-heading"><span>原录音</span><button class="secondary-button compact-button" type="button" :disabled="originalAudioLoadingTaskId === task.id" @click="playOriginalAudio(task)">{{ originalAudioLoadingTaskId === task.id ? '准备中…' : originalAudioUrl && originalAudioTaskId === task.id ? '已准备' : '试听原录音' }}</button></div><audio v-if="originalAudioUrl && originalAudioTaskId === task.id" :src="originalAudioUrl" controls preload="metadata" @canplay="tryPlayOriginalAudio"></audio><p v-if="originalAudioError && originalAudioTaskId === task.id" class="control-audio-preview-error">{{ originalAudioError }}</p></div>
                </template>
              </section>
            </div>
          </div>
          <div v-if="selectedTranscriptTaskId === task.id" class="control-transcript-disclosure">
            <section class="control-transcript-panel" aria-label="识别文字">
              <div class="control-transcript-heading"><div><strong>识别文字</strong><span v-if="selectedTranscript">{{ selectedTranscript.segments.length }} 段</span></div><button class="text-button compact-button" type="button" @click="closeTranscript">收起</button></div>
              <p v-if="transcriptLoadingTaskId === task.id" class="control-preview-loading">正在读取文字…</p>
              <p v-else-if="transcriptError" class="control-transcript-error">{{ transcriptError }}</p>
              <div v-else-if="selectedTranscript" class="control-transcript-content">
                <p v-for="segment in selectedTranscript.segments" :key="`${segment.index}-${segment.startMs}`" class="control-transcript-line"><time>{{ formatTranscriptTime(segment.startMs) }}</time><span>{{ segment.text }}</span></p>
                <p v-if="!selectedTranscript.segments.length && selectedTranscript.text" class="control-transcript-fallback">{{ selectedTranscript.text }}</p>
                <p v-if="!selectedTranscript.segments.length && !selectedTranscript.text" class="empty-state">这条录音没有可显示的识别文字。</p>
              </div>
            </section>
          </div>
        </article>
          </div>
        </section>
      </div>
      <p v-else class="empty-state">当前筛选没有任务。</p>
    </section>
    <section v-else-if="view === 'sessions'" class="control-panel control-sessions-panel">
      <div class="control-section-heading"><div><h2>会话</h2></div><div class="control-session-filters"><select v-model="sessionOwnerId" class="control-session-owner-filter" aria-label="按用户筛选" @change="refresh"><option value="">全部用户</option><option value="UNASSIGNED">未绑定用户</option><option v-for="user in users" :key="user.id" :value="user.id">{{ user.displayName || user.display_name }}</option></select><label class="control-date-filter"><span>从</span><input v-model="sessionDateFrom" type="date" aria-label="开始日期" @change="refresh" /></label><label class="control-date-filter"><span>到</span><input v-model="sessionDateTo" type="date" aria-label="结束日期" @change="refresh" /></label><input v-model="sessionQuery" class="control-inline-input" placeholder="搜索会话" @keyup.enter="refresh" /><button class="secondary-button compact-button" type="button" :disabled="loading" @click="refresh">筛选</button></div></div>
      <div v-if="sessionOwnerId === 'UNASSIGNED'" class="control-bulk-owner">
        <div><strong>批量归属</strong><span>当前筛选到 {{ sessionTotal }} 个未绑定 Session，可一次性归属给同一位用户。</span></div>
        <div class="control-bulk-owner-actions"><select v-model="bulkOwnerId" aria-label="批量归属目标用户"><option value="">选择目标用户</option><option v-for="user in users" :key="`bulk-${user.id}`" :value="user.id">{{ user.displayName || user.display_name }}</option></select><button class="secondary-button compact-button" type="button" :disabled="!bulkOwnerId || bulkAssigning" @click="beginBulkAssignSessions">批量归属</button></div>
        <div v-if="bulkAssignConfirming" class="control-bulk-owner-confirm"><span>确认将这 {{ sessionTotal }} 个未绑定 Session 归属给“{{ users.find((user) => user.id === bulkOwnerId)?.displayName || users.find((user) => user.id === bulkOwnerId)?.display_name }}”？</span><button class="primary-button compact-button" type="button" :disabled="bulkAssigning" @click="confirmBulkAssignSessions">{{ bulkAssigning ? '归属中…' : '确认' }}</button><button class="text-button compact-button" type="button" :disabled="bulkAssigning" @click="cancelBulkAssignSessions">取消</button></div>
      </div>
      <div v-if="sessions.length" class="control-task-list">
        <article v-for="session in sessions" :key="String(session.id)" class="control-task-row control-session-row">
          <div v-if="pendingDeleteSessionId === String(session.id)" class="control-delete-panel"><div class="control-delete-copy"><strong>删除会话</strong><span>{{ session.title || '未命名会话' }}</span><small>服务器上的音频、任务和处理结果都会一起删除，且无法恢复。</small><p v-if="deleteError" class="control-delete-error">{{ deleteError }}</p></div><div class="control-delete-actions"><button class="control-confirm-danger" type="button" :disabled="deletingSessionId === String(session.id)" @click="confirmDeleteSession(session)">{{ deletingSessionId === String(session.id) ? '删除中…' : '确认删除' }}</button><button class="control-confirm-cancel" type="button" :disabled="deletingSessionId === String(session.id)" @click="cancelDeleteSession">取消</button></div></div>
          <div v-else-if="editingSessionId === String(session.id)" class="control-session-edit">
            <label>标题<input v-model="editingSessionTitle" maxlength="200" /></label>
            <label>所属用户<select v-model="editingSessionOwnerId"><option value="">保持当前归属</option><option v-for="user in users" :key="user.id" :value="user.id">{{ user.displayName || user.display_name }}</option></select></label>
            <div class="control-session-edit-actions"><button class="primary-button compact-button" type="button" :disabled="savingSessionId === String(session.id)" @click="saveSessionEdit(session)">{{ savingSessionId === String(session.id) ? '保存中…' : '保存' }}</button><button class="text-button compact-button" type="button" :disabled="savingSessionId === String(session.id)" @click="cancelEditSession">取消</button></div>
          </div>
          <template v-else><div><strong>用户：{{ session.owner_name || session.owner_id || '未绑定用户' }}</strong><span>{{ session.title || '未命名会话' }} · {{ session.status }}</span></div><div class="control-task-actions"><span class="control-status">{{ session.task_status || '无任务' }}</span><button class="text-button compact-button" type="button" @click="beginEditSession(session)">编辑</button><button v-if="session.status === 'RECORDING'" class="text-button compact-button" type="button" :disabled="interruptingSessionId === String(session.id)" @click="interruptSession(session)">{{ interruptingSessionId === String(session.id) ? '结束中…' : '结束遗留录音' }}</button><button class="danger-text-button" type="button" @click="deleteSession(session)">删除</button></div></template>
        </article>
      </div>
      <p v-else class="empty-state">没有找到会话。</p>
    </section>
    <section v-else-if="view === 'users'" class="control-panel"><div class="control-section-heading"><div><h2>用户与设备</h2><p>先创建用户，再把配对码交给手机 PWA。</p></div><div class="control-create-row"><input v-model="newUserName" class="control-inline-input" placeholder="新用户名称" @keyup.enter="createUser" /><button class="primary-button compact-button" type="button" @click="createUser">创建并生成配对码</button></div></div><div v-if="users.length" class="control-task-list"><article v-for="user in users" :key="user.id" class="control-task-row"><div><strong>{{ user.displayName || user.display_name }}</strong><span>{{ user.session_count ?? 0 }} 场录音 · {{ user.device_count ?? 0 }} 台设备</span></div><button class="secondary-button compact-button" type="button" @click="createPairing(user)">生成配对码</button></article></div><p v-else class="empty-state">还没有正式用户。请先创建用户并绑定对应手机，录音上传后会自动归属。</p></section>
     <section v-else class="control-panel control-help"><h2>设置</h2><p>当前 API 地址：<code>{{ apiBase }}</code></p><p>电脑端目录：<code>worker-inbox/</code></p><p v-if="storageOnly">当前为 ECS 存储模式：本地 Worker 和 Codex Bridge 负责处理，网页不直接运行本地识别。</p><p v-else-if="localPullAvailable">本机领取已启用：任务会由当前服务器直接写入电脑端目录。</p><p v-else>本机领取未启用：云端需要家庭 PC Worker 常驻运行，负责自动下载任务。</p></section>
    <div v-if="view === 'tasks' && tasks.length < taskTotal" class="control-load-more"><button class="text-button compact-button" type="button" :disabled="taskLoadingMore" @click="loadMoreTasks">{{ taskLoadingMore ? '加载中…' : `加载更多任务（${tasks.length}/${taskTotal}）` }}</button></div>
    <div v-if="view === 'sessions' && sessions.length < sessionTotal" class="control-load-more"><button class="text-button compact-button" type="button" :disabled="sessionLoadingMore" @click="loadMoreSessions">{{ sessionLoadingMore ? '加载中…' : `加载更多会话（${sessions.length}/${sessionTotal}）` }}</button></div>
  </main>
</template>

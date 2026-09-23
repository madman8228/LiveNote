<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { AUDIO_PROFILES, type AudioProfileId } from './audio/AudioProfile'
import { detectAudioCapabilities, type AudioCapabilities } from './audio/AudioCapabilities'
import { AudioLevelMonitor } from './audio/AudioLevelMonitor'
import { MediaRecorderEngine } from './audio/MediaRecorderEngine'
import type { RecorderChunk, RecorderState } from './audio/RecorderEngine'
import { PageLifecycleManager, type LifecycleEvent } from './lifecycle/PageLifecycleManager'
import { WakeLockManager, type WakeLockState } from './lifecycle/WakeLockManager'
import { clearAllData, deleteSessionData, openDatabase } from './storage/db'
import { ChunkStore } from './storage/ChunkStore'
import { LifecycleStore } from './storage/LifecycleStore'
import { MarkerStore } from './storage/MarkerStore'
import { ResultStore } from './storage/ResultStore'
import { ResultDraftStore } from './storage/ResultDraftStore'
import { createSegmentPlayback, type SegmentPlayback } from './storage/SegmentRecovery'
import { createSessionPlayback, restoreSession, type SessionPlayback } from './storage/SessionRecovery'
import { SegmentStore } from './storage/SegmentStore'
import { SessionStore } from './storage/SessionStore'
import { sha256Blob } from './storage/sha256'
import type { ChunkMetadata, ChunkRecord, LifecycleEventRecord, MarkerRecord, MarkerType, PublishedResultRecord, SegmentRecord, SessionRecord } from './storage/types'
import { ApiClient, type ContentSummary, type LiveProcessingStatus, type ReportResponse, type ServerResultResponse, type TranscriptResponse } from './upload/ApiClient'
import { uploadQueue, type UploadQueueSnapshot } from './upload/UploadQueue'
import { ResultSyncManager } from './upload/ResultSync'
import { PwaInstallManager, type PwaInstallSnapshot } from './pwa/PwaInstallManager'
import { hasPlayableSession } from './playback/PlaybackAvailability'
import { playbackProgressPercent, resolvePlaybackDurationMs } from './playback/PlaybackProgress'
import { shouldShowPlaybackControls } from './playback/PlaybackUiState'
import { PlaybackDiagnostics, type PlaybackDiagnosticReport } from './diagnostics/PlaybackDiagnostics'
import { flushPendingPlaybackFeedback, saveAndUploadPlaybackFeedback } from './diagnostics/PlaybackFeedback'

type DebugChunk = Omit<ChunkRecord, 'blob'>
interface DebugSegment { segment: SegmentRecord; chunks: DebugChunk[] }
interface DebugSession { session: SessionRecord; segments: DebugSegment[]; markers: MarkerRecord[]; serverOnly?: boolean; serverSegmentCount?: number; serverChunkCount?: number; liveProcessing?: LiveProcessingStatus | null }
interface SessionGroup { key: string; label: string; items: DebugSession[] }
interface ServerProcessingState { status: 'idle' | 'queued' | 'reconstructing' | 'asr' | 'reporting' | 'completed' | 'error'; message: string; jobId?: string; report?: ReportResponse; startedAt?: number }
interface PendingPlaybackSeek { sessionId: string; elapsedMs: number }
type AppTab = 'recording' | 'sessions' | 'settings'
type DiagnosticFeedbackStatus = 'idle' | 'uploading' | 'success' | 'error'
type SummaryThemeId = 'paper' | 'ocean' | 'sunset' | 'forest' | 'night'
type KnowledgeFontFamilyId = 'serif' | 'sans' | 'yahei' | 'kai'
interface KnowledgeEditStructure { title: string; points: string }
interface KnowledgeEditQuestion { question: string; answer: string }
interface KnowledgeEditForm {
  title: string
  overview: string
  keyPoints: string
  actionItems: string
  knowledgeStructure: KnowledgeEditStructure[]
  questions: KnowledgeEditQuestion[]
}
type KnowledgeEditSection = 'overview' | 'structure' | 'keyPoints' | 'questions' | 'actionItems'

interface KnowledgeFullscreenTarget {
  label: string
  value: string
  update: (value: string) => void
}

const SUMMARY_THEMES: Array<{ id: SummaryThemeId; name: string }> = [
  { id: 'paper', name: '纸张' },
  { id: 'ocean', name: '海蓝' },
  { id: 'sunset', name: '暮光' },
  { id: 'forest', name: '森林' },
  { id: 'night', name: '夜色' },
]

const KNOWLEDGE_FONT_FAMILIES: Array<{ id: KnowledgeFontFamilyId; name: string; stack: string }> = [
  { id: 'serif', name: '宋体', stack: '"Noto Serif SC", "Songti SC", SimSun, serif' },
  { id: 'sans', name: '黑体', stack: '"Noto Sans SC", SimHei, sans-serif' },
  { id: 'yahei', name: '微软雅黑', stack: '"Microsoft YaHei", "Noto Sans SC", sans-serif' },
  { id: 'kai', name: '楷体', stack: 'KaiTi, STKaiti, serif' },
]

const COMPARISON_ORDER: AudioProfileId[] = ['browser-default', 'speech', 'raw-ish']
const AUTO_UPLOAD_STORAGE_KEY = 'livenote-auto-upload-enabled'
// Thirty-second fragments reduce request overhead while keeping the amount of
// audio that can remain only inside MediaRecorder bounded during page failure.
// MediaRecorder itself still runs continuously; this does not stop/start it.
const CHUNK_TIMESLICE_MS = 30_000

const capabilities = ref<AudioCapabilities>(detectAudioCapabilities())
const selectedProfile = ref<AudioProfileId>('speech')
const recorderState = ref<RecorderState>('idle')
const currentMimeType = ref('—')
const level = ref(0)
const elapsedMs = ref(0)
const isBusy = ref(false)
const errorMessage = ref('')
const storageReady = ref(false)
const storageError = ref('')
const wakeLockState = ref<WakeLockState>('RELEASED')
const wakeLockMessage = ref('')
const currentSession = ref<SessionRecord | null>(null)
const currentSegment = ref<SegmentRecord | null>(null)
const savedChunkCount = ref(0)
const currentSegmentChunkCount = ref(0)
const currentChunkIndex = ref(-1)
const savedBytes = ref(0)
const lastSavedChunk = ref<ChunkMetadata | null>(null)
const recoverySession = ref<SessionRecord | null>(null)
const recoverySessions = ref<SessionRecord[]>([])
const debugSessions = ref<DebugSession[]>([])
const restoredUrls = ref<Record<string, string>>({})
const restoredMeta = ref<Record<string, SegmentPlayback>>({})
const restoringSegmentId = ref<string | null>(null)
const restoredSessionUrls = ref<Record<string, string>>({})
const restoredSessionMeta = ref<Record<string, SessionPlayback>>({})
const restoredSessionAudio = ref<HTMLAudioElement | null>(null)
const restoringSessionId = ref<string | null>(null)
const playbackAttemptSessionId = ref<string | null>(null)
const playbackCurrentTimeMs = ref(0)
const playbackIsPlaying = ref(false)
const playbackPreparationMessage = ref('')
const comparisonMode = ref(false)
const comparisonIndex = ref(0)
const lifecycleEvents = ref<LifecycleEventRecord[]>([])
const pageVisibility = ref<DocumentVisibilityState>(document.visibilityState)
const uploadSnapshot = ref<UploadQueueSnapshot>({ serverOnline: null, serverCompatible: null, serverCapabilities: null, isUploading: false, total: 0, uploaded: 0, pending: 0, failed: 0, lastError: '' })
const sessionUpload = ref({ total: 0, uploaded: 0, pending: 0, failed: 0 })
const selectedSessionUpload = ref({ total: 0, uploaded: 0, pending: 0, failed: 0 })
const sessionMarkers = ref<MarkerRecord[]>([])
const lastMarkerMessage = ref('')
const markerNoteDraft = ref('')
const serverProcessing = ref<Record<string, ServerProcessingState>>({})
const summaryTheme = ref<SummaryThemeId>('paper')
const knowledgeFontScale = ref(1)
const knowledgeFontFamily = ref<KnowledgeFontFamilyId>('serif')
const knowledgeFontControlExpanded = ref(false)
const knowledgeCaptureMode = ref(false)
const activeTab = ref<AppTab>('recording')
const knowledgeViewSessionId = ref<string | null>(null)
const knowledgeEditing = ref(false)
const knowledgeOverviewExpanded = ref(false)
const knowledgeExpandedSections = ref<Record<KnowledgeEditSection, boolean>>({ overview: false, structure: false, keyPoints: false, questions: false, actionItems: false })
const knowledgeStructureExpanded = ref<Record<number, boolean>>({})
const knowledgeOverviewFullscreen = ref(false)
const knowledgeFullscreenTarget = ref<KnowledgeFullscreenTarget | null>(null)
const knowledgeFullscreenDraft = ref('')
const knowledgeDraft = ref<ContentSummary | null>(null)
const knowledgeEditForm = ref<KnowledgeEditForm>({ title: '', overview: '', keyPoints: '', actionItems: '', knowledgeStructure: [], questions: [] })
const knowledgeDraftMessage = ref('')
const knowledgeResetPending = ref(false)
const sessionQuery = ref('')
const selectedSessionId = ref<string | null>(null)
const editingSessionId = ref<string | null>(null)
const editingSessionTitle = ref('')
const deletingSessionId = ref<string | null>(null)
const liveRetryingSessionId = ref<string | null>(null)
const pendingSessionDeleteId = ref<string | null>(null)
const pendingClearLocalData = ref(false)
const networkOnline = ref(typeof navigator === 'undefined' ? true : navigator.onLine)
const storageEstimate = ref<{ usage: number | null; quota: number | null }>({ usage: null, quota: null })
const persistentStorage = ref<boolean | null>(null)
const pwaSnapshot = ref<PwaInstallSnapshot>({ status: 'unavailable', isStandalone: false, isSecureContext: false, isProductionBuild: import.meta.env.PROD })
const diagnosticFeedback = ref<{ status: DiagnosticFeedbackStatus; message: string; id?: string }>({ status: 'idle', message: '' })
const diagnosticImage = ref<File | null>(null)
const autoUploadEnabled = ref(readAutoUploadSetting())
const playbackFeedback = ref<{ status: 'idle' | 'collecting' | 'uploading' | 'success' | 'pending' | 'error'; message: string; id?: string }>({ status: 'idle', message: '' })
const playbackFeedbackDescription = ref('')
const playbackFeedbackExpanded = ref(false)
const pendingPlaybackSeek = ref<PendingPlaybackSeek | null>(null)
const pairingCode = ref('')
const pairingLabel = ref('Android Chrome')
const deviceIdentity = ref<{ deviceId: string; userId: string; displayName: string } | null>(null)
const deviceIdentityResolved = ref(false)
const pairingMessage = ref('')
const pairingBusy = ref(false)
const deviceLogoutBusy = ref(false)
let sessionUploadRefreshToken = 0

function readAutoUploadSetting(): boolean {
  try {
    return localStorage.getItem(AUTO_UPLOAD_STORAGE_KEY) !== 'false'
  } catch {
    return true
  }
}

function setAutoUploadEnabled(enabled: boolean): void {
  autoUploadEnabled.value = enabled
  try {
    localStorage.setItem(AUTO_UPLOAD_STORAGE_KEY, String(enabled))
  } catch {
    // Keep the in-memory preference for restricted browser storage modes.
  }
  if (enabled) {
    startUploadIfEnabled()
    uploadQueue.kick()
  } else {
    uploadQueue.stop()
  }
}

function startUploadIfEnabled(): void {
  if (autoUploadEnabled.value) uploadQueue.start()
}

const engine = new MediaRecorderEngine()
const levelMonitor = new AudioLevelMonitor((nextLevel) => { level.value = nextLevel })
const wakeLockManager = new WakeLockManager((state, message) => {
  wakeLockState.value = state
  wakeLockMessage.value = message ?? ''
})
const pageLifecycleManager = new PageLifecycleManager((event) => { pageVisibility.value = event.visibilityState; void persistLifecycleEvent(event); if (event.visibilityState === 'visible') void resultSyncManager.request(); if (['pagehide', 'freeze'].includes(event.eventType)) void persistSessionCheckpoint(true) })
const unsubscribeUploadQueue = uploadQueue.subscribe((snapshot) => {
  uploadSnapshot.value = snapshot
  const sessionIds = new Set([currentSession.value?.id, selectedSessionId.value].filter((sessionId): sessionId is string => Boolean(sessionId)))
  for (const sessionId of sessionIds) void refreshSessionUpload(sessionId)
})
const pwaInstallManager = new PwaInstallManager()
const unsubscribePwaInstall = pwaInstallManager.subscribe((snapshot) => { pwaSnapshot.value = snapshot })
let durationTimer: number | null = null
let activeMonotonicStartedAt = 0
let activeElapsedBaseMs = 0
let activeSegmentBaseMs = 0
let sessionCheckpointTimer: number | null = null
let storageEstimateTimer: number | null = null
let liveProcessingTimer: number | null = null
let sessionCheckpointChain: Promise<void> = Promise.resolve()
let recorderFailurePromise: Promise<void> | null = null
let activePlaybackDiagnostics: PlaybackDiagnostics | null = null
let playbackFeedbackTimer: number | null = null
let backgroundPlaybackPreparationSessionId: string | null = null
const recoveryInProgress = ref(false)
const resultSyncManager = new ResultSyncManager({
  intervalMs: 30_000,
  canRun: () => storageReady.value && !isRecording.value && networkOnline.value && pageVisibility.value === 'visible',
  poll: refreshDebugData,
  onError: (error) => { console.warn('Result sync failed', error) },
})

const selectedProfileDetails = computed(() => AUDIO_PROFILES.find((profile) => profile.id === selectedProfile.value) ?? AUDIO_PROFILES[0])
const isRecording = computed(() => recorderState.value === 'recording')
const comparisonComplete = computed(() => comparisonMode.value && comparisonIndex.value >= COMPARISON_ORDER.length)
const canStart = computed(() => storageReady.value && Boolean(deviceIdentity.value) && !isBusy.value && !isRecording.value && !comparisonComplete.value && !recoverySession.value && !recoveryInProgress.value)
const durationLabel = computed(() => formatDuration(elapsedMs.value))
const recordUploadProgress = computed(() => {
  const total = sessionUpload.value.total
  if (!total) return 0
  return Math.min(100, Math.round((sessionUpload.value.uploaded / total) * 100))
})
const recordUploadLabel = computed(() => `上传 ${sessionUpload.value.uploaded} / ${sessionUpload.value.total}`)
const segmentIdLabel = computed(() => currentSegment.value?.id ?? '—')
const comparisonStepLabel = computed(() => comparisonComplete.value ? '三组对比已完成' : `第 ${comparisonIndex.value + 1} / ${COMPARISON_ORDER.length} 组`)
const lastHiddenEvent = computed(() => [...lifecycleEvents.value].reverse().find((event) => ['pagehide', 'freeze'].includes(event.eventType) || (event.eventType === 'visibilitychange' && event.visibilityState !== 'visible')) ?? null)
const lastReturnEvent = computed(() => {
  const hidden = lastHiddenEvent.value
  if (!hidden) return null
  return [...lifecycleEvents.value].reverse().find((event) => event.wallClockMs > hidden.wallClockMs && ['pageshow', 'resume'].includes(event.eventType) || (event.wallClockMs > hidden.wallClockMs && event.eventType === 'visibilitychange' && event.visibilityState === 'visible')) ?? null
})
const hasLifecycleRisk = computed(() => Boolean(lastHiddenEvent.value))
const lifecycleGapMs = computed(() => {
  const hidden = lastHiddenEvent.value
  if (!hidden) return 0
  return Math.max(0, (lastReturnEvent.value?.wallClockMs ?? Date.now()) - hidden.wallClockMs)
})

const capabilityRows = computed(() => [
  ['getUserMedia', capabilities.value.getUserMedia],
  ['MediaRecorder', capabilities.value.mediaRecorder],
  ['IndexedDB', capabilities.value.indexedDB],
  ['navigator.wakeLock', capabilities.value.wakeLock],
  ['audio/webm;codecs=opus', capabilities.value.webmOpus],
  ['audio/webm', capabilities.value.webm],
  ['audio/mp4', capabilities.value.mp4],
] as const)

const selectedDebugSession = computed(() => debugSessions.value.find((item) => item.session.id === selectedSessionId.value) ?? null)
const playbackDurationMs = computed(() => {
  const sessionId = selectedSessionId.value
  return sessionId ? resolvePlaybackDurationMs(restoredSessionAudio.value?.duration ?? Number.NaN, restoredSessionMeta.value[sessionId]?.durationMs ?? 0) : 0
})
const playbackProgress = computed(() => playbackProgressPercent(playbackCurrentTimeMs.value / 1000, playbackDurationMs.value))
const selectedProcessing = computed(() => selectedSessionId.value ? serverProcessing.value[selectedSessionId.value] : undefined)
const knowledgeBaseReport = computed(() => knowledgeViewSessionId.value ? serverProcessing.value[knowledgeViewSessionId.value]?.report : undefined)
const knowledgeViewReport = computed<ReportResponse | undefined>(() => {
  const report = knowledgeBaseReport.value
  const draft = knowledgeDraft.value
  if (!report || !draft || !report.summary) return report
  return { ...report, summary: { ...report.summary, ...draft } }
})
const knowledgePageStyle = computed<Record<string, string>>(() => {
  const font = KNOWLEDGE_FONT_FAMILIES.find((item) => item.id === knowledgeFontFamily.value) ?? KNOWLEDGE_FONT_FAMILIES[0]
  return { '--knowledge-font-scale': String(knowledgeFontScale.value), '--knowledge-font-family': font.stack }
})
const filteredDebugSessions = computed(() => {
  const query = sessionQuery.value.trim().toLowerCase()
  return [...debugSessions.value]
    .filter((item) => {
      if (item.session.endedAt !== null && debugSessionChunkCount(item) === 0) return false
      if (!query) return true
      const session = item.session
      return session.title.toLowerCase().includes(query) || session.id.toLowerCase().includes(query) || formatDate(session.startedAt).toLowerCase().includes(query)
    })
    .sort((a, b) => b.session.startedAt - a.session.startedAt)
})
const sessionGroups = computed<SessionGroup[]>(() => {
  const groups = new Map<string, SessionGroup>()
  const now = new Date()
  for (const item of filteredDebugSessions.value) {
    const group = sessionGroupFor(item.session.startedAt, now)
    const existing = groups.get(group.key)
    if (existing) {
      existing.items.push(item)
    } else {
      groups.set(group.key, { ...group, items: [item] })
    }
  }
  return [...groups.values()]
})
const editingSession = computed(() => editingSessionId.value ? debugSessions.value.find((item) => item.session.id === editingSessionId.value) ?? null : null)
const shouldShowPwaGuide = computed(() => !isRecording.value && pwaSnapshot.value.status !== 'installed')
const pwaStatusLabel = computed(() => {
  if (pwaSnapshot.value.status === 'installed') return pwaSnapshot.value.isStandalone ? '已安装并正在使用' : '已安装，可从桌面打开'
  if (pwaSnapshot.value.status === 'prompt') return '可以安装'
  if (pwaSnapshot.value.status === 'manual') return '可手动安装'
  return '当前环境不可安装'
})
const storageUsageRatio = computed(() => {
  const { usage, quota } = storageEstimate.value
  return usage !== null && quota && quota > 0 ? usage / quota : null
})
const storageEstimateLabel = computed(() => {
  const { usage, quota } = storageEstimate.value
  if (usage === null || quota === null) return '浏览器未提供空间信息'
  return `${formatBytes(usage)} / ${formatBytes(quota)}`
})
const storageEstimateClass = computed(() => {
  if (storageUsageRatio.value !== null && storageUsageRatio.value >= 0.9) return 'status-warn'
  return storageUsageRatio.value !== null ? 'status-good' : ''
})

function createId(prefix: string): string {
  const uuid = globalThis.crypto?.randomUUID?.()
  return `${prefix}-${uuid ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`
}

function formatDuration(durationMs: number): string {
  const totalSeconds = Math.floor(Math.max(0, durationMs) / 1000)
  return `${Math.floor(totalSeconds / 60).toString().padStart(2, '0')}:${(totalSeconds % 60).toString().padStart(2, '0')}`
}

function formatSessionDuration(durationMs: number): string {
  const totalSeconds = Math.floor(Math.max(0, durationMs) / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (!minutes) return `${seconds}秒`
  if (!seconds) return `${minutes}分`
  return `${minutes}分${seconds}秒`
}

function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

async function loadDeviceIdentity(): Promise<void> {
  try {
    if (!localStorage.getItem('livenote-device-token')) return
    const response = await ApiClient.getCurrentDevice()
    const deviceId = response.device.id?.trim()
    const userId = response.user.id?.trim()
    const displayName = response.user.displayName?.trim()
    if (!deviceId || !userId || !displayName) {
      ApiClient.clearDeviceToken()
      deviceIdentity.value = null
      return
    }
    deviceIdentity.value = { deviceId, userId, displayName }
    void flushPendingPlaybackFeedback(userId)
  } catch {
    ApiClient.clearDeviceToken()
    deviceIdentity.value = null
  } finally {
    deviceIdentityResolved.value = true
  }
}

async function pairCurrentDevice(): Promise<void> {
  if (pairingBusy.value || pairingCode.value.trim().length < 6) return
  pairingBusy.value = true
  pairingMessage.value = ''
  try {
    const response = await ApiClient.pairDevice(pairingCode.value.trim(), pairingLabel.value.trim() || 'Android Chrome')
    ApiClient.setDeviceToken(response.token)
    pairingCode.value = ''
    await loadDeviceIdentity()
    pairingMessage.value = '设备已绑定；之后只会看到这个用户的已发布内容。'
    await refreshDebugData()
  } catch (error) {
    pairingMessage.value = error instanceof Error ? error.message : '设备绑定失败。'
  } finally {
    pairingBusy.value = false
  }
}

async function logoutCurrentDevice(): Promise<void> {
  if (!deviceIdentity.value || deviceLogoutBusy.value || isRecording.value || isBusy.value) return
  deviceLogoutBusy.value = true
  pairingMessage.value = ''
  try {
    await ApiClient.logoutDevice()
    ApiClient.clearDeviceToken()
    deviceIdentity.value = null
    pairingMessage.value = '已退出此设备；本机录音仍保留。再次使用需要重新配对。'
  } catch (error) {
    pairingMessage.value = error instanceof Error
      ? `退出失败，服务器未确认；请检查网络后重试。本机录音未受影响。${error.message}`
      : '退出失败，服务器未确认；请检查网络后重试。本机录音未受影响。'
    errorMessage.value = pairingMessage.value
  } finally {
    deviceLogoutBusy.value = false
  }
}

function formatDate(timestamp: number | null): string { return timestamp ? new Date(timestamp).toLocaleString() : '—' }

function formatSessionDate(timestamp: number | null): string {
  if (!timestamp) return '—'
  const date = new Date(timestamp)
  const weekday = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][date.getDay()]
  const monthDay = date.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
  const time = date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  return `${weekday} · ${monthDay} ${time}`
}

const WEEKDAY_LABELS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
const DAY_MS = 24 * 60 * 60 * 1000

function localDateOrdinal(date: Date): number {
  return Date.UTC(date.getFullYear(), date.getMonth(), date.getDate())
}

function localWeekStartOrdinal(date: Date): number {
  const weekdayOffset = date.getDay() === 0 ? 6 : date.getDay() - 1
  return localDateOrdinal(new Date(date.getFullYear(), date.getMonth(), date.getDate() - weekdayOffset))
}

function sessionGroupFor(timestamp: number, now: Date): { key: string; label: string } {
  const date = new Date(timestamp)
  const dayOrdinal = localDateOrdinal(date)
  const todayOrdinal = localDateOrdinal(now)
  const dayDiff = Math.round((todayOrdinal - dayOrdinal) / DAY_MS)
  const currentWeekStart = localWeekStartOrdinal(now)

  if (dayDiff === 0) return { key: `day-${dayOrdinal}`, label: '今天' }
  if (dayDiff === 1) return { key: `day-${dayOrdinal}`, label: '昨天' }
  if (dayOrdinal >= currentWeekStart && dayOrdinal < currentWeekStart + 7 * DAY_MS) {
    return { key: `day-${dayOrdinal}`, label: WEEKDAY_LABELS[date.getDay()] }
  }
  if (date.getFullYear() === now.getFullYear()) {
    return { key: `month-${date.getFullYear()}-${date.getMonth() + 1}`, label: `${date.getMonth() + 1}月` }
  }
  return { key: `year-${date.getFullYear()}`, label: `${date.getFullYear()}年` }
}

async function refreshStorageEstimate(): Promise<void> {
  if (typeof navigator === 'undefined' || !navigator.storage?.estimate) return
  try {
    const estimate = await navigator.storage.estimate()
    storageEstimate.value = { usage: estimate.usage ?? null, quota: estimate.quota ?? null }
    persistentStorage.value = navigator.storage.persisted ? await navigator.storage.persisted() : null
  } catch {
    // Storage estimation is optional and must never affect recording.
  }
}

async function requestPersistentStorage(): Promise<void> {
  if (typeof navigator === 'undefined' || !navigator.storage?.persist) return
  try {
    persistentStorage.value = await navigator.storage.persist()
  } catch {
    // Persistence is a browser policy decision and must never block recording.
  }
}

function recordingErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof DOMException && error.name === 'QuotaExceededError') return '手机本地存储空间不足，已停止继续保存；请先导出或删除旧 Session 后再录音。'
  return error instanceof Error ? error.message : fallback
}

function displaySessionTitle(title: string): string {
  const normalized = title.replace(/^(?:LiveNote|LN)\s*/i, '').trim()
  const timestamp = normalized.match(/^(\d{2,4})[/.\-](\d{1,2})[/.\-](\d{1,2})(?:\s+|T)(\d{1,2}):(\d{2})(?::\d{2})?/)
  if (timestamp) {
    const year = timestamp[1].length === 2 ? 2000 + Number(timestamp[1]) : Number(timestamp[1])
    const parsed = new Date(year, Number(timestamp[2]) - 1, Number(timestamp[3]), Number(timestamp[4]), Number(timestamp[5])).getTime()
    if (Number.isFinite(parsed)) return formatSessionDate(parsed)
  }
  return normalized.replace(/^\d{4}[/.\-]/, '').replace(/(\d{1,2}:\d{2}):\d{2}(?=$|\s)/, '$1')
}

function sessionStatusLabel(status: SessionRecord['status']): string {
  return { RECORDING: '录音中', PAUSED: '已暂停', FINALIZING: '收尾中', COMPLETED: '', INTERRUPTED: '需恢复' }[status]
}

function sessionUploadSummary(item: DebugSession): { total: number; uploaded: number; pending: number; failed: number } {
  const chunks = item.segments.flatMap((segment) => segment.chunks)
  return {
    total: chunks.length,
    uploaded: chunks.filter((chunk) => chunk.uploadStatus === 'UPLOADED').length,
    pending: chunks.filter((chunk) => ['PENDING', 'UPLOADING'].includes(chunk.uploadStatus)).length,
    failed: chunks.filter((chunk) => chunk.uploadStatus === 'FAILED').length,
  }
}

function sameUploadSummary(left: { total: number; uploaded: number; pending: number; failed: number }, right: { total: number; uploaded: number; pending: number; failed: number }): boolean {
  return left.total === right.total && left.uploaded === right.uploaded && left.pending === right.pending && left.failed === right.failed
}

function sessionUploadLabel(item: DebugSession): string {
  const summary = sessionUploadSummary(item)
  return summary.total ? `上传 ${summary.uploaded}/${summary.total}` : ''
}

function liveProcessingLabel(item: DebugSession): string {
  const live = item.liveProcessing
  if (!live) return ''
  if (live.status === 'FAILED') return '增量识别失败，可重试'
  if (live.status === 'WAITING_FOR_DECODABLE_PREFIX') return '等待音频可处理'
  if (live.processedDurationMs > 0) return `已识别至 ${formatDuration(live.processedDurationMs)}`
  return live.status === 'WAITING_FOR_CHUNKS' ? '等待更多音频' : '增量识别中'
}

async function retryLiveProcessing(sessionId: string): Promise<void> {
  if (liveRetryingSessionId.value) return
  liveRetryingSessionId.value = sessionId
  try {
    const response = await ApiClient.retryLiveProcessing(sessionId)
    debugSessions.value = debugSessions.value.map((item) => item.session.id === sessionId ? { ...item, liveProcessing: response.liveProcessing } : item)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '增量识别重试失败。'
  } finally {
    liveRetryingSessionId.value = null
  }
}

function sessionUploadComplete(item: DebugSession): boolean {
  const summary = sessionUploadSummary(item)
  return summary.total > 0 && summary.uploaded === summary.total && summary.pending === 0 && summary.failed === 0
}

function processingStatusLabel(sessionId: string): string {
  const state = serverProcessing.value[sessionId]
  if (!state) return '等待电脑端处理'
  if (state.report) return ''
  if (state.status === 'error') return '处理失败，等待重试'
  if (['queued', 'reconstructing', 'asr', 'reporting'].includes(state.status)) return '电脑端处理中'
  return '等待电脑端处理'
}

function processingStatusClass(sessionId: string): string {
  const state = serverProcessing.value[sessionId]
  if (!state) return 'session-processing-status-waiting'
  if (state.report) return 'session-processing-status-complete'
  if (state.status === 'error') return 'session-processing-status-error'
  if (['queued', 'reconstructing', 'asr', 'reporting'].includes(state.status)) return 'session-processing-status-active'
  return 'session-processing-status-waiting'
}

function sessionDetailStatusLabel(item: DebugSession): string {
  const processing = serverProcessing.value[item.session.id]
  if (processing?.status === 'error') return '处理失败，等待重试'
  if (processing && ['queued', 'reconstructing', 'asr', 'reporting'].includes(processing.status)) return '电脑端处理中'
  if (processing?.report) return '已完成'
  return { RECORDING: '录音中', PAUSED: '已暂停', FINALIZING: '收尾中', COMPLETED: '已完成', INTERRUPTED: '需恢复' }[item.session.status]
}

function openSessions(sessionId?: string): void {
  activeTab.value = 'sessions'
  pendingSessionDeleteId.value = null
  if (!sessionId) {
    selectedSessionId.value = null
    selectedSessionUpload.value = { total: 0, uploaded: 0, pending: 0, failed: 0 }
    void refreshDebugData()
    return
  }
  if (selectedSessionId.value === sessionId) {
    selectedSessionId.value = null
    selectedSessionUpload.value = { total: 0, uploaded: 0, pending: 0, failed: 0 }
    return
  }
  selectedSessionId.value = sessionId
  const selectedItem = debugSessions.value.find((item) => item.session.id === sessionId)
  selectedSessionUpload.value = selectedItem ? sessionUploadSummary(selectedItem) : { total: 0, uploaded: 0, pending: 0, failed: 0 }
  void refreshSessionUpload(sessionId)
  if (!debugSessions.value.some((item) => item.session.id === sessionId)) void refreshDebugData()
}

function openSettings(): void {
  activeTab.value = 'settings'
  selectedSessionId.value = null
  selectedSessionUpload.value = { total: 0, uploaded: 0, pending: 0, failed: 0 }
}

function resetKnowledgeExpandedSections(): void {
  knowledgeExpandedSections.value = { overview: false, structure: false, keyPoints: false, questions: false, actionItems: false }
  knowledgeStructureExpanded.value = {}
  knowledgeOverviewExpanded.value = false
}

function toggleKnowledgeSection(section: KnowledgeEditSection): void {
  knowledgeExpandedSections.value[section] = !knowledgeExpandedSections.value[section]
  if (section === 'overview') knowledgeOverviewExpanded.value = knowledgeExpandedSections.value.overview
}

function isKnowledgeSectionExpanded(section: KnowledgeEditSection): boolean {
  return knowledgeExpandedSections.value[section]
}

function toggleKnowledgeStructure(index: number): void {
  knowledgeStructureExpanded.value[index] = !Boolean(knowledgeStructureExpanded.value[index])
}

function isKnowledgeStructureExpanded(index: number): boolean {
  return Boolean(knowledgeStructureExpanded.value[index])
}

async function openKnowledgeView(): Promise<void> {
  if (!selectedSessionId.value || !selectedProcessing.value?.report) return
  knowledgeViewSessionId.value = selectedSessionId.value
  knowledgeEditing.value = false
  knowledgeFontScale.value = 1
  knowledgeFontFamily.value = 'serif'
  knowledgeFontControlExpanded.value = false
  knowledgeCaptureMode.value = false
  resetKnowledgeExpandedSections()
  knowledgeOverviewFullscreen.value = false
  closeKnowledgeFullscreenField()
  knowledgeDraftMessage.value = ''
  knowledgeResetPending.value = false
  const draft = await ResultDraftStore.get(selectedSessionId.value).catch(() => undefined)
  const ownerId = deviceIdentity.value?.userId ?? null
  knowledgeDraft.value = draft && (draft.ownerId === null || draft.ownerId === ownerId) ? draft.summary : null
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

function closeKnowledgeView(): void {
  knowledgeViewSessionId.value = null
  knowledgeEditing.value = false
  knowledgeFontScale.value = 1
  knowledgeFontFamily.value = 'serif'
  knowledgeFontControlExpanded.value = false
  knowledgeCaptureMode.value = false
  resetKnowledgeExpandedSections()
  knowledgeOverviewFullscreen.value = false
  closeKnowledgeFullscreenField()
  knowledgeDraft.value = null
  knowledgeDraftMessage.value = ''
  knowledgeResetPending.value = false
}

function beginKnowledgeEdit(): void {
  const summary = knowledgeViewReport.value?.summary
  if (!summary) return
  knowledgeEditForm.value = {
    title: summary.title,
    overview: summary.overview,
    keyPoints: summary.keyPoints.join('\n'),
    actionItems: summary.actionItems.join('\n'),
    knowledgeStructure: summary.knowledgeStructure.map((section) => ({ title: section.title, points: section.points.join('\n') })),
    questions: summary.questions.map((item) => ({ question: item.question, answer: item.answer })),
  }
  knowledgeDraftMessage.value = ''
  knowledgeResetPending.value = false
  resetKnowledgeExpandedSections()
  knowledgeOverviewFullscreen.value = false
  closeKnowledgeFullscreenField()
  knowledgeCaptureMode.value = false
  knowledgeEditing.value = true
  void nextTick(() => resizeKnowledgeTitle())
}

function resizeKnowledgeTitle(event?: Event): void {
  const textarea = event?.target instanceof HTMLTextAreaElement
    ? event.target
    : document.getElementById('knowledge-title-editor') as HTMLTextAreaElement | null
  if (!textarea) return
  textarea.style.height = 'auto'
  textarea.style.height = `${textarea.scrollHeight}px`
}

function cancelKnowledgeEdit(): void {
  knowledgeEditing.value = false
  resetKnowledgeExpandedSections()
  knowledgeOverviewFullscreen.value = false
  closeKnowledgeFullscreenField()
  knowledgeDraftMessage.value = ''
  knowledgeResetPending.value = false
}

function openKnowledgeOverviewFullscreen(): void {
  knowledgeExpandedSections.value.overview = true
  knowledgeOverviewExpanded.value = true
  knowledgeOverviewFullscreen.value = true
}

function closeKnowledgeOverviewFullscreen(): void {
  knowledgeOverviewFullscreen.value = false
}

function openKnowledgeFullscreenField(label: string, value: string, update: (nextValue: string) => void): void {
  knowledgeFullscreenTarget.value = { label, value, update }
  knowledgeFullscreenDraft.value = value
}

function closeKnowledgeFullscreenField(): void {
  knowledgeFullscreenTarget.value = null
  knowledgeFullscreenDraft.value = ''
}

function setKnowledgeFontScale(event: Event): void {
  const value = Number((event.target as HTMLInputElement).value)
  if (!Number.isFinite(value)) return
  knowledgeFontScale.value = Math.min(1.6, Math.max(0.85, value / 100))
}

function saveKnowledgeFullscreenField(): void {
  const target = knowledgeFullscreenTarget.value
  if (!target) return
  target.update(knowledgeFullscreenDraft.value)
  closeKnowledgeFullscreenField()
}

function openKnowledgeKeyPointsFullscreen(): void {
  openKnowledgeFullscreenField('关键知识点', knowledgeEditForm.value.keyPoints, (value) => { knowledgeEditForm.value.keyPoints = value })
}

function openKnowledgeActionItemsFullscreen(): void {
  openKnowledgeFullscreenField('行动建议', knowledgeEditForm.value.actionItems, (value) => { knowledgeEditForm.value.actionItems = value })
}

function openKnowledgeStructurePointsFullscreen(index: number): void {
  const item = knowledgeEditForm.value.knowledgeStructure[index]
  if (!item) return
  openKnowledgeFullscreenField('知识点', item.points, (value) => { item.points = value })
}

function openKnowledgeQuestionFullscreen(index: number): void {
  const item = knowledgeEditForm.value.questions[index]
  if (!item) return
  openKnowledgeFullscreenField('回答', item.answer, (value) => { item.answer = value })
}

function requestRestoreKnowledgeOriginal(): void {
  if (!knowledgeDraft.value) return
  knowledgeResetPending.value = true
}

function cancelRestoreKnowledgeOriginal(): void {
  knowledgeResetPending.value = false
}

async function restoreKnowledgeOriginal(): Promise<void> {
  const sessionId = knowledgeViewSessionId.value
  if (!sessionId || !knowledgeDraft.value) return
  await ResultDraftStore.delete(sessionId)
  knowledgeDraft.value = null
  knowledgeEditing.value = false
  resetKnowledgeExpandedSections()
  knowledgeOverviewFullscreen.value = false
  closeKnowledgeFullscreenField()
  knowledgeResetPending.value = false
  knowledgeDraftMessage.value = '已恢复服务器原稿。'
}

function editorLines(value: string): string[] {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
}

function addKnowledgeStructure(): void {
  const index = knowledgeEditForm.value.knowledgeStructure.length
  knowledgeEditForm.value.knowledgeStructure.push({ title: '', points: '' })
  knowledgeExpandedSections.value.structure = true
  knowledgeStructureExpanded.value[index] = true
}

function removeKnowledgeStructure(index: number): void {
  knowledgeEditForm.value.knowledgeStructure.splice(index, 1)
  knowledgeStructureExpanded.value = {}
}

function addKnowledgeQuestion(): void {
  knowledgeEditForm.value.questions.push({ question: '', answer: '' })
  knowledgeExpandedSections.value.questions = true
}

function removeKnowledgeQuestion(index: number): void {
  knowledgeEditForm.value.questions.splice(index, 1)
}

async function saveKnowledgeDraft(): Promise<void> {
  const sessionId = knowledgeViewSessionId.value
  const report = knowledgeBaseReport.value
  if (!sessionId || !report?.summary) return
  knowledgeDraftMessage.value = ''
  try {
    const summary: ContentSummary = {
      ...report.summary,
      title: knowledgeEditForm.value.title.trim() || report.summary.title || '直播总结',
      overview: knowledgeEditForm.value.overview.trim(),
      keyPoints: editorLines(knowledgeEditForm.value.keyPoints),
      actionItems: editorLines(knowledgeEditForm.value.actionItems),
      knowledgeStructure: knowledgeEditForm.value.knowledgeStructure
        .map((section) => ({ title: section.title.trim() || '知识要点', points: editorLines(section.points) }))
        .filter((section) => section.points.length),
      questions: knowledgeEditForm.value.questions
        .map((item) => ({ question: item.question.trim(), answer: item.answer.trim(), startMs: null }))
        .filter((item) => item.question || item.answer),
    }
    const published = await ResultStore.get(sessionId).catch(() => undefined)
    await ResultDraftStore.put({
      sessionId,
      ownerId: deviceIdentity.value?.userId ?? published?.ownerId ?? null,
      baseRevisionId: published?.revisionId ?? null,
      updatedAt: Date.now(),
      summary,
    })
    knowledgeDraft.value = summary
    knowledgeEditing.value = false
    resetKnowledgeExpandedSections()
    knowledgeOverviewFullscreen.value = false
    closeKnowledgeFullscreenField()
    knowledgeDraftMessage.value = '本机草稿已保存，不会修改服务器原稿。'
  } catch (error) {
    knowledgeDraftMessage.value = error instanceof Error ? `保存失败：${error.message}` : '保存失败，请稍后再试。'
  }
}

async function installPwa(): Promise<void> {
  const result = await pwaInstallManager.promptInstall()
  if (result === 'accepted') activeTab.value = 'recording'
}

function beginSessionTitleEdit(item: DebugSession): void {
  editingSessionId.value = item.session.id
  editingSessionTitle.value = displaySessionTitle(item.session.title)
}

function cancelSessionTitleEdit(): void {
  editingSessionId.value = null
  editingSessionTitle.value = ''
}

async function saveSessionTitle(item: DebugSession): Promise<void> {
  const title = editingSessionTitle.value.trim()
  if (!title) return
  const updated = { ...item.session, title, updatedAt: Date.now() }
  await SessionStore.put(updated)
  debugSessions.value = debugSessions.value.map((entry) => entry.session.id === updated.id ? { ...entry, session: updated } : entry)
  if (currentSession.value?.id === updated.id) currentSession.value = updated
  // The upload queue will upsert the changed metadata when the server is
  // reachable; offline title edits therefore remain local and are retried.
  uploadQueue.kick()
  cancelSessionTitleEdit()
}

function isSessionDeletionBlocked(item: DebugSession): boolean {
  const hasInflightUpload = item.segments.some((segment) => segment.chunks.some((chunk) => chunk.uploadStatus === 'UPLOADING'))
  return isRecording.value || isBusy.value || hasInflightUpload || deletingSessionId.value === item.session.id || ['RECORDING', 'PAUSED', 'FINALIZING'].includes(item.session.status) || ['reconstructing', 'asr', 'reporting'].includes(serverProcessing.value[item.session.id]?.status ?? '')
}

function isApiNotFound(error: unknown): boolean {
  return error instanceof Error && /服务器请求失败 \(404\)/.test(error.message)
}

function deleteSession(item: DebugSession): void {
  if (isSessionDeletionBlocked(item)) return
  const hasUploadedChunks = item.segments.some((segment) => segment.chunks.some((chunk) => chunk.uploadStatus === 'UPLOADED'))
  const serverOnline = uploadSnapshot.value.serverOnline === true
  if (!serverOnline && hasUploadedChunks) {
    errorMessage.value = '服务器当前离线，无法安全删除服务器副本；请恢复连接后再删除该 Session。'
    return
  }
  if (pendingSessionDeleteId.value === item.session.id) {
    void confirmSessionDelete(item)
    return
  }
  pendingSessionDeleteId.value = item.session.id
}

function cancelSessionDelete(): void {
  pendingSessionDeleteId.value = null
}

async function confirmSessionDelete(item: DebugSession): Promise<void> {
  if (pendingSessionDeleteId.value !== item.session.id || isSessionDeletionBlocked(item)) return
  pendingSessionDeleteId.value = null
  const serverOnline = uploadSnapshot.value.serverOnline === true

  deletingSessionId.value = item.session.id
  uploadQueue.stop()
  try {
    await uploadQueue.pauseSessionAndWait(item.session.id)
    if (serverOnline) {
      try {
        await ApiClient.deleteSession(item.session.id)
      } catch (error) {
        if (!isApiNotFound(error)) throw new Error(`服务器副本删除失败，已保留本机 Session：${error instanceof Error ? error.message : '请求失败'}`)
      }
    }
    const segments = await SegmentStore.listBySessionId(item.session.id)
    for (const segment of segments) {
      const segmentUrl = restoredUrls.value[segment.id]
      if (segmentUrl) URL.revokeObjectURL(segmentUrl)
    }
    await deleteSessionData(item.session.id)

    const nextRestoredUrls = { ...restoredUrls.value }
    for (const segment of segments) delete nextRestoredUrls[segment.id]
    restoredUrls.value = nextRestoredUrls
    const nextRestoredMeta = { ...restoredMeta.value }
    for (const segment of segments) delete nextRestoredMeta[segment.id]
    restoredMeta.value = nextRestoredMeta

    const sessionUrl = restoredSessionUrls.value[item.session.id]
    if (sessionUrl) URL.revokeObjectURL(sessionUrl)
    const nextSessionUrls = { ...restoredSessionUrls.value }
    delete nextSessionUrls[item.session.id]
    restoredSessionUrls.value = nextSessionUrls
    const nextSessionMeta = { ...restoredSessionMeta.value }
    delete nextSessionMeta[item.session.id]
    restoredSessionMeta.value = nextSessionMeta

    const nextProcessing = { ...serverProcessing.value }
    delete nextProcessing[item.session.id]
    serverProcessing.value = nextProcessing
    if (selectedSessionId.value === item.session.id) selectedSessionId.value = null
    recoverySessions.value = recoverySessions.value.filter((session) => session.id !== item.session.id)
    if (recoverySession.value?.id === item.session.id) recoverySession.value = recoverySessions.value[0] ?? null
    if (currentSession.value?.id === item.session.id) {
      currentSession.value = null
      sessionMarkers.value = []
      lifecycleEvents.value = []
      sessionUpload.value = { total: 0, uploaded: 0, pending: 0, failed: 0 }
      resetCurrentView()
    }
    debugSessions.value = debugSessions.value.filter((entry) => entry.session.id !== item.session.id)
    void refreshDebugData()
    void uploadQueue.refreshSnapshot()
  } catch (error) {
    pendingSessionDeleteId.value = item.session.id
    errorMessage.value = error instanceof Error ? `删除 Session 失败：${error.message}` : '删除 Session 失败。'
  } finally {
    deletingSessionId.value = null
    uploadQueue.resumeSession(item.session.id)
    startUploadIfEnabled()
  }
}

const markerTypeLabels: Record<MarkerType, string> = {
  KEY_POINT: '重点',
  QUESTION: '疑问',
  IDEA: '灵感',
  TODO: '待办',
}

function markerTypeLabel(type: MarkerType): string { return markerTypeLabels[type] }

async function refreshSessionUpload(sessionId = currentSession.value?.id): Promise<void> {
  const refreshToken = ++sessionUploadRefreshToken
  if (!sessionId) {
    const empty = { total: 0, uploaded: 0, pending: 0, failed: 0 }
    if (!sameUploadSummary(sessionUpload.value, empty)) sessionUpload.value = empty
    if (!sameUploadSummary(selectedSessionUpload.value, empty)) selectedSessionUpload.value = empty
    return
  }
  try {
    const snapshot = await uploadQueue.snapshotForSession(sessionId)
    if (refreshToken !== sessionUploadRefreshToken) return
    if (currentSession.value?.id === sessionId && !sameUploadSummary(sessionUpload.value, snapshot)) sessionUpload.value = snapshot
    if (selectedSessionId.value === sessionId && !sameUploadSummary(selectedSessionUpload.value, snapshot)) selectedSessionUpload.value = snapshot
  } catch {
    // Preserve the last confirmed count during a transient IndexedDB read
    // failure; a temporary read failure must not flash the detail back to 0/0.
  }
}

function updateRestoredSessionDuration(event: Event, sessionId: string): void {
  const audio = event.currentTarget as HTMLAudioElement
  const durationMs = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration * 1000 : null
  const current = restoredSessionMeta.value[sessionId]
  if (!durationMs || !current || Math.abs(current.durationMs - durationMs) < 1) return
  restoredSessionMeta.value = { ...restoredSessionMeta.value, [sessionId]: { ...current, durationMs } }
}

function updateRestoredSessionPlayback(event: Event, sessionId: string): void {
  if (sessionId !== selectedSessionId.value) return
  const audio = event.currentTarget as HTMLAudioElement
  if (Number.isFinite(audio.currentTime) && audio.currentTime >= 0) playbackCurrentTimeMs.value = audio.currentTime * 1000
}

function setRestoredSessionPlaybackState(isPlaying: boolean, sessionId: string): void {
  if (sessionId === selectedSessionId.value) playbackIsPlaying.value = isPlaying
}

async function toggleRestoredSessionPlayback(): Promise<void> {
  const audio = restoredSessionAudio.value
  if (!audio) return
  if (audio.paused) {
    try { await audio.play() } catch { playbackPreparationMessage.value = '播放失败，请点击“播放录音”重试。' }
  } else {
    audio.pause()
  }
}

async function handleSessionPlayback(sessionId: string): Promise<void> {
  if (sessionId !== selectedSessionId.value) return
  const preparedUrl = restoredSessionUrls.value[sessionId]
  if (preparedUrl) {
    await nextTick()
    if (restoredSessionAudio.value?.src === preparedUrl) {
      await toggleRestoredSessionPlayback()
      return
    }
  }
  await restoreSessionForPlayback(sessionId)
}

function seekRestoredSession(event: Event, sessionId: string): void {
  if (sessionId !== selectedSessionId.value) return
  const audio = restoredSessionAudio.value
  const percent = Number((event.target as HTMLInputElement).value)
  if (!audio || !Number.isFinite(percent) || playbackDurationMs.value <= 0) return
  audio.currentTime = (percent / 100) * (playbackDurationMs.value / 1000)
  playbackCurrentTimeMs.value = audio.currentTime * 1000
}

function seekPlaybackToMs(sessionId: string, elapsedMs: number): boolean {
  if (sessionId !== selectedSessionId.value || !Number.isFinite(elapsedMs)) return false
  const audio = restoredSessionAudio.value
  if (!audio || !restoredSessionUrls.value[sessionId]) return false
  const knownDurationMs = playbackDurationMs.value
    || restoredSessionMeta.value[sessionId]?.durationMs
    || debugSessions.value.find((item) => item.session.id === sessionId)?.session.durationMs
    || 0
  const targetMs = Math.max(0, Math.min(elapsedMs, knownDurationMs > 0 ? knownDurationMs : elapsedMs))
  try {
    audio.currentTime = targetMs / 1000
    playbackCurrentTimeMs.value = targetMs
    playbackPreparationMessage.value = `已定位到 ${formatDuration(targetMs)}，正在播放。`
    void audio.play().catch(() => { playbackPreparationMessage.value = `已定位到 ${formatDuration(targetMs)}，点击“播放录音”继续。` })
    return true
  } catch {
    return false
  }
}

async function seekToMarker(marker: MarkerRecord): Promise<void> {
  if (marker.sessionId !== selectedSessionId.value || !canPlayDebugSession(selectedDebugSession.value)) return
  pendingPlaybackSeek.value = { sessionId: marker.sessionId, elapsedMs: marker.elapsedMs }
  if (restoredSessionUrls.value[marker.sessionId]) {
    await nextTick()
    if (seekPlaybackToMs(marker.sessionId, marker.elapsedMs)) pendingPlaybackSeek.value = null
    return
  }
  await handleSessionPlayback(marker.sessionId)
}

function updateRestoredSegmentDuration(event: Event, segmentId: string): void {
  const audio = event.currentTarget as HTMLAudioElement
  const durationMs = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration * 1000 : null
  const current = restoredMeta.value[segmentId]
  if (!durationMs || !current || Math.abs(current.durationMs - durationMs) < 1) return
  restoredMeta.value = { ...restoredMeta.value, [segmentId]: { ...current, durationMs } }
}

function setRestoredSessionAudioElement(element: unknown): void {
  const audio = typeof HTMLAudioElement !== 'undefined' && element instanceof HTMLAudioElement ? element : null
  restoredSessionAudio.value = audio
  if (!audio) { playbackCurrentTimeMs.value = 0; playbackIsPlaying.value = false }
  activePlaybackDiagnostics?.attachMediaElement(audio)
}

async function playPreparedSessionAudio(url: string): Promise<void> {
  await nextTick()
  const audio = restoredSessionAudio.value
  if (!audio || audio.src !== url) return
  try {
    await audio.play()
    playbackPreparationMessage.value = '正在播放，进度条按音频实际时长显示。'
  } catch {
    playbackPreparationMessage.value = '音频已准备好，可直接播放。'
  }
}

function playbackBuildId(): string {
  return typeof __LIVENOTE_BUILD_ID__ === 'string' ? __LIVENOTE_BUILD_ID__ : 'unknown-build'
}

function serializeSettings(settings: MediaTrackSettings | undefined): Record<string, unknown> {
  if (!settings) return {}
  const keys: Array<keyof MediaTrackSettings> = ['sampleRate', 'sampleSize', 'channelCount', 'noiseSuppression', 'autoGainControl', 'echoCancellation', 'deviceId', 'groupId']
  const result: Record<string, unknown> = {}
  for (const key of keys) {
    const value = settings[key]
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') result[key] = value
  }
  return result
}

async function persistChunk(chunk: RecorderChunk, session: SessionRecord, segment: SegmentRecord): Promise<void> {
  const record: ChunkRecord = {
    id: `${segment.id}-chunk-${chunk.index}`,
    sessionId: session.id,
    segmentId: segment.id,
    index: chunk.index,
    blob: chunk.blob,
    size: chunk.blob.size,
    mimeType: chunk.mimeType,
    createdAt: Date.now(),
    wallClockMs: chunk.wallClockMs,
    elapsedMs: chunk.elapsedMs,
    uploadStatus: 'PENDING',
    sha256: '',
    retryCount: 0,
    lastUploadAttemptAt: null,
    uploadedAt: null,
  }
  await ChunkStore.put(record)
  let persistedRecord = record
  try {
    persistedRecord = { ...record, sha256: await sha256Blob(chunk.blob) }
    await ChunkStore.put(persistedRecord)
  } catch (error) {
    errorMessage.value = error instanceof Error ? `Chunk 已本地保存，但 SHA256 计算失败：${error.message}` : 'Chunk 已本地保存，但 SHA256 计算失败。'
  }
  lastSavedChunk.value = persistedRecord
  savedChunkCount.value += 1
  currentSegmentChunkCount.value += 1
  currentChunkIndex.value = persistedRecord.index
  savedBytes.value += persistedRecord.size
  elapsedMs.value = Math.max(elapsedMs.value, persistedRecord.elapsedMs)
  void refreshStorageEstimate()
  const updatedSession: SessionRecord = { ...session, durationMs: Math.max(session.durationMs, persistedRecord.elapsedMs), updatedAt: persistedRecord.createdAt }
  await SessionStore.put(updatedSession)
  currentSession.value = updatedSession
  // The initial refresh runs before the first recorder chunk exists. Refresh
  // after persistence so the recording tab immediately reflects the current
  // session's total, even when the upload queue is offline or already busy.
  await refreshSessionUpload(session.id)
  uploadQueue.kick()
}

async function persistLifecycleEvent(event: LifecycleEvent): Promise<void> {
  const session = currentSession.value
  if (!session || !isRecording.value) return
  const record: LifecycleEventRecord = { id: createId('lifecycle'), sessionId: session.id, ...event }
  lifecycleEvents.value = [...lifecycleEvents.value, record]
  await LifecycleStore.put(record).catch(() => undefined)
}

async function loadLifecycleEvents(sessionId: string): Promise<void> {
  lifecycleEvents.value = await LifecycleStore.listBySessionId(sessionId).catch(() => [])
}

async function loadSessionMarkers(sessionId: string): Promise<void> {
  sessionMarkers.value = await MarkerStore.listBySessionId(sessionId).catch(() => [])
}

async function markOpenSegmentsInterrupted(segments: SegmentRecord[], endedAt: number): Promise<void> {
  await Promise.all(segments.filter((segment) => segment.status === 'RECORDING').map(async (segment) => {
    const chunks = await ChunkStore.listMetadataBySegmentId(segment.id)
    const lastChunkElapsedMs = chunks.reduce((max, chunk) => Math.max(max, chunk.elapsedMs), segment.startElapsedMs ?? 0)
    const durationMs = Math.max(segment.durationMs, Math.max(0, lastChunkElapsedMs - (segment.startElapsedMs ?? 0)))
    await SegmentStore.put({ ...segment, status: 'INTERRUPTED', endedAt, durationMs })
  }))
}

async function addMarker(type: MarkerType): Promise<void> {
  const session = currentSession.value
  if (!session || !isRecording.value) return
  const note = markerNoteDraft.value.trim()
  markerNoteDraft.value = ''
  const now = Date.now()
  const marker: MarkerRecord = {
    id: createId('marker'),
    sessionId: session.id,
    type,
    elapsedMs: elapsedMs.value,
    wallClockMs: now,
    note,
    createdAt: now,
    uploadStatus: 'PENDING',
    retryCount: 0,
    lastUploadAttemptAt: null,
    uploadedAt: null,
  }
  await MarkerStore.put(marker)
  sessionMarkers.value = [...sessionMarkers.value, marker].sort((a, b) => a.elapsedMs - b.elapsedMs || a.createdAt - b.createdAt)
  lastMarkerMessage.value = `${type} 已本地保存 · ${formatDuration(marker.elapsedMs)}`
  uploadQueue.kick()
}

async function createSession(): Promise<SessionRecord> {
  const now = Date.now()
  const session: SessionRecord = { id: createId('session'), title: `LN ${new Date(now).toLocaleString()}`, startedAt: now, endedAt: null, status: 'RECORDING', durationMs: 0, createdAt: now, updatedAt: now }
  await SessionStore.put(session)
  currentSession.value = session
  return session
}

async function createSegment(session: SessionRecord): Promise<SegmentRecord> {
  const segment: SegmentRecord = { id: createId('segment'), sessionId: session.id, index: await SegmentStore.nextIndex(session.id), startedAt: Date.now(), startElapsedMs: session.durationMs, endedAt: null, mimeType: '', mediaSettings: {}, status: 'RECORDING', durationMs: 0 }
  await SegmentStore.put(segment)
  currentSegment.value = segment
  return segment
}

async function hydrateSessionStats(sessionId: string, segmentId?: string): Promise<SessionRecord | null> {
  const chunks = await ChunkStore.listMetadataBySessionId(sessionId)
  savedChunkCount.value = chunks.length
  currentSegmentChunkCount.value = segmentId ? chunks.filter((chunk) => chunk.segmentId === segmentId).length : 0
  currentChunkIndex.value = chunks.length ? chunks[chunks.length - 1].index : -1
  savedBytes.value = chunks.reduce((total, chunk) => total + chunk.size, 0)
  lastSavedChunk.value = chunks[chunks.length - 1] ?? null
  const storedSession = await SessionStore.get(sessionId)
  if (!storedSession) return null
  const chunkDurationMs = chunks.reduce((max, chunk) => Math.max(max, chunk.elapsedMs), 0)
  const durationMs = Math.max(storedSession.durationMs, chunkDurationMs)
  const hydratedSession = durationMs > storedSession.durationMs
    ? { ...storedSession, durationMs, updatedAt: Date.now() }
    : storedSession
  if (hydratedSession !== storedSession) await SessionStore.put(hydratedSession)
  currentSession.value = hydratedSession
  elapsedMs.value = durationMs
  return hydratedSession
}

async function hydratePersistedSessionDuration(session: SessionRecord): Promise<SessionRecord> {
  const chunks = await ChunkStore.listMetadataBySessionId(session.id)
  const durationMs = Math.max(session.durationMs, ...chunks.map((chunk) => chunk.elapsedMs), 0)
  if (durationMs <= session.durationMs) return session
  const updated = { ...session, durationMs, updatedAt: Date.now() }
  await SessionStore.put(updated)
  return updated
}

function startDurationTimer(baseMs: number): void {
  stopDurationTimer()
  activeElapsedBaseMs = baseMs
  activeMonotonicStartedAt = performance.now()
  elapsedMs.value = baseMs
  durationTimer = window.setInterval(() => { elapsedMs.value = activeElapsedBaseMs + Math.max(0, performance.now() - activeMonotonicStartedAt) }, 200)
  stopSessionCheckpointTimer()
  sessionCheckpointTimer = window.setInterval(() => { void persistSessionCheckpoint() }, 5_000)
}

function stopDurationTimer(): void {
  if (durationTimer !== null) { window.clearInterval(durationTimer); durationTimer = null }
}

function stopSessionCheckpointTimer(): void {
  if (sessionCheckpointTimer !== null) { window.clearInterval(sessionCheckpointTimer); sessionCheckpointTimer = null }
}

function currentElapsedMs(): number {
  const liveElapsed = isRecording.value && activeMonotonicStartedAt > 0
    ? activeElapsedBaseMs + Math.max(0, performance.now() - activeMonotonicStartedAt)
    : elapsedMs.value
  return Math.max(elapsedMs.value, liveElapsed, currentSession.value?.durationMs ?? 0, lastSavedChunk.value?.elapsedMs ?? 0)
}

async function markCurrentRecordingInterrupted(endedAt = Date.now(), elapsedOverride?: number): Promise<void> {
  const session = currentSession.value
  const segment = currentSegment.value
  const sessionDurationMs = Math.max(session?.durationMs ?? 0, elapsedOverride ?? currentElapsedMs())
  if (segment && segment.status === 'RECORDING') {
    await SegmentStore.put({
      ...segment,
      status: 'INTERRUPTED',
      endedAt,
      durationMs: Math.max(segment.durationMs, sessionDurationMs - activeSegmentBaseMs),
    }).catch(() => undefined)
  }
  if (session && ['RECORDING', 'PAUSED', 'FINALIZING'].includes(session.status)) {
    const interruptedSession = { ...session, status: 'INTERRUPTED' as const, endedAt, durationMs: sessionDurationMs, updatedAt: endedAt }
    await SessionStore.put(interruptedSession).catch(() => undefined)
    currentSession.value = interruptedSession
    if (!recoverySessions.value.some((item) => item.id === interruptedSession.id)) recoverySessions.value = [interruptedSession, ...recoverySessions.value]
    recoverySession.value ??= interruptedSession
  }
}

function handleRecorderFailure(error: Error): void {
  if (recorderFailurePromise) return
  const interruptedAt = Date.now()
  const interruptedElapsedMs = currentElapsedMs()
  recorderFailurePromise = (async () => {
    recorderState.value = 'error'
    stopDurationTimer()
    stopSessionCheckpointTimer()
    pageLifecycleManager.setRecording(false)
    levelMonitor.stop()
    await markCurrentRecordingInterrupted(interruptedAt, interruptedElapsedMs)
    await wakeLockManager.release()
    engine.dispose()
    isBusy.value = false
    errorMessage.value = `${error.message} 已保留已保存 Chunk，可从“继续录音”恢复。`
    await refreshDebugData()
  })().finally(() => { recorderFailurePromise = null })
  void recorderFailurePromise
}

function persistSessionCheckpoint(force = false): Promise<void> {
  const session = currentSession.value
  if (!session || !isRecording.value) return Promise.resolve()
  const durationMs = currentElapsedMs()
  if (!force && durationMs <= session.durationMs) return Promise.resolve()

  sessionCheckpointChain = sessionCheckpointChain.then(async () => {
    const latest = await SessionStore.get(session.id)
    if (!latest || latest.status !== 'RECORDING') return
    const nextDurationMs = Math.max(latest.durationMs, durationMs)
    if (!force && nextDurationMs <= latest.durationMs) return
    const updatedSession: SessionRecord = { ...latest, durationMs: nextDurationMs, updatedAt: Date.now() }
    await SessionStore.put(updatedSession)
    if (currentSession.value?.id === updatedSession.id) currentSession.value = updatedSession
  }).catch((error) => {
    errorMessage.value = error instanceof Error ? `录音时长检查点保存失败：${error.message}` : '录音时长检查点保存失败。'
  })
  return sessionCheckpointChain
}

async function startRecording(): Promise<void> {
  // Recovery calls this function after temporarily hiding the recovery banner.
  // A manual start must still be blocked while an unfinished Session awaits a choice.
  if (!storageReady.value || !deviceIdentity.value || isBusy.value || isRecording.value || comparisonComplete.value || recoverySession.value) return
  errorMessage.value = ''
  storageError.value = ''
  isBusy.value = true
  void requestPersistentStorage()
  let session: SessionRecord | null = currentSession.value
  let segment: SegmentRecord | null = null
  try {
    if (!session || (!comparisonMode.value && session.status === 'COMPLETED')) session = await createSession()
    else session = (await hydrateSessionStats(session.id)) ?? session
    segment = await createSegment(session)
    currentSegmentChunkCount.value = 0
    const sessionElapsedBaseMs = session.durationMs
    activeSegmentBaseMs = sessionElapsedBaseMs
    const mediaSession = await engine.start(selectedProfile.value, {
      timesliceMs: CHUNK_TIMESLICE_MS,
      sessionElapsedBaseMs,
      onChunk: (chunk) => persistChunk(chunk, session as SessionRecord, segment as SegmentRecord),
      onChunkError: (error) => {
        const message = error.message || 'Chunk 本地保存失败。'
        errorMessage.value = `本地保存失败：${message}。录音已停止，避免继续产生未保存音频。`
        storageError.value = message
        if (recorderState.value === 'recording' && !isBusy.value) {
          void stopRecording()
        }
      },
      onRecorderError: handleRecorderFailure,
    })
    const settings = mediaSession.stream.getAudioTracks()[0]?.getSettings()
    const updatedSegment: SegmentRecord = { ...segment, startedAt: mediaSession.segmentStartedAt, mimeType: mediaSession.mimeType, mediaSettings: serializeSettings(settings) }
    await SegmentStore.put(updatedSegment)
    currentSegment.value = updatedSegment
    currentMimeType.value = mediaSession.mimeType
    recorderState.value = 'recording'
    pageLifecycleManager.setRecording(true, sessionElapsedBaseMs)
    startDurationTimer(sessionElapsedBaseMs)
    await levelMonitor.start(mediaSession.stream)
    await wakeLockManager.request()
    await loadLifecycleEvents(session.id)
    await loadSessionMarkers(session.id)
    await refreshSessionUpload(session.id)
    uploadQueue.kick()
  } catch (error) {
    recorderState.value = 'error'
    stopDurationTimer()
    stopSessionCheckpointTimer()
    pageLifecycleManager.setRecording(false)
    engine.dispose()
    await markCurrentRecordingInterrupted()
    errorMessage.value = recordingErrorMessage(error, '无法开始录音。')
  } finally { isBusy.value = false }
}

async function stopRecording(): Promise<void> {
  if (!isRecording.value || isBusy.value) return
  isBusy.value = true
  recorderState.value = 'stopping'
  stopDurationTimer()
  stopSessionCheckpointTimer()
  pageLifecycleManager.setRecording(false)
  let completedSessionId: string | null = null
  try {
    const result = await engine.stop()
    const endedAt = Date.now()
    const segment = currentSegment.value
    const session = currentSession.value
    if (segment && session) {
      const completedSegment: SegmentRecord = { ...segment, endedAt, status: 'COMPLETED', durationMs: Math.max(0, result.durationMs - activeSegmentBaseMs) }
      await SegmentStore.put(completedSegment)
      currentSegment.value = completedSegment
      if (comparisonMode.value && comparisonIndex.value < COMPARISON_ORDER.length - 1) {
        const updatedSession = { ...session, status: 'RECORDING' as const, durationMs: result.durationMs, updatedAt: endedAt }
        await SessionStore.put(updatedSession)
        currentSession.value = updatedSession
        comparisonIndex.value += 1
        selectedProfile.value = COMPARISON_ORDER[comparisonIndex.value]
      } else {
        const completedSession = { ...session, status: 'COMPLETED' as const, endedAt, durationMs: result.durationMs, updatedAt: endedAt }
        await SessionStore.put(completedSession)
        currentSession.value = completedSession
        completedSessionId = completedSession.id
        if (comparisonMode.value) comparisonIndex.value = COMPARISON_ORDER.length
      }
    }
    elapsedMs.value = result.durationMs
    recorderState.value = 'ready'
    void uploadQueue.flush()
    if (segment && session) {
      void refreshSessionUpload(session.id)
      await hydrateSessionStats(session.id, segment.id)
      await refreshSessionUpload(session.id)
    }
    await refreshDebugData()
    if (completedSessionId) void prepareSessionPlaybackInBackground(completedSessionId)
  } catch (error) {
    recorderState.value = 'error'
    await markCurrentRecordingInterrupted()
    errorMessage.value = recordingErrorMessage(error, '无法停止录音。')
  } finally { pageLifecycleManager.setRecording(false); levelMonitor.stop(); await wakeLockManager.release(); engine.dispose(); stopSessionCheckpointTimer(); isBusy.value = false }
}

function beginComparison(): void {
  if (!canStart.value) return
  comparisonMode.value = true
  comparisonIndex.value = 0
  selectedProfile.value = COMPARISON_ORDER[0]
  currentSession.value = null
  void refreshSessionUpload()
  resetCurrentView()
}

async function endComparison(): Promise<void> {
  if (isRecording.value || isBusy.value) return
  if (currentSession.value?.status === 'RECORDING') {
    const now = Date.now()
    const completed = { ...currentSession.value, status: 'COMPLETED' as const, endedAt: now, updatedAt: now }
    await SessionStore.put(completed)
    currentSession.value = completed
  }
  comparisonMode.value = false
  comparisonIndex.value = 0
  await refreshDebugData()
}

function resetCurrentView(): void {
  if (isRecording.value) return
  recorderState.value = 'idle'
  errorMessage.value = ''
  currentSegment.value = null
  currentMimeType.value = '—'
  savedChunkCount.value = 0
  currentSegmentChunkCount.value = 0
  currentChunkIndex.value = -1
  savedBytes.value = 0
  lastSavedChunk.value = null
  elapsedMs.value = currentSession.value?.durationMs ?? 0
}

async function resetTest(): Promise<void> {
  if (isRecording.value) return
  if (comparisonMode.value) await endComparison()
  currentSession.value = null
  await refreshSessionUpload()
  resetCurrentView()
}

function clearLocalTestData(): void {
  if (isRecording.value || isBusy.value) return
  pendingClearLocalData.value = true
}

function cancelClearLocalData(): void {
  pendingClearLocalData.value = false
}

async function confirmClearLocalData(): Promise<void> {
  if (!pendingClearLocalData.value || isRecording.value || isBusy.value) return
  pendingClearLocalData.value = false
  try {
    uploadQueue.stop()
    Object.values(restoredUrls.value).forEach((url) => URL.revokeObjectURL(url))
    Object.values(restoredSessionUrls.value).forEach((url) => URL.revokeObjectURL(url))
    await clearAllData()
    currentSession.value = null
    currentSegment.value = null
    recoverySession.value = null
    recoverySessions.value = []
    sessionMarkers.value = []
    lifecycleEvents.value = []
    debugSessions.value = []
    restoredUrls.value = {}
    restoredMeta.value = {}
    restoredSessionUrls.value = {}
    restoredSessionMeta.value = {}
    await uploadQueue.refreshSnapshot()
    resetCurrentView()
    lastMarkerMessage.value = '手机本地录音已清理。'
    startUploadIfEnabled()
  } catch (error) {
    errorMessage.value = error instanceof Error ? `清理本地数据失败：${error.message}` : '清理本地数据失败。'
    startUploadIfEnabled()
  }
}

async function loadStorage(): Promise<void> {
  if (!capabilities.value.indexedDB) { storageError.value = '当前浏览器没有 IndexedDB，无法进入 M3 可靠录音模式。'; return }
  try {
    await openDatabase()
    storageReady.value = true
    const openSessions = await SessionStore.listOpen()
    const hydratedOpenSessions = await Promise.all(openSessions.map((session) => hydratePersistedSessionDuration(session)))
    recoverySessions.value = hydratedOpenSessions
    recoverySession.value = hydratedOpenSessions[0] ?? null
    await refreshSessionUpload(currentSession.value?.id)
    await refreshDebugData()
  } catch (error) { storageError.value = error instanceof Error ? error.message : '无法打开 IndexedDB。' }
}

async function continueRecovery(): Promise<void> {
  if (!recoverySession.value || recoveryInProgress.value || isBusy.value || isRecording.value) return
  recoveryInProgress.value = true
  const recoveryId = recoverySession.value.id
  recoverySession.value = null
  try {
    const latestSession = await SessionStore.get(recoveryId)
    if (!latestSession) throw new Error('待恢复的 Session 不存在。')
    const existingChunks = await ChunkStore.listMetadataBySessionId(recoveryId)
    const recoveredDurationMs = Math.max(latestSession.durationMs, ...existingChunks.map((chunk) => chunk.elapsedMs), 0)
    const recoverySegments = await SegmentStore.listBySessionId(recoveryId)
    await markOpenSegmentsInterrupted(recoverySegments, Date.now())
    const session = { ...latestSession, status: 'RECORDING' as const, endedAt: null, durationMs: recoveredDurationMs, updatedAt: Date.now() }
    await SessionStore.put(session)
    currentSession.value = session
    comparisonMode.value = false
    resetCurrentView()
    await startRecording()
    if (isRecording.value) {
      recoverySessions.value = recoverySessions.value.filter((item) => item.id !== recoveryId)
      recoverySession.value = recoverySessions.value[0] ?? null
    } else {
      recoverySession.value = recoverySessions.value.find((item) => item.id === recoveryId) ?? null
    }
  } catch (error) {
    recoverySession.value = recoverySessions.value.find((item) => item.id === recoveryId) ?? null
    errorMessage.value = error instanceof Error ? `恢复录音失败：${error.message}` : '恢复录音失败。'
  } finally {
    recoveryInProgress.value = false
  }
}

async function finishRecovery(): Promise<void> {
  if (recoveryInProgress.value || isBusy.value || isRecording.value) return
  const session = recoverySession.value
  if (!session) return
  recoverySession.value = null
  try {
    const hydratedSession = await hydratePersistedSessionDuration(session)
    const segments = await SegmentStore.listBySessionId(session.id)
    const endedAt = Date.now()
    await markOpenSegmentsInterrupted(segments, endedAt)
    await SessionStore.put({ ...hydratedSession, status: 'COMPLETED' as const, endedAt, updatedAt: endedAt })
    recoverySessions.value = recoverySessions.value.filter((item) => item.id !== session.id)
    recoverySession.value = recoverySessions.value[0] ?? null
    await refreshDebugData()
    void prepareSessionPlaybackInBackground(session.id)
  } catch (error) {
    recoverySession.value = session
    errorMessage.value = error instanceof Error ? `结束恢复 Session 失败：${error.message}` : '结束恢复 Session 失败。'
  }
}

async function refreshDebugData(): Promise<void> {
  if (!storageReady.value) return
  const localSessions = await SessionStore.list()
  const sessions = [...localSessions]
  const remoteStats = new Map<string, { segmentCount: number; chunkCount: number; liveProcessing: LiveProcessingStatus | null }>()
  try {
    const remote = await ApiClient.listSessions('', 0, 100)
    const localIds = new Set(localSessions.map((session) => session.id))
    for (const item of remote.items) {
      remoteStats.set(item.id, { segmentCount: item.segmentCount, chunkCount: item.chunkCount, liveProcessing: item.liveProcessing })
      if (localIds.has(item.id) || deletingSessionId.value === item.id) continue
      const status: SessionRecord['status'] = ['RECORDING', 'PAUSED', 'FINALIZING', 'INTERRUPTED', 'COMPLETED'].includes(item.status)
        ? item.status as SessionRecord['status']
        : 'COMPLETED'
      sessions.push({ id: item.id, title: item.title || '未命名会话', startedAt: item.startedAt, endedAt: item.endedAt, status, durationMs: item.durationMs, createdAt: item.createdAt, updatedAt: item.updatedAt })
    }
  } catch {
    // An unpaired/offline phone can still browse its local recordings.
  }
  const nested: DebugSession[] = []
  for (const session of sessions) {
    const segments = await SegmentStore.listBySessionId(session.id)
    const serverOnly = !localSessions.some((localSession) => localSession.id === session.id)
    const serverCounts = remoteStats.get(session.id)
    nested.push({
      session,
      segments: await Promise.all(segments.map(async (segment) => ({
        segment,
        // The debug tree only needs metadata. Keep audio Blob values out of
        // Vue state; playback reads the same Chunk records on demand.
        chunks: await ChunkStore.listMetadataBySegmentId(segment.id),
      }))),
      markers: await MarkerStore.listBySessionId(session.id),
      serverOnly,
      serverSegmentCount: serverOnly ? serverCounts?.segmentCount ?? 0 : undefined,
      serverChunkCount: serverOnly ? serverCounts?.chunkCount ?? 0 : undefined,
      liveProcessing: serverCounts?.liveProcessing ?? null,
    })
  }
  debugSessions.value = nested
  // The phone only reads the server-side task/result state. It never starts
  // ASR, downloads audio, or asks the user to move files into ChatGPT.
  const taskStates = await Promise.all(sessions.map(async (session) => {
    const response = await ApiClient.getSessionResult(session.id).catch(() => null)
    if (response?.result) {
      await ResultStore.put({ sessionId: session.id, ownerId: deviceIdentity.value?.userId ?? null, revisionId: response.revisionId ?? null, version: response.version, status: response.status, updatedAt: response.updatedAt, result: response.result })
      return [session.id, response] as const
    }
    const cached = await ResultStore.get(session.id)
    if (cached) {
      return [session.id, { sessionId: cached.sessionId, taskId: '', status: cached.status, version: cached.version, updatedAt: cached.updatedAt, result: cached.result, revisionId: cached.revisionId }] as const
    }
    return response ? [session.id, response] as const : null
  }))
  const restoredProcessing: Record<string, ServerProcessingState> = {}
  for (const entry of taskStates) {
    if (!entry) continue
    const [sessionId, response] = entry
    if (response.result) {
      restoredProcessing[sessionId] = {
        status: 'completed',
        message: '电脑端已完成总结，手机可直接查看。',
        report: serverResultToReport(response, sessions.find((item) => item.id === sessionId)),
        startedAt: response.updatedAt,
      }
    } else if (response.status === 'FAILED') {
      restoredProcessing[sessionId] = { status: 'error', message: '电脑端处理失败，等待重试。', startedAt: response.updatedAt }
    } else {
      restoredProcessing[sessionId] = { status: 'queued', message: taskStatusMessage(response.status), startedAt: response.updatedAt }
    }
  }
  serverProcessing.value = restoredProcessing
}

async function refreshLiveProcessingStatuses(): Promise<void> {
  const candidates = debugSessions.value.filter((item) => item.session.status !== 'COMPLETED' || item.liveProcessing)
  if (!candidates.length) return
  const updates = await Promise.all(candidates.map(async (item) => [item.session.id, (await ApiClient.getLiveProcessing(item.session.id).catch(() => null))?.liveProcessing ?? null] as const))
  const updateMap = new Map(updates)
  debugSessions.value = debugSessions.value.map((item) => updateMap.has(item.session.id) ? { ...item, liveProcessing: updateMap.get(item.session.id) ?? null } : item)
}

async function restoreSegmentForPlayback(segmentId: string): Promise<void> {
  if (restoringSegmentId.value) return
  const previousUrl = restoredUrls.value[segmentId]
  if (previousUrl) URL.revokeObjectURL(previousUrl)
  restoringSegmentId.value = segmentId
  restoredMeta.value = { ...restoredMeta.value }
  try {
    const restored = await createSegmentPlayback(segmentId, (url) => {
      restoredUrls.value = { ...restoredUrls.value, [segmentId]: url }
    })
    restoredMeta.value = { ...restoredMeta.value, [segmentId]: restored }
  } catch (error) { errorMessage.value = error instanceof Error ? error.message : '无法恢复 Segment。' }
  finally { restoringSegmentId.value = null }
}

async function restoreSessionForPlayback(sessionId: string): Promise<void> {
  if (restoringSessionId.value) return
  activePlaybackDiagnostics?.record('playback-attempt-replaced')
  activePlaybackDiagnostics?.dispose()
  const previousUrl = restoredSessionUrls.value[sessionId]
  if (previousUrl) { activePlaybackDiagnostics?.record('source-revoked'); URL.revokeObjectURL(previousUrl) }
  const nextRestoredUrls = { ...restoredSessionUrls.value }
  delete nextRestoredUrls[sessionId]
  restoredSessionUrls.value = nextRestoredUrls
  const nextRestoredMeta = { ...restoredSessionMeta.value }
  delete nextRestoredMeta[sessionId]
  restoredSessionMeta.value = nextRestoredMeta
  restoringSessionId.value = sessionId
  playbackAttemptSessionId.value = sessionId
  playbackCurrentTimeMs.value = 0
  playbackIsPlaying.value = false
  playbackPreparationMessage.value = '正在加载音频…'
  try {
    const debugSession = debugSessions.value.find((item) => item.session.id === sessionId)
    const chunks = debugSession?.segments.flatMap((segment) => segment.chunks) ?? []
    const localUploadComplete = isSessionUploadComplete(debugSession)
    const serverPlaybackAvailable = Boolean(debugSession && canPlayDebugSession(debugSession))
    const diagnostics = new PlaybackDiagnostics({
      sessionId,
      segmentCount: debugSession?.segments.length ?? 0,
      chunkCount: chunks.length || debugSession?.serverChunkCount || 0,
      durationMs: debugSession?.session.durationMs ?? 0,
      mimeType: debugSession?.segments.find((segment) => segment.segment.mimeType)?.segment.mimeType || 'audio/webm',
      localUploadComplete,
    }, playbackBuildId())
    activePlaybackDiagnostics = diagnostics
    diagnostics.record('restore-requested', { localUploadComplete, hasLocalChunks: chunks.length > 0 })
    // A server reconstruction is only authoritative after every local Chunk
    // is confirmed uploaded. Otherwise the server may legitimately contain
    // only the first part of a Session and return a shorter audio file.
    if (debugSession && serverPlaybackAvailable && (debugSession.serverOnly || localUploadComplete)) {
      playbackPreparationMessage.value = debugSession.serverOnly
        ? '正在加载服务器音频…'
        : '正在下载完整音频…'
      try {
        const blob = await ApiClient.downloadSessionAudio(sessionId, diagnostics)
        const url = URL.createObjectURL(blob)
        diagnostics.record('source-assigned', { mode: 'server-ffmpeg', sourceId: diagnostics.nextSourceId(), bytes: blob.size })
        restoredSessionUrls.value = { ...restoredSessionUrls.value, [sessionId]: url }
        restoredSessionMeta.value = { ...restoredSessionMeta.value, [sessionId]: { blob, url, playbackMode: 'server-ffmpeg', segmentCount: debugSession.serverOnly ? debugSession.serverSegmentCount ?? 0 : debugSession.segments.length, chunkCount: debugSession.serverOnly ? debugSession.serverChunkCount ?? 0 : chunks.length, totalBytes: blob.size, durationMs: debugSession.session.durationMs, mimeType: blob.type || 'audio/webm', hasGaps: debugSession.serverOnly ? false : debugSession.segments.some((segment) => segment.chunks.some((chunk, index) => chunk.index !== index)) } }
        return
      } catch (error) {
        diagnostics.preparation('server-audio-fallback', { reason: 'server-audio-unavailable', errorCategory: error instanceof Error && error.name === 'AbortError' ? 'timeout' : 'request-failed' })
        // Server reconstruction is preferred for multi-Segment sessions, but
        // a temporary API failure must not remove the local playback path.
        // MediaSource may still reject incompatible fragments; that failure
        // is handled by the existing user-facing error below.
      }
    }

    playbackPreparationMessage.value = `正在加载音频（0/${chunks.length} 个分片）…`
    const restored = await createSessionPlayback(sessionId, (url) => {
      diagnostics.record('source-assigned', { mode: 'local', sourceId: diagnostics.nextSourceId() })
      restoredSessionUrls.value = { ...restoredSessionUrls.value, [sessionId]: url }
    }, diagnostics, (progress) => {
      if (progress.phase === 'loading') playbackPreparationMessage.value = `正在加载音频（0/${progress.totalChunks} 个分片）…`
      else if (progress.phase === 'appending') playbackPreparationMessage.value = `正在处理音频片段（${progress.completedChunks}/${progress.totalChunks}）…`
      else playbackPreparationMessage.value = '音频已准备好，可直接播放。'
    })
    restoredSessionMeta.value = { ...restoredSessionMeta.value, [sessionId]: restored }
  } catch (error) {
    activePlaybackDiagnostics?.record('restore-error', { errorCategory: 'restore-failed' })
    errorMessage.value = error instanceof Error ? error.message : '无法重组整场 Session。'
    playbackPreparationMessage.value = '音频加载失败，请稍后重试。'
  }
  finally {
    restoringSessionId.value = null
    const preparedUrl = restoredSessionUrls.value[sessionId]
    if (preparedUrl) {
      await nextTick()
      const pendingSeek = pendingPlaybackSeek.value?.sessionId === sessionId ? pendingPlaybackSeek.value : null
      if (pendingSeek && seekPlaybackToMs(sessionId, pendingSeek.elapsedMs)) {
        pendingPlaybackSeek.value = null
      } else {
        playbackPreparationMessage.value = '音频已加载，正在播放…'
        void playPreparedSessionAudio(preparedUrl)
      }
    }
  }
}

async function prepareSessionPlaybackInBackground(sessionId: string): Promise<void> {
  if (backgroundPlaybackPreparationSessionId || isRecording.value || restoringSessionId.value) return
  const debugSession = debugSessions.value.find((item) => item.session.id === sessionId)
  if (!debugSession || debugSession.serverOnly || !canPlayDebugSession(debugSession) || restoredSessionUrls.value[sessionId]) return
  backgroundPlaybackPreparationSessionId = sessionId
  restoringSessionId.value = sessionId
  playbackAttemptSessionId.value = sessionId
  playbackPreparationMessage.value = '正在后台加载刚完成的录音…'
  try {
    const restored = await restoreSession(sessionId)
    if (isRecording.value) return
    const url = URL.createObjectURL(restored.blob)
    restoredSessionUrls.value = { ...restoredSessionUrls.value, [sessionId]: url }
    restoredSessionMeta.value = { ...restoredSessionMeta.value, [sessionId]: { ...restored, url, playbackMode: 'blob-fallback' } }
    playbackPreparationMessage.value = '录音已加载，打开历史记录即可播放。'
  } catch {
    playbackPreparationMessage.value = '后台加载未完成，点击播放时会自动重试。'
  } finally {
    backgroundPlaybackPreparationSessionId = null
    if (restoringSessionId.value === sessionId) restoringSessionId.value = null
  }
}

async function uploadPlaybackDiagnostic(): Promise<void> {
  const diagnostics = activePlaybackDiagnostics
  if (!diagnostics) {
    playbackFeedback.value = { status: 'error', message: '请先点击“播放录音”，复现问题后再反馈。' }
    return
  }
  if (playbackFeedback.value.status === 'collecting' || playbackFeedback.value.status === 'uploading') return
  playbackFeedback.value = { status: 'collecting', message: '正在收集播放现场…' }
  diagnostics.record('feedback-requested', { descriptionProvided: Boolean(playbackFeedbackDescription.value.trim()) })
  if (playbackFeedbackTimer !== null) window.clearTimeout(playbackFeedbackTimer)
  await new Promise<void>((resolve) => { playbackFeedbackTimer = window.setTimeout(resolve, 5_000) })
  playbackFeedbackTimer = null
  const report: PlaybackDiagnosticReport = diagnostics.freeze(playbackFeedbackDescription.value.trim() || '用户反馈播放进度异常')
  playbackFeedback.value = { status: 'uploading', message: '正在上传播放诊断…' }
  try {
    const result = await saveAndUploadPlaybackFeedback(report, deviceIdentity.value?.userId ?? null)
    if (result.status === 'sent') playbackFeedback.value = { status: 'success', message: `已收到播放诊断（编号：${result.diagnosticId}）`, id: result.diagnosticId }
    else playbackFeedback.value = { status: 'pending', message: '已保存，网络恢复后自动发送。' }
  } catch (error) {
    playbackFeedback.value = { status: 'error', message: error instanceof Error ? error.message : '播放诊断发送失败，可稍后重试。' }
  }
}

function taskStatusMessage(status: ServerResultResponse['status']): string {
  return {
    READY: '已上传，等待电脑端领取。',
    CLAIMED: '电脑端已领取，等待处理。',
    LOCAL_READY: '电脑端已领取到本机，等待开始处理。',
    PROCESSING: '电脑端正在处理录音。',
    REVIEW: '总结草稿已生成，等待电脑端发布。',
    READY_TO_UPLOAD: '总结已生成，等待回传服务器。',
    COMPLETED: '电脑端已完成总结，手机可直接查看。',
    FAILED: '电脑端处理失败，等待重试。',
  }[status] ?? '等待电脑端处理。'
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function asText(value: unknown): string { return typeof value === 'string' ? value.trim() : '' }

function asTextList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => typeof item === 'string' ? item.trim() : '').filter(Boolean) : []
}

function serverResultToReport(response: ServerResultResponse, session?: SessionRecord): ReportResponse {
  const root = asRecord(response.result)
  const source = Object.keys(asRecord(root.summary)).length ? asRecord(root.summary) : root
  const structure = Array.isArray(source.knowledgeStructure)
    ? source.knowledgeStructure.map((item) => {
      const section = asRecord(item)
      return { title: asText(section.title) || '知识要点', points: asTextList(section.points) }
    }).filter((item) => item.points.length)
    : []
  const questions = Array.isArray(source.questions)
    ? source.questions.map((item) => {
      if (typeof item === 'string') return { question: item, answer: '', startMs: null }
      const question = asRecord(item)
      return { question: asText(question.question), answer: asText(question.answer), startMs: typeof question.startMs === 'number' ? question.startMs : null }
    }).filter((item) => item.question || item.answer)
    : []
  const transcript = asRecord(root.transcript)
  return {
    ok: true,
    sessionId: response.sessionId,
    version: response.version,
    processingMode: 'server-worker',
    summaryStatus: 'GENERATED',
    session: { id: session?.id, title: session?.title, durationMs: session?.durationMs },
    summary: {
      title: asText(source.title) || session?.title || '直播总结',
      overview: asText(source.overview) || '暂无总结内容。',
      keyPoints: asTextList(source.keyPoints),
      knowledgeStructure: structure,
      questions,
      actionItems: asTextList(source.actionItems),
      entities: asTextList(source.entities),
      confidenceNotes: asTextList(source.confidenceNotes),
    },
    transcript: {
      model: '电脑端处理',
      device: 'server-worker',
      language: 'zh',
      text: asText(transcript.text),
      segments: Array.isArray(transcript.segments) ? transcript.segments as TranscriptResponse['segments'] : [],
    },
    counts: { transcriptSegments: Array.isArray(transcript.segments) ? transcript.segments.length : 0 },
  }
}

function refreshCapabilities(): void { capabilities.value = detectAudioCapabilities(); void loadStorage() }

function debugSessionChunkCount(item: DebugSession): number {
  if (item.serverOnly) return item.serverChunkCount ?? 0
  return item.segments.reduce((total, segment) => total + segment.chunks.length, 0)
}

function canPlayDebugSession(item: DebugSession | null | undefined): boolean {
  return hasPlayableSession(item)
}

function isSessionUploadComplete(item: DebugSession | null | undefined): boolean {
  return Boolean(item?.segments.length)
    && item!.segments.every((segment) => segment.chunks.length > 0 && segment.chunks.every((chunk) => chunk.uploadStatus === 'UPLOADED'))
}

function summaryTitle(report: ReportResponse | undefined): string {
  return report?.summary?.title || report?.session?.title || '直播总结'
}

function summaryOverview(report: ReportResponse | undefined): string {
  return report?.summary?.overview || report?.localDraft?.overviewPreview || '暂无总结内容。'
}

function summaryKeyPoints(report: ReportResponse | undefined): string[] {
  return report?.summary?.keyPoints ?? report?.localDraft?.keyPoints.map((item) => item.note || '').filter(Boolean) ?? []
}

function summaryKnowledgeStructure(report: ReportResponse | undefined): Array<{ title: string; points: string[] }> {
  return report?.summary?.knowledgeStructure ?? report?.localDraft?.knowledgeStructure ?? []
}

function summaryActionItems(report: ReportResponse | undefined): string[] {
  return report?.summary?.actionItems ?? report?.localDraft?.todos.map((item) => item.note || '').filter(Boolean) ?? []
}

function summaryQuestions(report: ReportResponse | undefined): Array<{ question: string; answer: string }> {
  if (report?.summary?.questions?.length) return report.summary.questions.map((item) => ({ question: item.question, answer: item.answer || '' })).filter((item) => item.question || item.answer)
  return report?.localDraft?.questions.map((item) => ({ question: item.note || '', answer: '' })).filter((item) => item.question) ?? []
}

watch([errorMessage, storageError], ([message, storageMessage], [previousMessage, previousStorageMessage]) => {
  if (message !== previousMessage || storageMessage !== previousStorageMessage) diagnosticFeedback.value = { status: 'idle', message: '' }
})

watch(selectedSessionId, (sessionId) => {
  if (sessionId === playbackAttemptSessionId.value) return
  playbackFeedback.value = { status: 'idle', message: '' }
  playbackFeedbackDescription.value = ''
  playbackFeedbackExpanded.value = false
  pendingPlaybackSeek.value = null
  playbackAttemptSessionId.value = null
  playbackPreparationMessage.value = ''
})

function buildDiagnosticSnapshot(trigger = 'error-feedback'): Record<string, unknown> {
  return {
    schemaVersion: 2,
    createdAt: new Date().toISOString(),
    trigger,
    url: window.location.href,
    userAgent: navigator.userAgent,
    activeTab: activeTab.value,
    capabilities: capabilities.value,
    recorder: { state: recorderState.value, profile: selectedProfile.value, mimeType: currentMimeType.value, elapsedMs: elapsedMs.value },
    current: { sessionId: currentSession.value?.id ?? null, segmentId: currentSegment.value?.id ?? null, savedChunkCount: savedChunkCount.value, currentSegmentChunkCount: currentSegmentChunkCount.value, currentChunkIndex: currentChunkIndex.value, savedBytes: savedBytes.value },
    page: { visibility: pageVisibility.value, networkOnline: networkOnline.value, wakeLock: wakeLockState.value, wakeLockMessage: wakeLockMessage.value },
    upload: { serverOnline: uploadSnapshot.value.serverOnline, serverCompatible: uploadSnapshot.value.serverCompatible, isUploading: uploadSnapshot.value.isUploading, total: uploadSnapshot.value.total, uploaded: uploadSnapshot.value.uploaded, pending: uploadSnapshot.value.pending, failed: uploadSnapshot.value.failed, lastError: uploadSnapshot.value.lastError },
    sessionUpload: sessionUpload.value,
    selectedSessionId: selectedSessionId.value,
    errorMessage: errorMessage.value || storageError.value,
    storageError: storageError.value,
    serverProcessing: Object.fromEntries(Object.entries(serverProcessing.value).map(([id, state]) => [id, { status: state.status, message: state.message, jobId: state.jobId }])),
    lifecycleEvents: lifecycleEvents.value.slice(-30),
    sessions: debugSessions.value.map((item) => ({ id: item.session.id, status: item.session.status, durationMs: item.session.durationMs, segmentCount: item.segments.length, chunkCount: item.segments.reduce((total, segment) => total + segment.chunks.length, 0), chunkStatuses: item.segments.flatMap((segment) => segment.chunks.map((chunk) => chunk.uploadStatus)) })),
  }
}

async function uploadDiagnostic(reason: string, trigger: string, attachment: File | null = null): Promise<void> {
  if (diagnosticFeedback.value.status === 'uploading') return
  diagnosticFeedback.value = { status: 'uploading', message: '正在自动反馈…' }
  try {
    const result = await ApiClient.uploadDiagnosticSnapshot(buildDiagnosticSnapshot(trigger), reason, attachment)
    diagnosticFeedback.value = { status: 'success', message: `已自动反馈（编号：${result.diagnosticId}）`, id: result.diagnosticId }
  } catch (error) {
    diagnosticFeedback.value = { status: 'error', message: error instanceof Error ? error.message : '自动反馈失败，请稍后再试。' }
  }
}

async function uploadAutomaticDiagnostic(): Promise<void> {
  return uploadDiagnostic('用户点击错误提示中的自动反馈问题', 'error-feedback')
}

async function uploadManualDiagnostic(): Promise<void> {
  const attachment = diagnosticImage.value
  await uploadDiagnostic('用户在设置页主动上传诊断数据', 'manual-settings', attachment)
  if (diagnosticFeedback.value.status === 'success') diagnosticImage.value = null
}

function handleDiagnosticImageSelected(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  if (!file) return
  if (!file.type.startsWith('image/')) {
    diagnosticFeedback.value = { status: 'error', message: '请选择图片文件。' }
    input.value = ''
    return
  }
  if (file.size > 10 * 1024 * 1024) {
    diagnosticFeedback.value = { status: 'error', message: '图片不能超过 10MB。' }
    input.value = ''
    return
  }
  diagnosticImage.value = file
  diagnosticFeedback.value = { status: 'idle', message: '' }
}

function handleNetworkOffline(): void {
  networkOnline.value = false
  activePlaybackDiagnostics?.record('network-offline')
}

function handleNetworkOnline(): void {
  networkOnline.value = true
  activePlaybackDiagnostics?.record('network-online')
  void flushPendingPlaybackFeedback(deviceIdentity.value?.userId ?? null)
  void resultSyncManager.request()
}

onMounted(() => { pwaInstallManager.start(); window.addEventListener('offline', handleNetworkOffline); window.addEventListener('online', handleNetworkOnline); wakeLockManager.start(); pageLifecycleManager.start(); startUploadIfEnabled(); resultSyncManager.start(); void refreshStorageEstimate(); storageEstimateTimer = window.setInterval(() => { void refreshStorageEstimate() }, 30_000); liveProcessingTimer = window.setInterval(() => { void refreshLiveProcessingStatuses() }, 15_000); void loadDeviceIdentity(); void loadStorage() })
onBeforeUnmount(() => { pwaInstallManager.stop(); unsubscribePwaInstall(); window.removeEventListener('offline', handleNetworkOffline); window.removeEventListener('online', handleNetworkOnline); stopDurationTimer(); stopSessionCheckpointTimer(); if (storageEstimateTimer !== null) window.clearInterval(storageEstimateTimer); if (liveProcessingTimer !== null) window.clearInterval(liveProcessingTimer); if (playbackFeedbackTimer !== null) window.clearTimeout(playbackFeedbackTimer); activePlaybackDiagnostics?.dispose(); resultSyncManager.stop(); pageLifecycleManager.stop(); levelMonitor.stop(); wakeLockManager.stop(); uploadQueue.stop(); unsubscribeUploadQueue(); engine.dispose(); Object.values(restoredUrls.value).forEach((url) => URL.revokeObjectURL(url)); Object.values(restoredSessionUrls.value).forEach((url) => URL.revokeObjectURL(url)) })
</script>

<template>
  <main class="app-shell" :class="{ 'recording-shell': activeTab === 'recording', 'knowledge-mode': Boolean(knowledgeViewReport) }">
    <section v-if="!deviceIdentityResolved" class="identity-gate identity-gate-loading" aria-labelledby="identity-gate-title" aria-busy="true">
      <div class="identity-gate-mark" aria-hidden="true">L</div>
      <h1 id="identity-gate-title">正在恢复手机身份</h1>
      <p>正在验证本机是否已经绑定，请稍候…</p>
    </section>
    <section v-else-if="!deviceIdentity && activeTab !== 'settings'" class="identity-gate" aria-labelledby="identity-gate-title">
      <div class="identity-gate-mark" aria-hidden="true">L</div>
      <h1 id="identity-gate-title">先绑定手机身份</h1>
      <p>绑定后才能录音、上传和查看属于你的会话。</p>
      <button class="primary-button identity-gate-button" type="button" @click="openSettings">去绑定手机</button>
    </section>
    <section v-else-if="knowledgeViewReport" class="knowledge-page" :class="['share-theme-' + summaryTheme, { 'knowledge-capture-mode': knowledgeCaptureMode }]" :style="knowledgePageStyle" aria-label="知识卡阅读页" @click="knowledgeCaptureMode = false; knowledgeFontControlExpanded = false">
      <div class="knowledge-controls">
        <button class="knowledge-back" type="button" @click="closeKnowledgeView" aria-label="返回会话">←</button>
        <div class="knowledge-control-actions">
          <button class="knowledge-edit" type="button" @click="knowledgeEditing ? cancelKnowledgeEdit() : beginKnowledgeEdit()">{{ knowledgeEditing ? '退出编辑' : '编辑' }}</button>
          <div class="knowledge-font-control" :class="{ expanded: knowledgeFontControlExpanded }" aria-label="字体大小" @click.stop><button class="knowledge-font-toggle" type="button" :aria-expanded="knowledgeFontControlExpanded" aria-label="调整字体大小" @click="knowledgeFontControlExpanded = !knowledgeFontControlExpanded">A</button><div v-if="knowledgeFontControlExpanded" class="knowledge-font-slider"><input type="range" min="85" max="160" step="5" :value="Math.round(knowledgeFontScale * 100)" aria-label="字体大小" :aria-valuetext="`${Math.round(knowledgeFontScale * 100)}%`" @input="setKnowledgeFontScale" /></div></div>
          <label class="knowledge-font-family" aria-label="字体"><span class="sr-only">字体</span><select v-model="knowledgeFontFamily"><option v-for="font in KNOWLEDGE_FONT_FAMILIES" :key="font.id" :value="font.id">{{ font.name }}</option></select></label>
          <button v-if="!knowledgeEditing" class="knowledge-capture" type="button" aria-label="截图" @click.stop="knowledgeCaptureMode = true"><svg class="knowledge-capture-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M2.5 12s3.35-5.2 9.5-5.2 9.5 5.2 9.5 5.2-3.35 5.2-9.5 5.2S2.5 12 2.5 12Z" /><circle cx="12" cy="12" r="2.7" /></svg></button>
          <label class="knowledge-theme-select" aria-label="纸张类型"><span class="sr-only">纸张类型</span><select v-model="summaryTheme"><option v-for="theme in SUMMARY_THEMES" :key="theme.id" :value="theme.id">{{ theme.name }}</option></select></label>
        </div>
      </div>
      <form class="knowledge-sheet knowledge-card-editor" :class="{ 'is-editing': knowledgeEditing }" @submit.prevent="saveKnowledgeDraft">
         <p v-if="knowledgeDraft && !knowledgeEditing" class="knowledge-draft-note">本机草稿 · {{ knowledgeDraftMessage || '已保存的修改' }}</p>
         <h1 v-if="!knowledgeEditing">{{ summaryTitle(knowledgeViewReport) }}</h1>
           <div v-if="knowledgeEditing" class="knowledge-card-title-edit"><textarea id="knowledge-title-editor" v-model="knowledgeEditForm.title" rows="1" maxlength="160" aria-label="知识卡标题" placeholder="输入知识卡标题" @input="resizeKnowledgeTitle"></textarea></div>
        <section v-if="!knowledgeEditing && summaryOverview(knowledgeViewReport) !== '暂无总结内容。'" class="knowledge-section knowledge-read-section" :class="{ expanded: isKnowledgeSectionExpanded('overview') }" aria-label="概览"><div class="knowledge-read-section-heading" role="button" tabindex="0" :aria-expanded="isKnowledgeSectionExpanded('overview')" @click="toggleKnowledgeSection('overview')" @keydown.enter.prevent="toggleKnowledgeSection('overview')" @keydown.space.prevent="toggleKnowledgeSection('overview')"><h2>概览</h2><span class="knowledge-read-toggle" aria-hidden="true"></span></div><p v-if="isKnowledgeSectionExpanded('overview')" class="knowledge-overview">{{ summaryOverview(knowledgeViewReport) }}</p></section>
         <section v-else-if="knowledgeEditing" class="knowledge-section knowledge-inline-overview" :class="{ expanded: isKnowledgeSectionExpanded('overview') }" aria-label="概览编辑"><div class="knowledge-inline-section-heading" role="button" tabindex="0" :aria-expanded="isKnowledgeSectionExpanded('overview')" @click="toggleKnowledgeSection('overview')" @keydown.enter.prevent="toggleKnowledgeSection('overview')" @keydown.space.prevent="toggleKnowledgeSection('overview')"><div class="knowledge-inline-heading-copy"><h2>概览</h2><span>一段话说明这场录音讲了什么</span></div><span class="knowledge-inline-toggle" aria-hidden="true">{{ isKnowledgeSectionExpanded('overview') ? '收起' : '展开' }}</span></div><div v-if="isKnowledgeSectionExpanded('overview')" class="knowledge-inline-section-body"><label class="knowledge-edit-field-label" for="knowledge-overview-editor">概览内容</label><textarea id="knowledge-overview-editor" v-model="knowledgeEditForm.overview" class="knowledge-inline-textarea" rows="10" maxlength="2000" placeholder="输入概览内容"></textarea><button class="knowledge-field-fullscreen" type="button" @click="openKnowledgeOverviewFullscreen">全屏</button></div></section>
         <section v-if="!knowledgeEditing && summaryKnowledgeStructure(knowledgeViewReport).length" class="knowledge-section knowledge-read-section" :class="{ expanded: isKnowledgeSectionExpanded('structure') }"><div class="knowledge-read-section-heading" role="button" tabindex="0" :aria-expanded="isKnowledgeSectionExpanded('structure')" @click="toggleKnowledgeSection('structure')" @keydown.enter.prevent="toggleKnowledgeSection('structure')" @keydown.space.prevent="toggleKnowledgeSection('structure')"><h2>知识结构</h2><span class="knowledge-read-toggle" aria-hidden="true"></span></div><div v-if="isKnowledgeSectionExpanded('structure')"><div v-for="(section, index) in summaryKnowledgeStructure(knowledgeViewReport)" :key="'knowledge-structure-' + index" class="knowledge-structure"><h3>{{ section.title }}</h3><ul><li v-for="(point, pointIndex) in section.points" :key="'knowledge-structure-point-' + index + '-' + pointIndex">{{ point }}</li></ul></div></div></section>
         <section v-else-if="knowledgeEditing" class="knowledge-section knowledge-inline-edit-section" :class="{ expanded: isKnowledgeSectionExpanded('structure') }">
           <div class="knowledge-inline-section-heading">
             <div class="knowledge-inline-section-trigger" role="button" tabindex="0" :aria-expanded="isKnowledgeSectionExpanded('structure')" @click="toggleKnowledgeSection('structure')" @keydown.enter.prevent="toggleKnowledgeSection('structure')" @keydown.space.prevent="toggleKnowledgeSection('structure')">
               <div class="knowledge-inline-heading-copy"><h2>知识结构</h2><span>按层级整理主题和知识点</span></div>
               <span class="knowledge-inline-toggle" aria-hidden="true">{{ isKnowledgeSectionExpanded('structure') ? '收起' : '展开' }}</span>
             </div>
          </div>
          <div v-if="isKnowledgeSectionExpanded('structure')" class="knowledge-inline-section-body">
            <div v-for="(section, index) in knowledgeEditForm.knowledgeStructure" :key="`edit-structure-${index}`" class="knowledge-inline-structure" :class="{ expanded: isKnowledgeStructureExpanded(index) }">
               <div class="knowledge-inline-title-row">
                 <div class="knowledge-inline-structure-trigger" role="button" tabindex="0" :aria-expanded="isKnowledgeStructureExpanded(index)" @click="toggleKnowledgeStructure(index)" @keydown.enter.prevent="toggleKnowledgeStructure(index)" @keydown.space.prevent="toggleKnowledgeStructure(index)">
                   <span class="knowledge-inline-structure-index">{{ String(index + 1).padStart(2, '0') }}</span><span class="knowledge-inline-structure-title">{{ section.title || '未命名结构' }}</span>
                   <span class="knowledge-inline-structure-toggle" aria-hidden="true">{{ isKnowledgeStructureExpanded(index) ? '收起' : '展开' }}</span>
                 </div>
                 <div class="knowledge-inline-item-actions">
                   <button class="knowledge-remove" type="button" @click.stop="removeKnowledgeStructure(index)">移除</button>
                 </div>
               </div>
               <div v-if="isKnowledgeStructureExpanded(index)" class="knowledge-inline-structure-body">
                 <label class="knowledge-edit-field-label" :for="`knowledge-structure-title-${index}`">结构标题</label>
                 <input :id="`knowledge-structure-title-${index}`" v-model="section.title" maxlength="120" placeholder="例如：第一部分 · 核心概念" />
                  <textarea :id="`knowledge-structure-points-${index}`" v-model="section.points" rows="7" maxlength="3000" aria-label="知识点内容" placeholder="每行一条知识点"></textarea>
               </div>
            </div>
            <p v-if="!knowledgeEditForm.knowledgeStructure.length" class="knowledge-inline-empty">暂无知识结构，点击标题后的“添加”开始编辑。</p>
          </div>
        </section>
        <section v-if="!knowledgeEditing && summaryKeyPoints(knowledgeViewReport).length" class="knowledge-section knowledge-read-section" :class="{ expanded: isKnowledgeSectionExpanded('keyPoints') }"><div class="knowledge-read-section-heading" role="button" tabindex="0" :aria-expanded="isKnowledgeSectionExpanded('keyPoints')" @click="toggleKnowledgeSection('keyPoints')" @keydown.enter.prevent="toggleKnowledgeSection('keyPoints')" @keydown.space.prevent="toggleKnowledgeSection('keyPoints')"><h2>关键知识点</h2><span class="knowledge-read-toggle" aria-hidden="true"></span></div><ul v-if="isKnowledgeSectionExpanded('keyPoints')" class="knowledge-list"><li v-for="(point, index) in summaryKeyPoints(knowledgeViewReport)" :key="'knowledge-point-' + index">{{ point }}</li></ul></section>
         <section v-else-if="knowledgeEditing" class="knowledge-section knowledge-inline-edit-section" :class="{ expanded: isKnowledgeSectionExpanded('keyPoints') }"><div class="knowledge-inline-section-heading" role="button" tabindex="0" :aria-expanded="isKnowledgeSectionExpanded('keyPoints')" @click="toggleKnowledgeSection('keyPoints')" @keydown.enter.prevent="toggleKnowledgeSection('keyPoints')" @keydown.space.prevent="toggleKnowledgeSection('keyPoints')"><div class="knowledge-inline-heading-copy"><h2>关键知识点</h2><span>提炼最值得记住的内容</span></div><span class="knowledge-inline-toggle" aria-hidden="true">{{ isKnowledgeSectionExpanded('keyPoints') ? '收起' : '展开' }}</span></div><div v-if="isKnowledgeSectionExpanded('keyPoints')" class="knowledge-inline-section-body"><textarea id="knowledge-key-points-editor" v-model="knowledgeEditForm.keyPoints" class="knowledge-inline-textarea" rows="7" maxlength="6000" aria-label="关键知识点" placeholder="每行一条知识点"></textarea></div></section>
        <section v-if="!knowledgeEditing && summaryQuestions(knowledgeViewReport).length" class="knowledge-section knowledge-read-section" :class="{ expanded: isKnowledgeSectionExpanded('questions') }"><div class="knowledge-read-section-heading" role="button" tabindex="0" :aria-expanded="isKnowledgeSectionExpanded('questions')" @click="toggleKnowledgeSection('questions')" @keydown.enter.prevent="toggleKnowledgeSection('questions')" @keydown.space.prevent="toggleKnowledgeSection('questions')"><h2>重要问答</h2><span class="knowledge-read-toggle" aria-hidden="true"></span></div><ul v-if="isKnowledgeSectionExpanded('questions')" class="knowledge-list knowledge-question-list"><li v-for="(question, index) in summaryQuestions(knowledgeViewReport)" :key="'knowledge-question-' + index"><span class="knowledge-question-line">问：{{ question.question }}</span><span v-if="question.answer" class="knowledge-question-line">答：{{ question.answer }}</span></li></ul></section>
          <section v-else-if="knowledgeEditing" class="knowledge-section knowledge-inline-edit-section" :class="{ expanded: isKnowledgeSectionExpanded('questions') }"><div class="knowledge-inline-section-heading" role="button" tabindex="0" :aria-expanded="isKnowledgeSectionExpanded('questions')" @click="toggleKnowledgeSection('questions')" @keydown.enter.prevent="toggleKnowledgeSection('questions')" @keydown.space.prevent="toggleKnowledgeSection('questions')"><div class="knowledge-inline-heading-copy"><h2>重要问答</h2><span>保留问题和对应的回答</span></div><span class="knowledge-inline-toggle" aria-hidden="true">{{ isKnowledgeSectionExpanded('questions') ? '收起' : '展开' }}</span></div><div v-if="isKnowledgeSectionExpanded('questions')" class="knowledge-inline-section-body"><div v-for="(item, index) in knowledgeEditForm.questions" :key="`edit-question-${index}`" class="knowledge-inline-question"><div class="knowledge-inline-title-row"><div class="knowledge-inline-field"><label class="knowledge-edit-field-label" :for="`knowledge-question-${index}`">问题</label><input :id="`knowledge-question-${index}`" v-model="item.question" maxlength="500" placeholder="输入问题" /></div></div><div class="knowledge-inline-field"><label class="knowledge-edit-field-label" :for="`knowledge-answer-${index}`">回答</label><textarea :id="`knowledge-answer-${index}`" v-model="item.answer" rows="6" maxlength="2000" placeholder="输入回答"></textarea></div></div><p v-if="!knowledgeEditForm.questions.length" class="knowledge-inline-empty">暂无问答。</p></div></section>
        <section v-if="!knowledgeEditing && summaryActionItems(knowledgeViewReport).length" class="knowledge-section knowledge-read-section" :class="{ expanded: isKnowledgeSectionExpanded('actionItems') }"><div class="knowledge-read-section-heading" role="button" tabindex="0" :aria-expanded="isKnowledgeSectionExpanded('actionItems')" @click="toggleKnowledgeSection('actionItems')" @keydown.enter.prevent="toggleKnowledgeSection('actionItems')" @keydown.space.prevent="toggleKnowledgeSection('actionItems')"><h2>行动建议</h2><span class="knowledge-read-toggle" aria-hidden="true"></span></div><ul v-if="isKnowledgeSectionExpanded('actionItems')" class="knowledge-list"><li v-for="(itemText, index) in summaryActionItems(knowledgeViewReport)" :key="'knowledge-action-' + index">{{ itemText }}</li></ul></section>
         <section v-else-if="knowledgeEditing" class="knowledge-section knowledge-inline-edit-section" :class="{ expanded: isKnowledgeSectionExpanded('actionItems') }"><div class="knowledge-inline-section-heading" role="button" tabindex="0" :aria-expanded="isKnowledgeSectionExpanded('actionItems')" @click="toggleKnowledgeSection('actionItems')" @keydown.enter.prevent="toggleKnowledgeSection('actionItems')" @keydown.space.prevent="toggleKnowledgeSection('actionItems')"><div class="knowledge-inline-heading-copy"><h2>行动建议</h2><span>把下一步要做的事情写清楚</span></div><span class="knowledge-inline-toggle" aria-hidden="true">{{ isKnowledgeSectionExpanded('actionItems') ? '收起' : '展开' }}</span></div><div v-if="isKnowledgeSectionExpanded('actionItems')" class="knowledge-inline-section-body"><textarea id="knowledge-action-items-editor" v-model="knowledgeEditForm.actionItems" class="knowledge-inline-textarea" rows="6" maxlength="3000" aria-label="行动建议" placeholder="每行一条行动建议"></textarea></div></section>
         <p v-if="knowledgeEditing && knowledgeDraftMessage" class="knowledge-editor-message" role="status">{{ knowledgeDraftMessage }}</p><div v-if="knowledgeEditing" class="knowledge-editor-actions"><button class="knowledge-save" type="submit">保存修改</button><button class="knowledge-cancel" type="button" @click="cancelKnowledgeEdit">取消</button><button v-if="knowledgeDraft" class="knowledge-restore" type="button" @click="requestRestoreKnowledgeOriginal">恢复原稿</button></div>
        <div v-if="knowledgeEditing && knowledgeResetPending" class="knowledge-editor-confirm"><span>删除本机草稿并恢复服务器版本？</span><button class="knowledge-save" type="button" @click="restoreKnowledgeOriginal">确认恢复</button><button class="knowledge-cancel" type="button" @click="cancelRestoreKnowledgeOriginal">取消</button></div>
        <div v-if="knowledgeOverviewFullscreen" class="knowledge-overview-modal" role="dialog" aria-modal="true" aria-labelledby="knowledge-overview-modal-title">
          <button class="knowledge-overview-backdrop" type="button" aria-label="关闭概览编辑" @click="closeKnowledgeOverviewFullscreen"></button>
          <div class="knowledge-overview-dialog">
            <div class="knowledge-overview-dialog-heading"><strong id="knowledge-overview-modal-title">编辑概览</strong><button class="knowledge-overview-close" type="button" aria-label="关闭概览编辑" @click="closeKnowledgeOverviewFullscreen">×</button></div>
            <textarea v-model="knowledgeEditForm.overview" class="knowledge-overview-fullscreen-input" rows="12" maxlength="2000" autofocus></textarea>
            <button class="knowledge-overview-done" type="button" @click="closeKnowledgeOverviewFullscreen">完成</button>
          </div>
        </div>
        <div v-if="knowledgeFullscreenTarget" class="knowledge-overview-modal knowledge-fullscreen-modal" role="dialog" aria-modal="true" aria-labelledby="knowledge-fullscreen-modal-title">
          <button class="knowledge-overview-backdrop" type="button" aria-label="关闭全屏编辑" @click="closeKnowledgeFullscreenField"></button>
          <div class="knowledge-overview-dialog">
            <div class="knowledge-overview-dialog-heading"><strong id="knowledge-fullscreen-modal-title">编辑{{ knowledgeFullscreenTarget.label }}</strong><button class="knowledge-overview-close" type="button" aria-label="关闭全屏编辑" @click="closeKnowledgeFullscreenField">×</button></div>
            <textarea v-model="knowledgeFullscreenDraft" class="knowledge-overview-fullscreen-input" rows="12" maxlength="6000" autofocus></textarea>
            <button class="knowledge-overview-done" type="button" @click="saveKnowledgeFullscreenField">完成</button>
          </div>
        </div>
      </form>
    </section>
    <template v-else>
    <header class="topbar">
      <div class="brand-lockup">
        <div class="brand-mark" aria-hidden="true">L</div>
        <div><h1>LiveNote</h1></div>
        <button v-if="activeTab === 'recording' && shouldShowPwaGuide" class="topbar-install-link" type="button" @click="openSettings">立即安装</button>
      </div>
    </header>

    <section v-if="recoverySession" class="recovery-banner" aria-labelledby="recovery-title">
      <div><h2 id="recovery-title">发现未结束的录音</h2><p>{{ recoverySession.title }} · 已记录 {{ formatDuration(recoverySession.durationMs) }}<span v-if="recoverySessions.length > 1"> · 还有 {{ recoverySessions.length - 1 }} 场待处理</span></p></div>
      <div class="recovery-actions"><button class="primary-button compact-button" type="button" :disabled="isBusy || recoveryInProgress" @click="continueRecovery">{{ recoveryInProgress ? '正在恢复…' : '继续录音' }}</button><button class="stop-button compact-button" type="button" :disabled="isBusy || recoveryInProgress" @click="finishRecovery">结束并保存</button></div>
    </section>

    <section v-if="activeTab === 'recording'" class="app-view recording-view" aria-labelledby="recording-title">
      <section class="recording-hero">
        <div class="record-readout"><strong>{{ durationLabel }}</strong><span v-if="comparisonMode" class="record-profile-caption">{{ comparisonStepLabel }}</span><span class="record-upload-progress" :class="{ 'has-failed': sessionUpload.failed > 0 }" :style="{ '--upload-progress': `${recordUploadProgress}%` }" role="img" :aria-label="recordUploadLabel" :title="recordUploadLabel"><span>{{ sessionUpload.uploaded }}/{{ sessionUpload.total }}</span></span></div>
        <div class="level-meter" :class="`record-level-${selectedProfile}`" :aria-label="`实时麦克风音量，当前模式：${selectedProfileDetails.name}`"><span v-for="index in 18" :key="index" class="meter-segment" :class="{ lit: level >= index / 18 }"></span></div>
        <div class="control-actions"><button class="primary-button record-primary" :class="{ 'is-recording': isRecording }" type="button" :disabled="isBusy || (!isRecording && !canStart)" :aria-label="isRecording ? '停止并保存录音' : '开始录音'" @click="isRecording ? stopRecording() : startRecording()">{{ isRecording ? (comparisonMode && comparisonIndex < COMPARISON_ORDER.length - 1 ? '停止并下一组' : '停止并保存') : (comparisonMode ? '开始第 ' + (comparisonIndex + 1) + ' 组' : '开始录音') }}</button></div>
        <p v-if="!deviceIdentity" class="auth-required-note">录音前请先完成手机配对。<button class="text-button compact-button" type="button" @click="openSettings">打开设置</button></p>
        <div v-if="errorMessage" class="error-message error-feedback" role="alert"><span class="error-feedback-copy">{{ errorMessage }}</span><button v-if="diagnosticFeedback.status !== 'success'" class="text-button compact-button error-feedback-button" type="button" :disabled="diagnosticFeedback.status === 'uploading'" @click="uploadAutomaticDiagnostic">{{ diagnosticFeedback.status === 'uploading' ? '反馈中…' : '自动反馈问题' }}</button><span v-if="diagnosticFeedback.message" class="error-feedback-status" :class="'diagnostic-' + diagnosticFeedback.status">{{ diagnosticFeedback.message }}</span></div>
      </section>

      <div v-if="isRecording" class="marker-actions live-markers">
        <div class="live-marker-note-row"><span class="field-label">快速标记</span><input v-model="markerNoteDraft" class="marker-note-input" maxlength="400" placeholder="备注（可选）" /></div>
        <div class="live-marker-button-row"><button type="button" @click="addMarker('KEY_POINT')">标记重点</button><button type="button" @click="addMarker('QUESTION')">标记疑问</button></div>
        <small v-if="lastMarkerMessage">{{ lastMarkerMessage }}</small>
      </div>

      <div v-if="sessionMarkers.length" class="inline-marker-list"><div class="inline-marker-list-heading"><strong>本场标记</strong><button class="text-button" type="button" @click="openSessions(currentSession?.id)">查看全部</button></div><div class="inline-marker-items"><span v-for="marker in sessionMarkers.slice(-3).reverse()" :key="marker.id" class="inline-marker-item"><strong>{{ markerTypeLabel(marker.type) }}</strong><span>{{ formatDuration(marker.elapsedMs) }}</span></span></div></div>
      <div v-if="hasLifecycleRisk" class="lifecycle-warning"><strong>录音期间页面曾离开前台</strong><span>{{ formatDate(lastHiddenEvent?.wallClockMs ?? null) }} · 风险区间 {{ formatDuration(lifecycleGapMs) }}</span></div>
    </section>

    <section v-else-if="activeTab === 'sessions'" class="app-view sessions-view" aria-label="会话列表">
      <div v-if="errorMessage" class="error-message error-feedback session-error-message" role="alert"><span class="error-feedback-copy">{{ errorMessage }}</span><button v-if="diagnosticFeedback.status !== 'success'" class="text-button compact-button error-feedback-button" type="button" :disabled="diagnosticFeedback.status === 'uploading'" @click="uploadAutomaticDiagnostic">{{ diagnosticFeedback.status === 'uploading' ? '反馈中…' : '自动反馈问题' }}</button><span v-if="diagnosticFeedback.message" class="error-feedback-status" :class="'diagnostic-' + diagnosticFeedback.status">{{ diagnosticFeedback.message }}</span></div>
      <div class="session-toolbar"><div class="session-search-row"><label class="search-field"><span class="sr-only">搜索会话</span><input v-model="sessionQuery" type="search" placeholder="搜索标题或日期" /></label></div></div>
      <div class="sessions-overview-grid" aria-label="会话概览"><div class="sessions-overview-tile sessions-count-tile"><strong>{{ debugSessions.length }}<small> 场会话</small></strong></div><button class="sessions-overview-tile sessions-refresh-tile" type="button" aria-label="刷新会话列表" @click="refreshDebugData"><strong>刷新</strong></button></div>
      <div v-if="filteredDebugSessions.length" class="session-list"><details v-for="group in sessionGroups" :key="group.key" class="session-group" :open="group.label === '今天'" :aria-labelledby="`session-group-${group.key}`"><summary class="session-group-heading" :id="`session-group-${group.key}`"><strong>{{ group.label }}</strong><span>{{ group.items.length }} 场</span></summary><div class="session-group-items"><template v-for="item in group.items" :key="item.session.id"><article class="session-item" :class="{ selected: selectedSessionId === item.session.id, 'has-summary': Boolean(serverProcessing[item.session.id]?.report), 'has-processing': processingStatusClass(item.session.id) === 'session-processing-status-active' }"><form v-if="editingSessionId === item.session.id" class="session-edit-form" @submit.prevent="saveSessionTitle(item)"><input v-model="editingSessionTitle" aria-label="会话标题" maxlength="80" /><button class="primary-button compact-button" type="submit">保存</button><button class="text-button" type="button" @click="cancelSessionTitleEdit">取消</button></form><div class="session-row" role="button" tabindex="0" :aria-expanded="selectedSessionId === item.session.id" @click="openSessions(item.session.id)" @keydown.enter="openSessions(item.session.id)"><div class="session-row-main"><div class="session-row-title-line"><strong>{{ displaySessionTitle(item.session.title) }}</strong><span class="session-row-duration">{{ formatSessionDuration(item.session.durationMs) }}</span></div></div><div class="session-row-footer"><span class="session-row-statuses"><span v-if="sessionStatusLabel(item.session.status)" class="session-status" :class="'session-status-' + item.session.status.toLowerCase()">{{ sessionStatusLabel(item.session.status) }}</span><span v-if="sessionUploadLabel(item) && !sessionUploadComplete(item)" class="session-upload-status" :class="{ 'session-upload-status-pending': sessionUploadSummary(item).pending > 0, 'session-upload-status-failed': sessionUploadSummary(item).failed > 0 }">{{ sessionUploadLabel(item) }}</span><span v-if="serverProcessing[item.session.id] && !serverProcessing[item.session.id].report && processingStatusClass(item.session.id) !== 'session-processing-status-active'" class="session-processing-status" :class="processingStatusClass(item.session.id)">{{ processingStatusLabel(item.session.id) }}</span><span v-if="!sessionUploadComplete(item)">{{ debugSessionChunkCount(item) }} 块</span></span></div></div>

        <Transition name="session-detail">
        <section v-if="selectedSessionId === item.session.id && selectedDebugSession" class="session-detail" aria-labelledby="session-detail-title">
         <div class="session-detail-overview"><div class="session-recording-time"><span>时间</span><strong>{{ formatSessionDate(selectedDebugSession.session.startedAt) }}</strong></div><div class="session-current-status"><span>状态</span><strong>{{ sessionDetailStatusLabel(selectedDebugSession) }}</strong></div></div><div v-if="!restoredSessionUrls[selectedSessionId || '']" class="detail-actions"><button class="secondary-button" type="button" :disabled="restoringSessionId === selectedSessionId || !canPlayDebugSession(selectedDebugSession)" @click="handleSessionPlayback(selectedSessionId || '')">{{ restoringSessionId === selectedSessionId ? '正在加载…' : '播放录音' }}</button></div>
          <div v-if="restoredSessionUrls[selectedSessionId || '']" class="restore-result detail-player"><audio :ref="setRestoredSessionAudioElement" :key="restoredSessionUrls[selectedSessionId || '']" :src="restoredSessionUrls[selectedSessionId || '']" :controls="shouldShowPlaybackControls(restoringSessionId === selectedSessionId, restoredSessionUrls[selectedSessionId || '']) && restoredSessionMeta[selectedSessionId || '']?.playbackMode !== 'blob-fallback'" :aria-busy="restoringSessionId === selectedSessionId" preload="auto" @loadedmetadata="updateRestoredSessionDuration($event, selectedSessionId || '')" @durationchange="updateRestoredSessionDuration($event, selectedSessionId || '')" @timeupdate="updateRestoredSessionPlayback($event, selectedSessionId || '')" @play="setRestoredSessionPlaybackState(true, selectedSessionId || '')" @playing="setRestoredSessionPlaybackState(true, selectedSessionId || '')" @pause="setRestoredSessionPlaybackState(false, selectedSessionId || '')" @ended="setRestoredSessionPlaybackState(false, selectedSessionId || '')"></audio><div v-if="restoredSessionMeta[selectedSessionId || '']?.playbackMode === 'blob-fallback'" class="playback-fallback-controls"><button class="secondary-button compact-button" type="button" @click="toggleRestoredSessionPlayback">{{ playbackIsPlaying ? '暂停播放' : '开始播放' }}</button><label class="playback-progress-control"><span class="sr-only">播放进度</span><input type="range" min="0" max="100" step="0.1" :value="playbackProgress" :aria-valuetext="`${formatDuration(playbackCurrentTimeMs)} / ${formatDuration(playbackDurationMs)}`" @input="seekRestoredSession($event, selectedSessionId || '')" /></label><span class="playback-time-label">{{ formatDuration(playbackCurrentTimeMs) }} / {{ formatDuration(playbackDurationMs) }}</span></div></div>
         <div v-if="selectedProcessing?.status === 'error'" class="processing-error">{{ selectedProcessing.message }}</div><button v-if="selectedProcessing?.report" class="knowledge-open-button" type="button" @click="openKnowledgeView">知识卡</button>
         <section v-if="selectedDebugSession.markers.length" class="session-notes" aria-labelledby="session-notes-title"><div class="session-notes-heading"><h3 id="session-notes-title">本场笔记</h3><span>{{ selectedDebugSession.markers.length }} 条 · 点击定位</span></div><div class="session-note-list"><button v-for="marker in selectedDebugSession.markers" :key="marker.id" class="session-note-row" type="button" :disabled="!canPlayDebugSession(selectedDebugSession) || restoringSessionId === selectedSessionId" :aria-label="`${markerTypeLabel(marker.type)}，${formatDuration(marker.elapsedMs)}，${marker.note || '未填写文字'}，点击定位`" @click.stop="seekToMarker(marker)"><span class="session-note-type">{{ markerTypeLabel(marker.type) }}</span><span class="session-note-time">{{ formatDuration(marker.elapsedMs) }}</span><span class="session-note-copy">{{ marker.note || '未填写文字' }}</span><span class="session-note-action">{{ restoringSessionId === selectedSessionId ? '加载中…' : '定位' }}</span></button></div></section>
         <div class="session-detail-management-actions"><button class="secondary-button session-edit-button" type="button" @click.stop="beginSessionTitleEdit(selectedDebugSession)">改标题</button><button class="danger-text-button session-delete-button" type="button" :disabled="isSessionDeletionBlocked(selectedDebugSession)" :aria-label="`删除 ${displaySessionTitle(selectedDebugSession.session.title)}`" @click.stop="deleteSession(selectedDebugSession)">{{ deletingSessionId === selectedDebugSession.session.id ? '删除中…' : pendingSessionDeleteId === selectedSessionId ? '确认删除' : '删除' }}</button></div>
      </section></Transition></article></template></div></details></div><p v-else class="empty-state session-empty">{{ sessionQuery ? '没有找到匹配的会话。' : '还没有录音，完成第一场直播后会显示在这里。' }}</p>
      <div v-if="editingSessionId && editingSession" class="session-edit-modal" role="dialog" aria-modal="true" aria-labelledby="session-edit-dialog-title">
        <button class="session-edit-backdrop" type="button" aria-label="关闭编辑标题" @click="cancelSessionTitleEdit"></button>
        <form class="session-edit-dialog" @submit.prevent="saveSessionTitle(editingSession)">
          <div class="session-edit-dialog-heading"><h2 id="session-edit-dialog-title">编辑标题</h2><button class="session-edit-dialog-close" type="button" aria-label="关闭编辑标题" @click="cancelSessionTitleEdit">×</button></div>
          <label>标题<input v-model="editingSessionTitle" class="session-edit-input" maxlength="80" autofocus /></label>
          <div class="session-edit-dialog-actions"><button class="primary-button compact-button" type="submit">保存</button><button class="session-edit-dialog-cancel" type="button" @click="cancelSessionTitleEdit">取消</button></div>
        </form>
      </div>
    </section>

    <section v-else-if="activeTab === 'settings' && !deviceIdentity" class="app-view settings-view identity-settings-view" aria-label="手机身份设置">
      <section class="identity-pairing-panel" aria-labelledby="identity-pairing-title"><div class="identity-pairing-heading"><h2 id="identity-pairing-title">手机身份</h2><span>未绑定</span></div><div class="identity-pairing-body"><div class="pairing-form"><div class="pairing-fields"><label>6位配对码<input v-model="pairingCode" inputmode="numeric" maxlength="6" autocomplete="one-time-code" placeholder="输入6位配对码" /></label><label>浏览器名称<input v-model="pairingLabel" maxlength="120" autocomplete="off" placeholder="例如：我的手机" /></label></div><button class="primary-button identity-pair-button" type="button" :disabled="pairingBusy || pairingCode.length < 6" @click="pairCurrentDevice">{{ pairingBusy ? '绑定中…' : '绑定手机' }}</button><p class="settings-help">未完成配对前不能录音或上传</p><p v-if="pairingMessage" class="settings-help">{{ pairingMessage }}</p></div></div></section>
    </section>
    <section v-else class="app-view settings-view" aria-label="设置页面">
      <div v-if="errorMessage" class="error-message" role="alert">{{ errorMessage }}</div>
      <details class="settings-group"><summary>手机身份 <span>{{ deviceIdentity?.displayName || '未绑定' }}</span></summary><div class="settings-group-body"><div v-if="deviceIdentity"><div class="settings-status-list"><div><span>当前用户</span><strong>{{ deviceIdentity.displayName }}</strong></div><div><span>设备 ID</span><code>{{ deviceIdentity.deviceId }}</code></div></div><div class="settings-action-row device-logout-row"><button class="text-button" type="button" :disabled="deviceLogoutBusy || isRecording || isBusy" @click="logoutCurrentDevice">{{ deviceLogoutBusy ? '正在退出…' : '退出此设备' }}</button><span class="settings-help">退出不会删除本机录音；再次使用需要重新配对。</span></div></div><div v-else class="pairing-form"><p class="settings-help">由电脑控制台生成 6 位配对码，在这里绑定一次。未完成配对前不能录音或上传。</p><div class="settings-action-row"><input v-model="pairingCode" inputmode="numeric" maxlength="6" placeholder="6 位配对码" /><input v-model="pairingLabel" maxlength="120" placeholder="设备名称" /><button class="primary-button compact-button" type="button" :disabled="pairingBusy || pairingCode.length < 6" @click="pairCurrentDevice">{{ pairingBusy ? '绑定中…' : '绑定手机' }}</button></div><p v-if="pairingMessage" class="settings-help">{{ pairingMessage }}</p></div></div></details>
      <details class="settings-group"><summary>自动上传 <span :class="autoUploadEnabled ? 'status-good' : 'status-warn'">{{ autoUploadEnabled ? '已开启' : '已关闭' }}</span></summary><div class="settings-group-body"><div class="settings-action-row"><div><strong>{{ autoUploadEnabled ? '录音保存后自动上传' : '仅保存到手机本地' }}</strong><p>{{ autoUploadEnabled ? '录音保存后会自动上传到电脑端处理。' : '关闭后不会自动上传，已保存的录音不会丢失；重新开启后会继续上传。' }}</p></div><button class="secondary-button" type="button" @click="setAutoUploadEnabled(!autoUploadEnabled)">{{ autoUploadEnabled ? '关闭' : '开启' }}</button></div></div></details>
      <details class="settings-group pwa-settings-group"><summary>安装 LiveNote <span>{{ pwaStatusLabel }}</span></summary><div class="settings-group-body"><p class="settings-help">安装后的 PWA 会从手机主屏幕独立打开。它不会改变录音权限，也不会让网页获得原生后台录音能力。</p><button v-if="pwaSnapshot.status === 'prompt'" class="primary-button" type="button" @click="installPwa">安装到主屏幕</button><ol v-else class="pwa-install-steps"><li>确认当前使用 HTTPS 地址。</li><li>打开 Android Chrome 右上角菜单。</li><li>选择“安装应用”或“添加到主屏幕”。</li><li>从手机主屏幕重新打开 LiveNote；页面会显示“已安装并正在使用”。</li></ol><p v-if="pwaSnapshot.status === 'unavailable'" class="settings-help">当前开发环境未注册生产 Service Worker，不能把开发页当作完整 PWA 验收。正式构建部署后再测试安装。</p></div></details>
      <details class="settings-group"><summary>录音设置 <span>{{ selectedProfileDetails.name }}</span></summary><div class="settings-group-body"><div v-if="!comparisonMode" class="profile-selector" role="radiogroup" aria-label="默认录音配置"><button v-for="profile in AUDIO_PROFILES" :key="profile.id" class="profile-option" :class="[`profile-option-${profile.id}`, { selected: selectedProfile === profile.id }]" type="button" :aria-checked="selectedProfile === profile.id" role="radio" :disabled="isRecording || isBusy" @click="selectedProfile = profile.id"><span class="profile-radio"></span><span><strong>{{ profile.name }}</strong><small>{{ profile.description }}</small></span></button></div><p class="settings-help">MIME 类型由浏览器自动选择；当前默认建议使用 Speech。实际输入设置以浏览器返回值为准。</p></div></details>
      <details class="settings-group"><summary>音质对比 <span>{{ comparisonMode ? comparisonStepLabel : '未进行' }}</span></summary><div class="settings-group-body"><div v-if="!comparisonMode" class="settings-action-row"><div><p>对同一段声音进行 Browser Default、Speech、Raw-ish 对比。</p></div><button class="secondary-button" type="button" :disabled="!canStart" @click="beginComparison">开始对比</button></div><div v-else class="comparison-guide"><div class="comparison-guide-top"><strong>{{ comparisonStepLabel }} · {{ selectedProfileDetails.name }}</strong><button class="text-button" type="button" :disabled="isRecording || isBusy" @click="endComparison">结束对比</button></div><div class="comparison-steps"><span v-for="(profileId, index) in COMPARISON_ORDER" :key="profileId" class="comparison-step" :class="{ current: index === comparisonIndex, done: index < comparisonIndex }">{{ index + 1 }}. {{ AUDIO_PROFILES.find((profile) => profile.id === profileId)?.shortName }}</span></div><p>手机 A 播放同一段内容，播放时开始录音，结束后停止。</p></div></div></details>
      <details class="settings-group"><summary>浏览器能力 <span>{{ capabilities.indexedDB ? '本地存储可用' : '需要检查' }}</span></summary><div class="settings-group-body"><div class="capability-list"><div v-for="([name, supported]) in capabilityRows" :key="name" class="capability-row"><span>{{ name }}</span><span class="status" :class="supported ? 'is-ok' : 'is-no'"><i></i>{{ supported ? '可用' : '不可用' }}</span></div></div><div class="storage-estimate"><span>本地空间</span><strong :class="storageEstimateClass">{{ storageEstimateLabel }}</strong></div><div class="storage-estimate"><span>持久化存储</span><strong>{{ persistentStorage === true ? '已启用' : persistentStorage === false ? '未获批准' : '浏览器未提供' }}</strong></div><p class="settings-help">录音会先保存到手机浏览器本地空间；接近上限时请先上传、导出或删除旧 Session。持久化存储由浏览器策略决定。</p><button class="text-button capability-refresh" type="button" @click="refreshCapabilities">重新检测</button><div v-if="storageError" class="error-message error-feedback" role="alert"><span class="error-feedback-copy">{{ storageError }}</span><button v-if="diagnosticFeedback.status !== 'success'" class="text-button compact-button error-feedback-button" type="button" :disabled="diagnosticFeedback.status === 'uploading'" @click="uploadAutomaticDiagnostic">{{ diagnosticFeedback.status === 'uploading' ? '反馈中…' : '自动反馈问题' }}</button><span v-if="diagnosticFeedback.message" class="error-feedback-status" :class="'diagnostic-' + diagnosticFeedback.status">{{ diagnosticFeedback.message }}</span></div></div></details>
      <details class="settings-group"><summary>诊断与反馈 <span>{{ diagnosticFeedback.status === 'success' ? '已上传' : diagnosticFeedback.status === 'uploading' ? '上传中' : '可选' }}</span></summary><div class="settings-group-body"><p class="settings-help">上传当前设备、录音、网络和错误状态，帮助定位问题；不会上传录音内容。也可以附加一张截图，帮助说明页面现象。</p><div class="diagnostic-image-row"><label class="secondary-button diagnostic-image-picker">选择截图<input type="file" accept="image/*" @change="handleDiagnosticImageSelected" /></label><span v-if="diagnosticImage" class="diagnostic-image-name">{{ diagnosticImage.name }}</span></div><div class="diagnostic-settings-actions"><button class="secondary-button" type="button" :disabled="diagnosticFeedback.status === 'uploading'" @click="uploadManualDiagnostic">{{ diagnosticFeedback.status === 'uploading' ? '正在上传…' : diagnosticImage ? '上传诊断数据和截图' : '上传诊断数据' }}</button><span v-if="diagnosticFeedback.message" class="error-feedback-status" :class="'diagnostic-' + diagnosticFeedback.status">{{ diagnosticFeedback.message }}</span></div></div></details>
      <details class="settings-group"><summary>设备与连接状态 <span>{{ uploadSnapshot.serverCompatible === false ? 'API 需重启' : uploadSnapshot.serverOnline === true ? '服务器在线' : uploadSnapshot.serverOnline === false ? '服务器离线' : '未检测' }}</span></summary><div class="settings-group-body"><div class="settings-status-list"><div><span>本地保存</span><strong class="status-good">正常</strong></div><div><span>服务器</span><strong :class="uploadSnapshot.serverOnline !== true || uploadSnapshot.serverCompatible === false ? 'status-warn' : 'status-good'">{{ uploadSnapshot.serverCompatible === false ? '需重启' : uploadSnapshot.serverOnline === true ? '在线' : uploadSnapshot.serverOnline === false ? '离线' : '未检测' }}</strong></div><div><span>屏幕常亮</span><strong :class="wakeLockState === 'ACTIVE' ? 'status-good' : 'status-warn'">{{ wakeLockState === 'ACTIVE' ? '正常' : wakeLockState === 'UNSUPPORTED' ? '不支持' : wakeLockState === 'FAILED' ? '失败但继续录音' : '待申请' }}</strong></div><div><span>设备网络</span><strong>{{ networkOnline ? '在线' : '离线（录音继续）' }}</strong></div><div><span>服务器 API</span><strong>{{ uploadSnapshot.serverCompatible === false ? '在线但版本过旧' : uploadSnapshot.serverOnline === true ? '在线' : uploadSnapshot.serverOnline === false ? '离线 / 请求失败' : '未检测' }}</strong></div><div><span>上传队列</span><strong>{{ uploadSnapshot.uploaded }} / {{ uploadSnapshot.total }}</strong></div><div><span>整场音频重组</span><strong>{{ uploadSnapshot.serverCapabilities?.ffmpeg && uploadSnapshot.serverCapabilities?.ffprobe ? '可用' : uploadSnapshot.serverOnline === true ? '未就绪' : '未检测' }}</strong></div><div><span>内容整理</span><strong>电脑端任务处理</strong></div></div><p v-if="uploadSnapshot.lastError" class="upload-error">{{ uploadSnapshot.lastError }}</p></div></details>
      <details class="settings-group danger-group"><summary>本地数据 <span>{{ debugSessions.length }} 场录音</span></summary><div class="settings-group-body"><p class="settings-help">这里管理手机本地录音。单个删除会在服务器在线时同步删除服务器副本；“清理本地全部录音”只删除本机保存的所有录音、分段和标记，不会删除服务器副本。</p><div class="settings-action-row"><button class="text-button" type="button" :disabled="isRecording" @click="resetTest">重置当前页面状态</button><button v-if="!pendingClearLocalData" class="danger-text-button" type="button" :disabled="isRecording || isBusy" @click="clearLocalTestData">清理本地全部录音</button><div v-else class="inline-danger-confirm"><span>确认清理全部本地录音？</span><button class="danger-text-button" type="button" @click="confirmClearLocalData">确认清理</button><button class="text-button" type="button" @click="cancelClearLocalData">取消</button></div></div></div></details>
    </section>

    <nav v-if="deviceIdentity" class="bottom-nav" aria-label="主导航"><button type="button" :class="{ active: activeTab === 'recording' }" @click="activeTab = 'recording'"><span class="nav-icon">●</span><span>录音</span></button><button type="button" :class="{ active: activeTab === 'sessions' }" @click="openSessions()"><span class="nav-icon">▤</span><span>历史</span></button><button type="button" :class="{ active: activeTab === 'settings' }" @click="openSettings"><span class="nav-icon">⋯</span><span>设置</span></button></nav>
    </template>
  </main>
</template>

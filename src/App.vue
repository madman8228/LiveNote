<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { AUDIO_PROFILES, type AudioProfileId } from './audio/AudioProfile'
import { detectAudioCapabilities, type AudioCapabilities } from './audio/AudioCapabilities'
import { AudioLevelMonitor } from './audio/AudioLevelMonitor'
import { MediaRecorderEngine } from './audio/MediaRecorderEngine'
import type { RecorderChunk, RecorderState } from './audio/RecorderEngine'
import { PageLifecycleManager, type LifecycleEvent } from './lifecycle/PageLifecycleManager'
import { WakeLockManager, type WakeLockState } from './lifecycle/WakeLockManager'
import { clearAllData, openDatabase } from './storage/db'
import { ChunkStore } from './storage/ChunkStore'
import { LifecycleStore } from './storage/LifecycleStore'
import { MarkerStore } from './storage/MarkerStore'
import { createSegmentPlayback, type SegmentPlayback } from './storage/SegmentRecovery'
import { createSessionPlayback, type SessionPlayback } from './storage/SessionRecovery'
import { SegmentStore } from './storage/SegmentStore'
import { SessionStore } from './storage/SessionStore'
import { sha256Blob } from './storage/sha256'
import type { ChunkMetadata, ChunkRecord, LifecycleEventRecord, MarkerRecord, MarkerType, SegmentRecord, SessionRecord } from './storage/types'
import { ApiClient, type ProcessingJob, type ReportResponse } from './upload/ApiClient'
import { uploadQueue, type UploadQueueSnapshot } from './upload/UploadQueue'
import { PwaInstallManager, type PwaInstallSnapshot } from './pwa/PwaInstallManager'

type DebugChunk = Omit<ChunkRecord, 'blob'>
interface DebugSegment { segment: SegmentRecord; chunks: DebugChunk[] }
interface DebugSession { session: SessionRecord; segments: DebugSegment[]; markers: MarkerRecord[] }
interface ServerProcessingState { status: 'idle' | 'queued' | 'reconstructing' | 'asr' | 'reporting' | 'completed' | 'error'; message: string; jobId?: string; report?: ReportResponse }
type AppTab = 'recording' | 'sessions' | 'settings'
type SessionFilter = 'all' | 'RECORDING' | 'COMPLETED' | 'INTERRUPTED' | 'PROCESSING'
type SummaryThemeId = 'paper' | 'ocean' | 'sunset' | 'forest' | 'night'

const SUMMARY_THEMES: Array<{ id: SummaryThemeId; name: string }> = [
  { id: 'paper', name: '纸张' },
  { id: 'ocean', name: '海蓝' },
  { id: 'sunset', name: '暮光' },
  { id: 'forest', name: '森林' },
  { id: 'night', name: '夜色' },
]

const COMPARISON_ORDER: AudioProfileId[] = ['browser-default', 'speech', 'raw-ish']
// Shorter logical fragments reduce the amount of audio that can remain only
// inside MediaRecorder when Android Chrome refreshes or discards the page.
// MediaRecorder itself still runs continuously; this does not stop/start it.
const CHUNK_TIMESLICE_MS = 10_000

const capabilities = ref<AudioCapabilities>(detectAudioCapabilities())
const selectedProfile = ref<AudioProfileId>('speech')
const recorderState = ref<RecorderState>('idle')
const currentMimeType = ref('—')
const trackSettings = ref<MediaTrackSettings | null>(null)
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
const restoringSessionId = ref<string | null>(null)
const comparisonMode = ref(false)
const comparisonIndex = ref(0)
const lifecycleEvents = ref<LifecycleEventRecord[]>([])
const pageVisibility = ref<DocumentVisibilityState>(document.visibilityState)
const uploadSnapshot = ref<UploadQueueSnapshot>({ serverOnline: null, serverCompatible: null, serverCapabilities: null, isUploading: false, total: 0, uploaded: 0, pending: 0, failed: 0, lastError: '' })
const sessionUpload = ref({ total: 0, uploaded: 0, pending: 0, failed: 0 })
const sessionMarkers = ref<MarkerRecord[]>([])
const lastMarkerMessage = ref('')
const serverProcessing = ref<Record<string, ServerProcessingState>>({})
const summaryTheme = ref<SummaryThemeId>('paper')
const summaryCustomBackgroundUrl = ref<string | null>(null)
const activeTab = ref<AppTab>('recording')
const sessionQuery = ref('')
const sessionFilter = ref<SessionFilter>('all')
const selectedSessionId = ref<string | null>(null)
const editingSessionId = ref<string | null>(null)
const editingSessionTitle = ref('')
const deletingSessionId = ref<string | null>(null)
const networkOnline = ref(typeof navigator === 'undefined' ? true : navigator.onLine)
const storageEstimate = ref<{ usage: number | null; quota: number | null }>({ usage: null, quota: null })
const persistentStorage = ref<boolean | null>(null)
const diagnosticFile = ref<File | null>(null)
const diagnosticDescription = ref('')
const diagnosticState = ref<{ status: 'idle' | 'uploading' | 'success' | 'error'; message: string; id?: string }>({ status: 'idle', message: '' })
const pwaSnapshot = ref<PwaInstallSnapshot>({ status: 'unavailable', isStandalone: false, isSecureContext: false, isProductionBuild: import.meta.env.PROD })
let sessionUploadRefreshToken = 0

const engine = new MediaRecorderEngine()
const levelMonitor = new AudioLevelMonitor((nextLevel) => { level.value = nextLevel })
const wakeLockManager = new WakeLockManager((state, message) => {
  wakeLockState.value = state
  wakeLockMessage.value = message ?? ''
})
const pageLifecycleManager = new PageLifecycleManager((event) => { pageVisibility.value = event.visibilityState; void persistLifecycleEvent(event); if (['pagehide', 'freeze'].includes(event.eventType)) void persistSessionCheckpoint(true) })
const unsubscribeUploadQueue = uploadQueue.subscribe((snapshot) => { uploadSnapshot.value = snapshot; void refreshSessionUpload() })
const pwaInstallManager = new PwaInstallManager()
const unsubscribePwaInstall = pwaInstallManager.subscribe((snapshot) => { pwaSnapshot.value = snapshot })
let durationTimer: number | null = null
let activeMonotonicStartedAt = 0
let activeElapsedBaseMs = 0
let activeSegmentBaseMs = 0
let sessionCheckpointTimer: number | null = null
let storageEstimateTimer: number | null = null
let sessionCheckpointChain: Promise<void> = Promise.resolve()
let recorderFailurePromise: Promise<void> | null = null
const processingPolls = new Map<string, Promise<void>>()
const recoveryInProgress = ref(false)

const selectedProfileDetails = computed(() => AUDIO_PROFILES.find((profile) => profile.id === selectedProfile.value) ?? AUDIO_PROFILES[0])
const isRecording = computed(() => recorderState.value === 'recording')
const comparisonComplete = computed(() => comparisonMode.value && comparisonIndex.value >= COMPARISON_ORDER.length)
const canStart = computed(() => storageReady.value && !isBusy.value && !isRecording.value && !comparisonComplete.value && !recoverySession.value && !recoveryInProgress.value)
const durationLabel = computed(() => formatDuration(elapsedMs.value))
const sessionIdLabel = computed(() => currentSession.value?.id ?? '—')
const segmentIdLabel = computed(() => currentSegment.value?.id ?? '—')
const segmentIndexLabel = computed(() => currentSegment.value?.index ?? '—')
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

const settingsRows = computed(() => {
  const settings = trackSettings.value
  if (!settings) return []
  const keys: Array<keyof MediaTrackSettings> = ['sampleRate', 'sampleSize', 'channelCount', 'noiseSuppression', 'autoGainControl', 'echoCancellation', 'deviceId', 'groupId']
  return keys.filter((key) => settings[key] !== undefined).map((key) => ({ key, value: String(settings[key]) }))
})

const selectedDebugSession = computed(() => debugSessions.value.find((item) => item.session.id === selectedSessionId.value) ?? null)
const selectedProcessing = computed(() => selectedSessionId.value ? serverProcessing.value[selectedSessionId.value] : undefined)
const filteredDebugSessions = computed(() => {
  const query = sessionQuery.value.trim().toLowerCase()
  return [...debugSessions.value]
    .filter((item) => {
      if (!query) return true
      const session = item.session
      return session.title.toLowerCase().includes(query) || session.id.toLowerCase().includes(query) || formatDate(session.startedAt).toLowerCase().includes(query)
    })
    .filter((item) => {
      if (sessionFilter.value === 'all') return true
      if (sessionFilter.value === 'PROCESSING') return Boolean(serverProcessing.value[item.session.id]?.status && !['completed', 'error'].includes(serverProcessing.value[item.session.id].status))
      return item.session.status === sessionFilter.value
    })
    .sort((a, b) => b.session.startedAt - a.session.startedAt)
})
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

function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

function formatDate(timestamp: number | null): string { return timestamp ? new Date(timestamp).toLocaleString() : '—' }

function downloadAudio(url: string | undefined, filename: string): void {
  if (!url) return
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  downloadAudio(url, filename)
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000)
}

async function downloadSegmentRecording(sessionId: string, segmentId: string, filename: string): Promise<void> {
  const playback = restoredMeta.value[segmentId]
  if (!playback) return
  if (playback.blob.size > 0) {
    downloadBlob(playback.blob, filename)
    return
  }
  try {
    downloadBlob(await ApiClient.downloadSegmentAudio(sessionId, segmentId), filename)
  } catch (error) {
    errorMessage.value = error instanceof Error
      ? `${error.message}；本地顺序追加播放器不能直接导出，请先确认服务器已在线并完成上传。`
      : '当前本地顺序追加播放器不能直接导出，请先确认服务器已在线并完成上传。'
  }
}

async function downloadSessionRecording(sessionId: string, filename: string): Promise<void> {
  const playback = restoredSessionMeta.value[sessionId]
  if (!playback) return
  if (playback.blob.size > 0) {
    downloadBlob(playback.blob, filename)
    return
  }
  try {
    downloadBlob(await ApiClient.downloadSessionAudio(sessionId), filename)
  } catch (error) {
    errorMessage.value = error instanceof Error
      ? `${error.message}；本地顺序追加播放器不能直接导出，请先确认服务器已在线并完成上传。`
      : '当前本地顺序追加播放器不能直接导出，请先确认服务器已在线并完成上传。'
  }
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
  return title.replace(/^LiveNote(?=\s|$)/i, 'LN')
}

function sessionStatusLabel(status: SessionRecord['status']): string {
  return { RECORDING: '录音中', PAUSED: '已暂停', FINALIZING: '收尾中', COMPLETED: '已完成', INTERRUPTED: '需恢复' }[status]
}

function processingStatusLabel(sessionId: string): string {
  const status = serverProcessing.value[sessionId]?.status
  if (!status) return '未处理'
  return { idle: '未处理', queued: '排队中', reconstructing: '重建音频', asr: 'ASR 转写', reporting: '生成报告', completed: '已完成', error: '处理失败' }[status]
}

function openSessions(sessionId?: string): void {
  activeTab.value = 'sessions'
  if (!sessionId) {
    selectedSessionId.value = null
    void refreshDebugData()
    return
  }
  if (selectedSessionId.value === sessionId) {
    selectedSessionId.value = null
    return
  }
  selectedSessionId.value = sessionId
  if (!debugSessions.value.some((item) => item.session.id === sessionId)) void refreshDebugData()
}

function openSettings(): void {
  activeTab.value = 'settings'
  selectedSessionId.value = null
}

async function installPwa(): Promise<void> {
  const result = await pwaInstallManager.promptInstall()
  if (result === 'accepted') activeTab.value = 'recording'
}

function beginSessionTitleEdit(item: DebugSession): void {
  editingSessionId.value = item.session.id
  editingSessionTitle.value = item.session.title
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
  return isRecording.value || isBusy.value || uploadSnapshot.value.isUploading || deletingSessionId.value === item.session.id || ['RECORDING', 'PAUSED', 'FINALIZING'].includes(item.session.status) || ['queued', 'reconstructing', 'asr', 'reporting'].includes(serverProcessing.value[item.session.id]?.status ?? '')
}

function isApiNotFound(error: unknown): boolean {
  return error instanceof Error && /服务器请求失败 \(404\)/.test(error.message)
}

async function deleteSession(item: DebugSession): Promise<void> {
  if (isSessionDeletionBlocked(item)) return
  const title = displaySessionTitle(item.session.title)
  const hasUploadedChunks = item.segments.some((segment) => segment.chunks.some((chunk) => chunk.uploadStatus === 'UPLOADED'))
  const serverOnline = uploadSnapshot.value.serverOnline === true
  if (!serverOnline && hasUploadedChunks) {
    errorMessage.value = '服务器当前离线，无法安全删除服务器副本；请恢复连接后再删除该 Session。'
    return
  }
  const scope = serverOnline ? '手机和服务器' : '手机本地'
  const suffix = serverOnline ? '服务器上的音频、处理结果和任务记录也会删除。' : '当前没有已确认的服务器副本。'
  const confirmed = window.confirm(`确定删除${scope}的 Session“${title}”？\n将同时删除其中的 Segment、Chunk、标记和本地音频，无法恢复。${suffix}`)
  if (!confirmed) return

  deletingSessionId.value = item.session.id
  uploadQueue.stop()
  try {
    if (serverOnline) {
      try {
        await ApiClient.deleteSession(item.session.id)
      } catch (error) {
        if (!isApiNotFound(error)) throw new Error(`服务器副本删除失败，已保留本机 Session：${error instanceof Error ? error.message : '请求失败'}`)
      }
    }
    const segments = await SegmentStore.listBySessionId(item.session.id)
    const chunks = await ChunkStore.listMetadataBySessionId(item.session.id)
    for (const chunk of chunks) await ChunkStore.delete(chunk.id)
    for (const segment of segments) {
      const segmentUrl = restoredUrls.value[segment.id]
      if (segmentUrl) URL.revokeObjectURL(segmentUrl)
      await SegmentStore.delete(segment.id)
    }

    const markers = await MarkerStore.listBySessionId(item.session.id)
    for (const marker of markers) await MarkerStore.delete(marker.id)
    const events = await LifecycleStore.listBySessionId(item.session.id)
    for (const event of events) await LifecycleStore.delete(event.id)
    await SessionStore.delete(item.session.id)

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
    await refreshDebugData()
    await uploadQueue.refreshSnapshot()
  } catch (error) {
    errorMessage.value = error instanceof Error ? `删除 Session 失败：${error.message}` : '删除 Session 失败。'
  } finally {
    deletingSessionId.value = null
    uploadQueue.start()
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
    sessionUpload.value = { total: 0, uploaded: 0, pending: 0, failed: 0 }
    return
  }
  try {
    const snapshot = await uploadQueue.snapshotForSession(sessionId)
    if (refreshToken === sessionUploadRefreshToken && currentSession.value?.id === sessionId) sessionUpload.value = snapshot
  } catch {
    if (refreshToken === sessionUploadRefreshToken && currentSession.value?.id === sessionId) sessionUpload.value = { total: 0, uploaded: 0, pending: 0, failed: 0 }
  }
}

function updateRestoredSessionDuration(event: Event, sessionId: string): void {
  const audio = event.currentTarget as HTMLAudioElement
  const durationMs = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration * 1000 : null
  const current = restoredSessionMeta.value[sessionId]
  if (!durationMs || !current || Math.abs(current.durationMs - durationMs) < 1) return
  restoredSessionMeta.value = { ...restoredSessionMeta.value, [sessionId]: { ...current, durationMs } }
}

function updateRestoredSegmentDuration(event: Event, segmentId: string): void {
  const audio = event.currentTarget as HTMLAudioElement
  const durationMs = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration * 1000 : null
  const current = restoredMeta.value[segmentId]
  if (!durationMs || !current || Math.abs(current.durationMs - durationMs) < 1) return
  restoredMeta.value = { ...restoredMeta.value, [segmentId]: { ...current, durationMs } }
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
  const note = window.prompt(`${type} 备注（可留空）`, '') ?? ''
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
  if (!storageReady.value || isBusy.value || isRecording.value || comparisonComplete.value || recoverySession.value) return
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
    trackSettings.value = settings ?? null
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
  trackSettings.value = null
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

async function clearLocalTestData(): Promise<void> {
  if (isRecording.value || isBusy.value) return
  const confirmed = window.confirm('确定清理手机本地全部测试录音、标记和恢复记录吗？此操作不可撤销。')
  if (!confirmed) return
  try {
    uploadQueue.stop()
    Object.values(restoredUrls.value).forEach((url) => URL.revokeObjectURL(url))
    Object.values(restoredSessionUrls.value).forEach((url) => URL.revokeObjectURL(url))
    if (summaryCustomBackgroundUrl.value) URL.revokeObjectURL(summaryCustomBackgroundUrl.value)
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
    summaryCustomBackgroundUrl.value = null
    await uploadQueue.refreshSnapshot()
    resetCurrentView()
    lastMarkerMessage.value = '手机本地测试数据已清理。'
    uploadQueue.start()
  } catch (error) {
    errorMessage.value = error instanceof Error ? `清理本地数据失败：${error.message}` : '清理本地数据失败。'
    uploadQueue.start()
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
  } catch (error) {
    recoverySession.value = session
    errorMessage.value = error instanceof Error ? `结束恢复 Session 失败：${error.message}` : '结束恢复 Session 失败。'
  }
}

async function refreshDebugData(): Promise<void> {
  if (!storageReady.value) return
  const sessions = await SessionStore.list()
  const nested: DebugSession[] = []
  for (const session of sessions) {
    const segments = await SegmentStore.listBySessionId(session.id)
    nested.push({
      session,
      segments: await Promise.all(segments.map(async (segment) => ({
        segment,
        // The debug tree only needs metadata. Keep audio Blob values out of
        // Vue state; playback reads the same Chunk records on demand.
        chunks: await ChunkStore.listMetadataBySegmentId(segment.id),
      }))),
      markers: await MarkerStore.listBySessionId(session.id),
    })
  }
  debugSessions.value = nested
  const savedReports = await Promise.all(sessions.map(async (session) => {
    const [reportResult, jobResult] = await Promise.allSettled([
      ApiClient.getReport(session.id),
      ApiClient.getLatestProcessingJob(session.id),
    ])
    const latestJob = jobResult.status === 'fulfilled' ? jobResult.value : null
    const jobIsActive = latestJob && !['COMPLETED', 'FAILED'].includes(latestJob.status) && latestJob.stage !== 'DONE' && latestJob.stage !== 'FAILED'
    if (jobIsActive && latestJob) {
      return [session.id, { status: processingUiStatus(latestJob), message: latestJob.message, jobId: latestJob.id }] as const
    }
    if (reportResult.status === 'fulfilled') {
      const report = reportResult.value
      return [session.id, { status: 'completed' as const, message: `已有处理结果：${report.counts.transcriptSegments ?? 0} 个 ASR 时间片。`, report }] as const
    }
    if (latestJob) {
      return [session.id, { status: processingUiStatus(latestJob), message: latestJob.message, jobId: latestJob.id }] as const
    }
    return null
  }))
  const restoredProcessing: Record<string, ServerProcessingState> = {}
  const jobsToResume: Array<{ sessionId: string; job: ProcessingJob }> = []
  for (const entry of savedReports) {
    if (!entry) continue
    restoredProcessing[entry[0]] = entry[1]
    if ('jobId' in entry[1] && entry[1].jobId && ['queued', 'reconstructing', 'asr', 'reporting'].includes(entry[1].status)) {
      const job = await ApiClient.getProcessingJob(entry[1].jobId).catch(() => null)
      if (job) jobsToResume.push({ sessionId: entry[0], job })
    }
  }
  if (Object.keys(restoredProcessing).length) serverProcessing.value = { ...serverProcessing.value, ...restoredProcessing }
  for (const item of jobsToResume) void monitorProcessingJob(item.sessionId, item.job)
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
  const previousUrl = restoredSessionUrls.value[sessionId]
  if (previousUrl) URL.revokeObjectURL(previousUrl)
  restoringSessionId.value = sessionId
  try {
    const debugSession = debugSessions.value.find((item) => item.session.id === sessionId)
    const chunks = debugSession?.segments.flatMap((segment) => segment.chunks) ?? []
    const localUploadComplete = isSessionUploadComplete(debugSession)
    // A server reconstruction is only authoritative after every local Chunk
    // is confirmed uploaded. Otherwise the server may legitimately contain
    // only the first part of a Session and return a shorter audio file.
    if (debugSession && localUploadComplete) {
      try {
        const blob = await ApiClient.downloadSessionAudio(sessionId)
        const url = URL.createObjectURL(blob)
        restoredSessionUrls.value = { ...restoredSessionUrls.value, [sessionId]: url }
        restoredSessionMeta.value = { ...restoredSessionMeta.value, [sessionId]: { blob, url, playbackMode: 'server-ffmpeg', segmentCount: debugSession.segments.length, chunkCount: chunks.length, totalBytes: blob.size, durationMs: debugSession.session.durationMs, mimeType: blob.type || 'audio/webm', hasGaps: debugSession.segments.some((segment) => segment.chunks.some((chunk, index) => chunk.index !== index)) } }
        return
      } catch {
        // Server reconstruction is preferred for multi-Segment sessions, but
        // a temporary API failure must not remove the local playback path.
        // MediaSource may still reject incompatible fragments; that failure
        // is handled by the existing user-facing error below.
      }
    }

    const restored = await createSessionPlayback(sessionId, (url) => {
      restoredSessionUrls.value = { ...restoredSessionUrls.value, [sessionId]: url }
    })
    restoredSessionMeta.value = { ...restoredSessionMeta.value, [sessionId]: restored }
  } catch (error) { errorMessage.value = error instanceof Error ? error.message : '无法重组整场 Session。' }
  finally { restoringSessionId.value = null }
}

function refreshCapabilities(): void { capabilities.value = detectAudioCapabilities(); void loadStorage() }

function debugSessionChunkCount(item: DebugSession): number {
  return item.segments.reduce((total, segment) => total + segment.chunks.length, 0)
}

function isSessionUploadComplete(item: DebugSession | null | undefined): boolean {
  return Boolean(item?.segments.length)
    && item!.segments.every((segment) => segment.chunks.length > 0 && segment.chunks.every((chunk) => chunk.uploadStatus === 'UPLOADED'))
}

function formatDb(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${value.toFixed(1)} dB`
}

async function processSessionOnServer(sessionId: string): Promise<void> {
  if (isRecording.value || isBusy.value) return
  const current = serverProcessing.value[sessionId]
  if (current && ['queued', 'reconstructing', 'asr', 'reporting'].includes(current.status)) return
  const capabilities = uploadSnapshot.value.serverCapabilities
  if (uploadSnapshot.value.serverCompatible === false) {
    serverProcessing.value = { ...serverProcessing.value, [sessionId]: { status: 'error', message: '服务器 API 版本过旧，请重启 8000 服务后再处理。' } }
    return
  }
  if (capabilities && (!capabilities.ffmpeg || !capabilities.ffprobe)) {
    serverProcessing.value = { ...serverProcessing.value, [sessionId]: { status: 'error', message: '服务器缺少 FFmpeg/FFprobe，无法重建音频。请先安装并重启 API 服务。' } }
    return
  }
  const model = capabilities?.asrModel || 'medium'
  if (capabilities && !capabilities.asrModelCached) {
    serverProcessing.value = { ...serverProcessing.value, [sessionId]: { status: 'error', message: `服务器未缓存 Whisper 模型 ${model}，请先准备模型文件后再处理。` } }
    return
  }
  serverProcessing.value = { ...serverProcessing.value, [sessionId]: { status: 'queued', message: `正在创建本地处理任务（${model}）…` } }
  try {
    // Follow the server's configured default model. Keeping this in the
    // client hard-coded would make a production `LIVENOTE_WHISPER_MODEL`
    // setting ineffective and could queue a model that is not cached.
    let debugSession = debugSessions.value.find((item) => item.session.id === sessionId) ?? null
    if (!isSessionUploadComplete(debugSession)) {
      serverProcessing.value = { ...serverProcessing.value, [sessionId]: { status: 'queued', message: '正在等待本场音频全部上传…' } }
      await uploadQueue.flush()
      await refreshDebugData()
      debugSession = debugSessions.value.find((item) => item.session.id === sessionId) ?? null
      if (!isSessionUploadComplete(debugSession)) {
        throw new Error('本场音频尚未全部上传，暂不能开始 ASR。请确认服务器在线后稍后重试。')
      }
    }
    const job = await ApiClient.startProcessing(sessionId, model, 'zh')
    await monitorProcessingJob(sessionId, job)
  } catch (error) {
    serverProcessing.value = { ...serverProcessing.value, [sessionId]: { status: 'error', message: error instanceof Error ? error.message : '本地处理失败。' } }
  }
}

async function monitorProcessingJob(sessionId: string, initialJob: ProcessingJob): Promise<void> {
  const existing = processingPolls.get(sessionId)
  if (existing) return existing

  const promise = (async () => {
    let state = initialJob
    serverProcessing.value = { ...serverProcessing.value, [sessionId]: { status: processingUiStatus(state), message: state.message, jobId: state.id } }
    try {
      while (state.status !== 'COMPLETED' && state.status !== 'FAILED') {
        await waitForProcessingPoll()
        state = await ApiClient.getProcessingJob(initialJob.id)
        serverProcessing.value = { ...serverProcessing.value, [sessionId]: { status: processingUiStatus(state), message: state.message, jobId: state.id } }
      }
      if (state.status === 'FAILED') {
        serverProcessing.value = { ...serverProcessing.value, [sessionId]: { status: 'error', message: state.message || state.error || '本地处理失败。', jobId: state.id } }
        return
      }
      const report = await ApiClient.getReport(sessionId)
      const completionMessage = report.summaryStatus === 'GENERATED'
        ? `处理完成：${state.transcriptSegments ?? report.counts.transcriptSegments ?? 0} 个 ASR 时间片，语义总结已生成。`
        : `处理完成：${state.transcriptSegments ?? report.counts.transcriptSegments ?? 0} 个 ASR 时间片，当前为本地草稿。`
      serverProcessing.value = { ...serverProcessing.value, [sessionId]: { status: 'completed', message: completionMessage, jobId: state.id, report } }
    } catch (error) {
      serverProcessing.value = { ...serverProcessing.value, [sessionId]: { status: 'error', message: error instanceof Error ? error.message : '读取处理任务失败。', jobId: state.id } }
    }
  })().finally(() => { processingPolls.delete(sessionId) })

  processingPolls.set(sessionId, promise)
  return promise
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

function summaryQuestions(report: ReportResponse | undefined): string[] {
  if (report?.summary?.questions?.length) return report.summary.questions.map((item) => item.answer ? `问：${item.question}  答：${item.answer}` : `问：${item.question}`)
  return report?.localDraft?.questions.map((item) => item.note || '').filter(Boolean) ?? []
}

function handleSummaryBackgroundFile(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file || !file.type.startsWith('image/')) return
  if (summaryCustomBackgroundUrl.value) URL.revokeObjectURL(summaryCustomBackgroundUrl.value)
  summaryCustomBackgroundUrl.value = URL.createObjectURL(file)
  input.value = ''
}

function clearSummaryBackground(): void {
  if (summaryCustomBackgroundUrl.value) URL.revokeObjectURL(summaryCustomBackgroundUrl.value)
  summaryCustomBackgroundUrl.value = null
}

function processingUiStatus(job: ProcessingJob): ServerProcessingState['status'] {
  if (job.status === 'FAILED' || job.stage === 'FAILED') return 'error'
  if (job.status === 'COMPLETED' || job.stage === 'DONE') return 'completed'
  if (job.stage === 'RECONSTRUCTING') return 'reconstructing'
  if (job.stage === 'ASR') return 'asr'
  if (job.stage === 'REPORT') return 'reporting'
  return 'queued'
}

function waitForProcessingPoll(): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, 1500))
}

function buildDiagnosticSnapshot(): Record<string, unknown> {
  return {
    schemaVersion: 1,
    createdAt: new Date().toISOString(),
    url: window.location.href,
    userAgent: navigator.userAgent,
    capabilities: capabilities.value,
    recorder: { state: recorderState.value, profile: selectedProfile.value, mimeType: currentMimeType.value, trackSettings: trackSettings.value },
    current: { sessionId: currentSession.value?.id ?? null, segmentId: currentSegment.value?.id ?? null, segmentIndex: currentSegment.value?.index ?? null, elapsedMs: elapsedMs.value, savedChunkCount: savedChunkCount.value, currentSegmentChunkCount: currentSegmentChunkCount.value, currentChunkIndex: currentChunkIndex.value, savedBytes: savedBytes.value },
    page: { visibility: pageVisibility.value, networkOnline: networkOnline.value, wakeLock: wakeLockState.value, wakeLockMessage: wakeLockMessage.value },
    upload: { ...uploadSnapshot.value, lastError: uploadSnapshot.value.lastError },
    sessionUpload: sessionUpload.value,
    errorMessage: errorMessage.value,
    serverProcessing: Object.fromEntries(Object.entries(serverProcessing.value).map(([id, state]) => [id, { status: state.status, message: state.message, jobId: state.jobId }])),
    lifecycleEvents: lifecycleEvents.value.slice(-30),
    sessions: debugSessions.value.map((item) => ({ id: item.session.id, title: item.session.title, status: item.session.status, durationMs: item.session.durationMs, segments: item.segments.map((segmentItem) => ({ id: segmentItem.segment.id, index: segmentItem.segment.index, status: segmentItem.segment.status, durationMs: segmentItem.segment.durationMs, chunkCount: segmentItem.chunks.length, chunkIndexes: segmentItem.chunks.map((chunk) => chunk.index) })) })),
  }
}

function handleDiagnosticFile(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  if (file && file.size > 10 * 1024 * 1024) {
    diagnosticFile.value = null
    diagnosticState.value = { status: 'error', message: '诊断附件不能超过 10 MB。' }
    input.value = ''
    return
  }
  diagnosticFile.value = file
  diagnosticState.value = { status: 'idle', message: file ? `已选择：${file.name}` : '' }
}

function downloadDiagnosticBundle(): void {
  const blob = new Blob([JSON.stringify(buildDiagnosticSnapshot(), null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `livenote-diagnostic-${Date.now()}.json`
  link.click()
  URL.revokeObjectURL(url)
  diagnosticState.value = { status: 'success', message: '诊断 JSON 已下载。' }
}

async function uploadDiagnosticBundle(): Promise<void> {
  if (diagnosticState.value.status === 'uploading') return
  diagnosticState.value = { status: 'uploading', message: '正在上传诊断资料…' }
  try {
    const formData = new FormData()
    formData.append('description', diagnosticDescription.value)
    formData.append('snapshot', new Blob([JSON.stringify(buildDiagnosticSnapshot(), null, 2)], { type: 'application/json' }), 'snapshot.json')
    if (diagnosticFile.value) formData.append('attachment', diagnosticFile.value, diagnosticFile.value.name)
    const result = await ApiClient.uploadDiagnostic(formData)
    diagnosticState.value = { status: 'success', message: `诊断资料已上传，编号：${result.diagnosticId}`, id: result.diagnosticId }
  } catch (error) {
    diagnosticState.value = { status: 'error', message: error instanceof Error ? error.message : '诊断资料上传失败。' }
  }
}

function handleNetworkOffline(): void {
  networkOnline.value = false
}

function handleNetworkOnline(): void {
  networkOnline.value = true
}

onMounted(() => { pwaInstallManager.start(); window.addEventListener('offline', handleNetworkOffline); window.addEventListener('online', handleNetworkOnline); wakeLockManager.start(); pageLifecycleManager.start(); uploadQueue.start(); void refreshStorageEstimate(); storageEstimateTimer = window.setInterval(() => { void refreshStorageEstimate() }, 30_000); void loadStorage() })
onBeforeUnmount(() => { pwaInstallManager.stop(); unsubscribePwaInstall(); window.removeEventListener('offline', handleNetworkOffline); window.removeEventListener('online', handleNetworkOnline); stopDurationTimer(); stopSessionCheckpointTimer(); if (storageEstimateTimer !== null) window.clearInterval(storageEstimateTimer); pageLifecycleManager.stop(); levelMonitor.stop(); wakeLockManager.stop(); uploadQueue.stop(); unsubscribeUploadQueue(); engine.dispose(); Object.values(restoredUrls.value).forEach((url) => URL.revokeObjectURL(url)); Object.values(restoredSessionUrls.value).forEach((url) => URL.revokeObjectURL(url)); if (summaryCustomBackgroundUrl.value) URL.revokeObjectURL(summaryCustomBackgroundUrl.value) })
</script>

<template>
  <main class="app-shell" :class="{ 'recording-shell': activeTab === 'recording' }">
    <header class="topbar">
      <div class="brand-lockup">
        <div class="brand-mark" aria-hidden="true">L</div>
        <div><h1>LiveNote</h1></div>
      </div>
      <span class="topbar-state" :class="{ recording: isRecording }">{{ isRecording ? '正在录音' : networkOnline ? '准备就绪' : '离线可录音' }}</span>
    </header>

    <section v-if="recoverySession" class="recovery-banner" aria-labelledby="recovery-title">
      <div><h2 id="recovery-title">发现未结束的录音</h2><p>{{ recoverySession.title }} · 已记录 {{ formatDuration(recoverySession.durationMs) }}<span v-if="recoverySessions.length > 1"> · 还有 {{ recoverySessions.length - 1 }} 场待处理</span></p></div>
      <div class="recovery-actions"><button class="primary-button compact-button" type="button" :disabled="isBusy || recoveryInProgress" @click="continueRecovery">{{ recoveryInProgress ? '正在恢复…' : '继续录音' }}</button><button class="stop-button compact-button" type="button" :disabled="isBusy || recoveryInProgress" @click="finishRecovery">结束并保存</button></div>
    </section>

    <section v-if="activeTab === 'recording' && shouldShowPwaGuide" class="pwa-install-banner" aria-labelledby="pwa-install-title">
      <div><h2 id="pwa-install-title">把 LiveNote 放到主屏幕</h2></div>
      <button v-if="pwaSnapshot.status === 'prompt'" class="primary-button compact-button" type="button" @click="installPwa">安装</button><button v-else class="text-button" type="button" @click="openSettings">查看方法</button>
    </section>

    <section v-if="activeTab === 'recording'" class="app-view recording-view" aria-labelledby="recording-title">
      <section class="recording-hero">
        <div class="record-readout"><span class="record-indicator" :class="{ active: isRecording }"></span><strong>{{ durationLabel }}</strong><span class="record-profile-caption">{{ comparisonMode ? comparisonStepLabel : selectedProfileDetails.shortName }}</span><button v-if="currentSession" class="text-button record-session-link" type="button" @click="openSessions(currentSession.id)">查看会话</button></div>
        <div class="level-meter" aria-label="实时麦克风音量"><span v-for="index in 18" :key="index" class="meter-segment" :class="{ lit: level >= index / 18 }"></span></div>
        <div class="control-actions"><button class="primary-button record-primary" type="button" :disabled="!canStart" @click="startRecording"><span class="button-icon">●</span>{{ comparisonMode ? '开始第 ' + (comparisonIndex + 1) + ' 组' : '开始录音' }}</button><button class="stop-button record-primary" type="button" :disabled="!isRecording || isBusy" @click="stopRecording"><span class="button-icon">■</span>{{ comparisonMode && comparisonIndex < COMPARISON_ORDER.length - 1 ? '停止并下一组' : '停止并保存' }}</button></div>
        <p v-if="errorMessage" class="error-message" role="alert">{{ errorMessage }}</p>
      </section>

      <details v-if="!isRecording" class="record-options"><summary>录音配置 · {{ selectedProfileDetails.name }}</summary><div v-if="!comparisonMode" class="profile-selector" role="radiogroup" aria-label="录音配置"><button v-for="profile in AUDIO_PROFILES" :key="profile.id" class="profile-option" :class="{ selected: selectedProfile === profile.id }" type="button" :aria-checked="selectedProfile === profile.id" role="radio" :disabled="isBusy" @click="selectedProfile = profile.id"><span class="profile-radio"></span><span><strong>{{ profile.name }}</strong><small>{{ profile.description }}</small></span></button></div><p v-else class="settings-help">对比模式会按 Browser Default → Speech → Raw-ish 自动推进。</p></details>

      <div class="record-status-grid"><div><span>本地保存</span><strong class="status-good">正常</strong></div><div><span>服务器</span><strong :class="uploadSnapshot.serverOnline !== true || uploadSnapshot.serverCompatible === false ? 'status-warn' : 'status-good'">{{ uploadSnapshot.serverCompatible === false ? '需重启' : uploadSnapshot.serverOnline === true ? '在线' : uploadSnapshot.serverOnline === false ? '离线' : '未检测' }}</strong></div><div><span>屏幕常亮</span><strong :class="wakeLockState === 'ACTIVE' ? 'status-good' : 'status-warn'">{{ wakeLockState === 'ACTIVE' ? '正常' : wakeLockState === 'UNSUPPORTED' ? '不支持' : wakeLockState === 'FAILED' ? '失败但继续录音' : '待申请' }}</strong></div><div><span>本场上传</span><strong>{{ sessionUpload.uploaded }} / {{ sessionUpload.total }}</strong></div></div>

      <div v-if="isRecording" class="marker-actions live-markers"><span class="field-label">快速标记</span><button type="button" @click="addMarker('KEY_POINT')">重点</button><button type="button" @click="addMarker('QUESTION')">疑问</button><button type="button" @click="addMarker('IDEA')">灵感</button><button type="button" @click="addMarker('TODO')">待办</button><small v-if="lastMarkerMessage">{{ lastMarkerMessage }}</small></div>

      <details class="record-details"><summary>录音详情</summary><div class="data-grid compact-data-grid"><div class="data-field"><span class="field-label">Session</span><code>{{ sessionIdLabel }}</code></div><div class="data-field"><span class="field-label">Segment</span><strong>#{{ segmentIndexLabel }}</strong></div><div class="data-field"><span class="field-label">已保存 Chunk</span><strong>{{ savedChunkCount }} · 当前 {{ currentSegmentChunkCount }}</strong></div><div class="data-field"><span class="field-label">本地大小</span><strong>{{ formatBytes(savedBytes) }}</strong></div><div class="data-field"><span class="field-label">当前 MIME</span><code>{{ currentMimeType }}</code></div><div class="data-field"><span class="field-label">Pending</span><strong>{{ sessionUpload.pending }}</strong></div><div class="data-field"><span class="field-label">Failed</span><strong>{{ sessionUpload.failed }}</strong></div><div class="data-field"><span class="field-label">页面</span><strong>{{ pageVisibility === 'visible' ? '前台' : '后台' }}</strong></div></div><p class="local-save-note">{{ !networkOnline ? '设备网络已断开，录音仍继续保存在本机。' : uploadSnapshot.serverOnline === false ? '服务器暂时不可用，录音仍继续保存在本机。' : '录音先保存本机，再异步上传。' }}</p></details>

      <div v-if="sessionMarkers.length" class="inline-marker-list"><div class="subsection-heading"><span>本场标记</span><button class="text-button" type="button" @click="openSessions(currentSession?.id)">查看全部</button></div><span v-for="marker in sessionMarkers.slice(-3).reverse()" :key="marker.id">{{ markerTypeLabel(marker.type) }} · {{ formatDuration(marker.elapsedMs) }}</span></div>
      <div v-if="hasLifecycleRisk" class="lifecycle-warning"><strong>录音期间页面曾离开前台</strong><span>{{ formatDate(lastHiddenEvent?.wallClockMs ?? null) }} · 风险区间 {{ formatDuration(lifecycleGapMs) }}</span></div>
    </section>

    <section v-else-if="activeTab === 'sessions'" class="app-view sessions-view" aria-label="会话列表">
      <div class="view-heading"><div><p>{{ debugSessions.length }} 场本地录音</p></div><button class="text-button" type="button" @click="refreshDebugData">刷新</button></div>
      <div class="session-toolbar"><div class="session-search-row"><label class="search-field"><span class="sr-only">搜索会话</span><input v-model="sessionQuery" type="search" placeholder="搜索标题、日期或 Session ID" /></label><label class="session-filter-field"><span class="sr-only">会话筛选</span><select v-model="sessionFilter" aria-label="会话筛选"><option value="all">全部</option><option value="RECORDING">录音中</option><option value="COMPLETED">已完成</option><option value="INTERRUPTED">需恢复</option><option value="PROCESSING">处理中</option></select></label></div></div>
      <div v-if="filteredDebugSessions.length" class="session-list"><template v-for="item in filteredDebugSessions" :key="item.session.id"><article class="session-item" :class="{ selected: selectedSessionId === item.session.id }"><form v-if="editingSessionId === item.session.id" class="session-edit-form" @submit.prevent="saveSessionTitle(item)"><input v-model="editingSessionTitle" aria-label="会话标题" maxlength="80" /><button class="primary-button compact-button" type="submit">保存</button><button class="text-button" type="button" @click="cancelSessionTitleEdit">取消</button></form><div v-else class="session-row" role="button" tabindex="0" :aria-expanded="selectedSessionId === item.session.id" @click="openSessions(item.session.id)" @keydown.enter="openSessions(item.session.id)"><div class="session-row-main"><strong>{{ displaySessionTitle(item.session.title) }}</strong><span>{{ formatDate(item.session.startedAt) }} · {{ formatDuration(item.session.durationMs) }}</span></div><div class="session-row-meta"><button class="text-button session-edit-button" type="button" @click.stop="beginSessionTitleEdit(item)">编辑</button><button class="danger-text-button session-delete-button" type="button" :disabled="isSessionDeletionBlocked(item)" :aria-label="`删除 ${displaySessionTitle(item.session.title)}`" @click.stop="deleteSession(item)">{{ deletingSessionId === item.session.id ? '删除中…' : '删除' }}</button><span class="session-status" :class="'session-status-' + item.session.status.toLowerCase()">{{ sessionStatusLabel(item.session.status) }}</span><span>{{ item.segments.length }} 段 · {{ debugSessionChunkCount(item) }} 块</span></div></div>

        <section v-if="selectedSessionId === item.session.id && selectedDebugSession" class="session-detail" aria-labelledby="session-detail-title">
        <div class="detail-actions"><button class="secondary-button" type="button" :disabled="restoringSessionId === selectedSessionId || !selectedDebugSession.segments.some((segmentItem) => segmentItem.chunks.length)" @click="restoreSessionForPlayback(selectedSessionId || '')">{{ restoringSessionId === selectedSessionId ? '正在重组…' : '播放整场录音' }}</button><button class="primary-button" type="button" :disabled="isRecording || uploadSnapshot.serverCompatible === false || ['queued', 'reconstructing', 'asr', 'reporting'].includes(selectedProcessing?.status ?? '')" @click="processSessionOnServer(selectedSessionId || '')">{{ uploadSnapshot.serverCompatible === false ? '重启 API 后处理' : selectedProcessing?.status === 'completed' ? '重新处理' : uploadSnapshot.serverCapabilities?.llmConfigured === false ? '本地 ASR + 生成草稿' : '本地 ASR + 生成总结' }}</button></div>
         <div v-if="restoredSessionUrls[selectedSessionId || '']" class="restore-result detail-player"><audio :key="restoredSessionUrls[selectedSessionId || '']" :src="restoredSessionUrls[selectedSessionId || '']" controls preload="auto" @loadedmetadata="updateRestoredSessionDuration($event, selectedSessionId || '')"></audio><span v-if="restoredSessionMeta[selectedSessionId || '']">{{ formatDuration(restoredSessionMeta[selectedSessionId || ''].durationMs) }} · {{ restoredSessionMeta[selectedSessionId || ''].segmentCount }} 段 · {{ restoredSessionMeta[selectedSessionId || ''].chunkCount }} 块 · {{ restoredSessionMeta[selectedSessionId || ''].playbackMode === 'server-ffmpeg' ? '服务器重建' : '本地顺序追加' }}</span><button class="text-button restore-download" type="button" @click="downloadSessionRecording(selectedSessionId || '', `livenote-${selectedSessionId || 'session'}.webm`)">下载整场</button></div>
        <div class="session-status-summary"><span>上传：{{ selectedDebugSession.segments.reduce((total, segment) => total + segment.chunks.filter((chunk) => chunk.uploadStatus === 'UPLOADED').length, 0) }} / {{ debugSessionChunkCount(selectedDebugSession) }}</span><span>内容处理：{{ processingStatusLabel(selectedDebugSession.session.id) }}</span><span>标记：{{ selectedDebugSession.markers.length }}</span></div>
        <div v-if="selectedProcessing?.status === 'queued' || selectedProcessing?.status === 'reconstructing' || selectedProcessing?.status === 'asr' || selectedProcessing?.status === 'reporting'" class="processing-status-line">{{ selectedProcessing.message }}</div><div v-if="selectedProcessing?.status === 'error'" class="processing-error">{{ selectedProcessing.message }}</div>
        <details v-if="selectedProcessing?.report" class="report-panel"><summary>总结与转写结果</summary><div class="report-content"><div class="report-facts"><span>模型：{{ selectedProcessing.report.transcript.model }}</span><span>ASR 时间片：{{ selectedProcessing.report.counts.transcriptSegments ?? 0 }}</span><span v-if="selectedProcessing.report.transcript.chunked">长音频分段：{{ selectedProcessing.report.transcript.chunkDurationSeconds }} 秒</span><span>报告状态：{{ selectedProcessing.report.summaryStatus }}</span><span v-if="selectedProcessing.report.transcript.audioQuality">输入音量：平均 {{ formatDb(selectedProcessing.report.transcript.audioQuality.meanVolumeDb) }} · 峰值 {{ formatDb(selectedProcessing.report.transcript.audioQuality.maxVolumeDb) }}</span></div><div v-if="selectedProcessing.report.summary" class="summary-content"><strong>{{ selectedProcessing.report.summary.title || 'AI 内容总结' }}</strong><p>{{ selectedProcessing.report.summary.overview }}</p><section v-if="summaryKnowledgeStructure(selectedProcessing.report).length"><strong>知识结构</strong><div v-for="(section, index) in summaryKnowledgeStructure(selectedProcessing.report)" :key="'structure-' + index"><b>{{ section.title }}</b><ul><li v-for="(point, pointIndex) in section.points" :key="'structure-point-' + index + '-' + pointIndex">{{ point }}</li></ul></div></section><ul><li v-for="point in selectedProcessing.report.summary.keyPoints" :key="point">{{ point }}</li></ul></div><div v-else-if="selectedProcessing.report.localDraft?.overviewPreview" class="summary-content summary-draft"><strong>{{ selectedProcessing.report.analysisProvider === 'local-extractive-draft' ? '本地抽取式草稿' : '本地草稿预览' }}</strong><p>{{ selectedProcessing.report.localDraft.overviewPreview }}</p><section v-if="summaryKnowledgeStructure(selectedProcessing.report).length"><strong>知识结构（抽取式）</strong><div v-for="(section, index) in summaryKnowledgeStructure(selectedProcessing.report)" :key="'draft-structure-' + index"><b>{{ section.title }}</b><ul><li v-for="(point, pointIndex) in section.points" :key="'draft-structure-point-' + index + '-' + pointIndex">{{ point }}</li></ul></div></section></div><div class="share-card-tools"><span class="field-label">分享阅读页 · 可直接截图</span><button v-for="theme in SUMMARY_THEMES" :key="theme.id" class="theme-chip" :class="{ selected: summaryTheme === theme.id }" type="button" @click="summaryTheme = theme.id">{{ theme.name }}</button><label class="theme-upload" title="选择背景图片" aria-label="选择背景图片"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5v-13Z" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="8.5" cy="9" r="1.5" fill="currentColor"/><path d="m5.5 17 4.25-4.25a1 1 0 0 1 1.42 0l1.65 1.65 1.55-1.55a1 1 0 0 1 1.42 0L19 16.07V18.5H5.5V17Z" fill="currentColor"/></svg><span class="sr-only">选择背景图片</span><input type="file" accept="image/*" @change="handleSummaryBackgroundFile" /></label><button v-if="summaryCustomBackgroundUrl" class="text-button" type="button" @click="clearSummaryBackground">清除背景</button></div><article class="share-card" :class="'share-theme-' + summaryTheme" :style="summaryCustomBackgroundUrl ? { backgroundImage: 'linear-gradient(rgba(7,19,31,.35), rgba(7,19,31,.55)), url(' + summaryCustomBackgroundUrl + ')' } : undefined"><div class="share-card-kicker">LIVENOTE · 直播知识卡</div><h4>{{ summaryTitle(selectedProcessing.report) }}</h4><p class="share-card-overview">{{ summaryOverview(selectedProcessing.report) }}</p><section v-if="summaryKnowledgeStructure(selectedProcessing.report).length"><h5>知识结构</h5><div v-for="(section, index) in summaryKnowledgeStructure(selectedProcessing.report)" :key="'card-structure-' + index"><h6>{{ section.title }}</h6><ul><li v-for="(point, pointIndex) in section.points" :key="'card-structure-point-' + index + '-' + pointIndex">{{ point }}</li></ul></div></section><section v-if="summaryKeyPoints(selectedProcessing.report).length"><h5>关键知识点</h5><ul><li v-for="(point, index) in summaryKeyPoints(selectedProcessing.report)" :key="'point-' + index">{{ point }}</li></ul></section><section v-if="summaryQuestions(selectedProcessing.report).length"><h5>问答</h5><ul><li v-for="(question, index) in summaryQuestions(selectedProcessing.report)" :key="'question-' + index">{{ question }}</li></ul></section><section v-if="summaryActionItems(selectedProcessing.report).length"><h5>待办事项</h5><ul><li v-for="(itemText, index) in summaryActionItems(selectedProcessing.report)" :key="'todo-' + index">{{ itemText }}</li></ul></section><footer>LiveNote · {{ selectedProcessing.report.summaryStatus === 'GENERATED' ? 'AI 总结' : '本地草稿' }}</footer></article><span v-if="selectedProcessing.report.summaryError" class="processing-error">内容总结失败：{{ selectedProcessing.report.summaryError }}</span><details v-if="selectedProcessing.report.transcript.segments?.length" class="processing-timeline"><summary>查看 ASR 时间轴（{{ selectedProcessing.report.transcript.segments.length }} 段）</summary><ol><li v-for="line in selectedProcessing.report.transcript.segments" :key="line.index"><span>{{ formatDuration(line.startMs) }}–{{ formatDuration(line.endMs) }}</span><p>{{ line.text }}</p></li></ol></details><details v-if="selectedProcessing.report.transcript.text" class="processing-transcript"><summary>查看完整逐字稿</summary><p>{{ selectedProcessing.report.transcript.text }}</p></details></div></details>
        <details class="advanced-panel"><summary><span>高级诊断 · Segment / Chunk</span><svg class="disclosure-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m6 9 6 6 6-6" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8"/></svg></summary><div class="debug-tree"><div v-for="segmentItem in selectedDebugSession.segments" :key="segmentItem.segment.id" class="debug-segment"><div class="debug-line"><span>Segment #{{ segmentItem.segment.index }} · {{ segmentItem.segment.status }}</span><span>{{ segmentItem.chunks.length }} 块 · {{ segmentItem.segment.mimeType || '—' }}</span></div><div class="debug-line muted-line"><span>{{ segmentItem.segment.id }}</span><span>{{ formatDate(segmentItem.segment.startedAt) }}</span></div><div class="chunk-list"><div v-for="chunk in segmentItem.chunks" :key="chunk.id" class="chunk-row"><span>#{{ chunk.index }}</span><span>{{ formatBytes(chunk.size) }}</span><span>{{ formatDuration(chunk.elapsedMs) }}</span><span>{{ formatDate(chunk.createdAt) }}</span><span>{{ chunk.uploadStatus }}</span></div></div><button class="secondary-button restore-button" type="button" :disabled="restoringSegmentId === segmentItem.segment.id || segmentItem.chunks.length === 0" @click="restoreSegmentForPlayback(segmentItem.segment.id)">{{ restoringSegmentId === segmentItem.segment.id ? '正在重组…' : segmentItem.chunks.length === 0 ? '暂无可播放 Chunk' : '按序重组并验证' }}</button><div v-if="restoredUrls[segmentItem.segment.id]" class="restore-result"><audio :key="restoredUrls[segmentItem.segment.id]" :src="restoredUrls[segmentItem.segment.id]" controls preload="auto" @loadedmetadata="updateRestoredSegmentDuration($event, segmentItem.segment.id)"></audio><span v-if="restoredMeta[segmentItem.segment.id]">{{ formatDuration(restoredMeta[segmentItem.segment.id].durationMs) }} · {{ restoredMeta[segmentItem.segment.id].chunkCount }} 块 · {{ restoredMeta[segmentItem.segment.id].playbackMode === 'media-source' ? '本地顺序追加' : '兼容回退' }}</span><button class="text-button restore-download" type="button" @click="downloadSegmentRecording(selectedSessionId || '', segmentItem.segment.id, `livenote-segment-${segmentItem.segment.index}.webm`)">下载 Segment</button></div></div></div></details>
        <details v-if="selectedDebugSession.markers.length" class="advanced-panel"><summary>标记记录 · {{ selectedDebugSession.markers.length }} 条</summary><div class="marker-list"><details v-for="marker in selectedDebugSession.markers" :key="marker.id" class="marker-item"><summary class="marker-row"><span class="marker-type">{{ markerTypeLabel(marker.type) }}</span><span class="marker-time">{{ formatDuration(marker.elapsedMs) }}</span><span class="marker-preview">{{ marker.note ? '有备注' : '无备注' }}</span><span class="marker-status">{{ marker.uploadStatus === 'UPLOADED' ? '已上传' : '本地' }}</span></summary><div class="marker-detail"><span>备注：{{ marker.note || '无备注' }}</span><span>记录时间：{{ formatDate(marker.createdAt) }}</span></div></details></div></details>
      </section></article></template></div><p v-else class="empty-state session-empty">{{ sessionQuery ? '没有找到匹配的会话。' : '还没有录音，完成第一场直播后会显示在这里。' }}</p>
    </section>

    <section v-else class="app-view settings-view" aria-label="设置页面">
      <div class="view-heading"><div><p>录音默认值、诊断和本地数据</p></div></div>
      <details class="settings-group pwa-settings-group" :open="pwaSnapshot.status !== 'installed'"><summary>安装 LiveNote <span>{{ pwaStatusLabel }}</span></summary><div class="settings-group-body"><p class="settings-help">安装后的 PWA 会从手机主屏幕独立打开。它不会改变录音权限，也不会让网页获得原生后台录音能力。</p><button v-if="pwaSnapshot.status === 'prompt'" class="primary-button" type="button" @click="installPwa">安装到主屏幕</button><ol v-else class="pwa-install-steps"><li>确认当前使用 HTTPS 地址。</li><li>打开 Android Chrome 右上角菜单。</li><li>选择“安装应用”或“添加到主屏幕”。</li><li>从手机主屏幕重新打开 LiveNote；页面会显示“已安装并正在使用”。</li></ol><p v-if="pwaSnapshot.status === 'unavailable'" class="settings-help">当前开发环境未注册生产 Service Worker，不能把开发页当作完整 PWA 验收。正式构建部署后再测试安装。</p></div></details>
      <details class="settings-group" open><summary>录音设置 <span>{{ selectedProfileDetails.name }}</span></summary><div class="settings-group-body"><div v-if="!comparisonMode" class="profile-selector" role="radiogroup" aria-label="默认录音配置"><button v-for="profile in AUDIO_PROFILES" :key="profile.id" class="profile-option" :class="{ selected: selectedProfile === profile.id }" type="button" :aria-checked="selectedProfile === profile.id" role="radio" :disabled="isRecording || isBusy" @click="selectedProfile = profile.id"><span class="profile-radio"></span><span><strong>{{ profile.name }}</strong><small>{{ profile.description }}</small></span></button></div><p class="settings-help">MIME 类型由浏览器自动选择；当前默认建议使用 Speech。实际输入设置以浏览器返回值为准。</p></div></details>
      <details class="settings-group" :open="comparisonMode"><summary>音质对比 <span>{{ comparisonMode ? comparisonStepLabel : '未进行' }}</span></summary><div class="settings-group-body"><div v-if="!comparisonMode" class="settings-action-row"><div><strong>三组音质测试</strong><p>对同一段声音进行 Browser Default、Speech、Raw-ish 对比。</p></div><button class="secondary-button" type="button" :disabled="!canStart" @click="beginComparison">开始对比</button></div><div v-else class="comparison-guide"><div class="comparison-guide-top"><strong>{{ comparisonStepLabel }} · {{ selectedProfileDetails.name }}</strong><button class="text-button" type="button" :disabled="isRecording || isBusy" @click="endComparison">结束对比</button></div><div class="comparison-steps"><span v-for="(profileId, index) in COMPARISON_ORDER" :key="profileId" class="comparison-step" :class="{ current: index === comparisonIndex, done: index < comparisonIndex }">{{ index + 1 }}. {{ AUDIO_PROFILES.find((profile) => profile.id === profileId)?.shortName }}</span></div><p>手机 A 播放同一段内容，播放时开始录音，结束后停止。</p></div></div></details>
      <details class="settings-group"><summary>浏览器能力 <span>{{ capabilities.indexedDB ? '本地存储可用' : '需要检查' }}</span></summary><div class="settings-group-body"><div class="capability-list"><div v-for="([name, supported]) in capabilityRows" :key="name" class="capability-row"><span>{{ name }}</span><span class="status" :class="supported ? 'is-ok' : 'is-no'"><i></i>{{ supported ? '可用' : '不可用' }}</span></div></div><div class="storage-estimate"><span>本地空间</span><strong :class="storageEstimateClass">{{ storageEstimateLabel }}</strong></div><div class="storage-estimate"><span>持久化存储</span><strong>{{ persistentStorage === true ? '已启用' : persistentStorage === false ? '未获批准' : '浏览器未提供' }}</strong></div><p class="settings-help">录音会先保存到手机浏览器本地空间；接近上限时请先上传、导出或删除旧 Session。持久化存储由浏览器策略决定。</p><button class="text-button capability-refresh" type="button" @click="refreshCapabilities">重新检测</button><p v-if="storageError" class="error-message">{{ storageError }}</p></div></details>
      <details class="settings-group"><summary>实际麦克风设置 <span>{{ settingsRows.length ? '已读取' : '录音后可见' }}</span></summary><div class="settings-group-body"><div v-if="settingsRows.length" class="settings-table"><div v-for="row in settingsRows" :key="row.key" class="settings-row"><span>{{ row.key }}</span><code>{{ row.value }}</code></div></div><p v-else class="empty-state">开始录音后，这里会显示浏览器实际提供的输入设置。</p></div></details>
      <details class="settings-group"><summary>连接状态 <span>{{ uploadSnapshot.serverCompatible === false ? 'API 需重启' : uploadSnapshot.serverOnline === true ? '服务器在线' : uploadSnapshot.serverOnline === false ? '服务器离线' : '未检测' }}</span></summary><div class="settings-group-body"><div class="settings-status-list"><div><span>设备网络</span><strong>{{ networkOnline ? '在线' : '离线（录音继续）' }}</strong></div><div><span>服务器 API</span><strong>{{ uploadSnapshot.serverCompatible === false ? '在线但版本过旧' : uploadSnapshot.serverOnline === true ? '在线' : uploadSnapshot.serverOnline === false ? '离线 / 请求失败' : '未检测' }}</strong></div><div><span>上传队列</span><strong>{{ uploadSnapshot.uploaded }} / {{ uploadSnapshot.total }}</strong></div><div><span>音频处理</span><strong>{{ uploadSnapshot.serverCapabilities?.ffmpeg && uploadSnapshot.serverCapabilities?.ffprobe ? 'FFmpeg 可用' : uploadSnapshot.serverOnline === true ? 'FFmpeg 未就绪' : '未检测' }}</strong></div><div><span>ASR 模型</span><strong>{{ uploadSnapshot.serverCapabilities ? (uploadSnapshot.serverCapabilities.asrModelCached ? uploadSnapshot.serverCapabilities.asrModel : uploadSnapshot.serverCapabilities.asrModel + ' 未缓存') : '未检测' }}</strong></div><div><span>语义总结</span><strong>{{ uploadSnapshot.serverCapabilities ? (uploadSnapshot.serverCapabilities.llmConfigured ? '已配置' : '未配置') : '未检测' }}</strong></div></div><p v-if="uploadSnapshot.lastError" class="upload-error">{{ uploadSnapshot.lastError }}</p></div></details>
      <details class="settings-group"><summary>错误诊断 <span>不包含音频</span></summary><div class="settings-group-body"><p class="settings-help">遇到错误时，可生成页面状态 JSON，并附加截图、日志或 JSON 文件。这里不会上传录音 Blob。</p><textarea v-model="diagnosticDescription" class="diagnostic-description" rows="2" placeholder="可选：描述你刚刚做了什么、看到了什么错误"></textarea><div class="diagnostic-actions"><label class="secondary-button diagnostic-file-button">选择图片或日志<input type="file" accept="image/*,.log,.txt,.json,.har,.csv" @change="handleDiagnosticFile" /></label><button class="secondary-button" type="button" @click="downloadDiagnosticBundle">下载诊断 JSON</button><button class="primary-button" type="button" :disabled="diagnosticState.status === 'uploading'" @click="uploadDiagnosticBundle">{{ diagnosticState.status === 'uploading' ? '上传中…' : '上传诊断资料' }}</button></div><small v-if="diagnosticFile" class="diagnostic-file-name">附件：{{ diagnosticFile.name }}</small><small v-if="diagnosticState.message" class="diagnostic-status" :class="'diagnostic-' + diagnosticState.status">{{ diagnosticState.message }}</small></div></details>
      <details class="settings-group danger-group"><summary>本地数据 <span>{{ debugSessions.length }} 场录音</span></summary><div class="settings-group-body"><p class="settings-help">会话列表中的单个删除会在服务器在线时同步删除服务器副本；服务器离线时，只允许删除没有已上传 Chunk 的本地 Session。</p><div class="settings-action-row"><button class="text-button" type="button" :disabled="isRecording" @click="resetTest">清空当前页面状态</button><button class="danger-text-button" type="button" :disabled="isRecording || isBusy" @click="clearLocalTestData">清理本地全部测试数据</button></div></div></details>
    </section>

    <nav class="bottom-nav" aria-label="主导航"><button type="button" :class="{ active: activeTab === 'recording' }" @click="activeTab = 'recording'"><span class="nav-icon">●</span><span>录音</span></button><button type="button" :class="{ active: activeTab === 'sessions' }" @click="openSessions()"><span class="nav-icon">▤</span><span>会话</span></button><button type="button" :class="{ active: activeTab === 'settings' }" @click="openSettings"><span class="nav-icon">⋯</span><span>设置</span></button></nav>
    <footer class="footer-note"><span>LiveNote</span><span>本地优先 · 录音不断</span></footer>
  </main>
</template>

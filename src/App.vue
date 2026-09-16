<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { AUDIO_PROFILES, type AudioProfileId } from './audio/AudioProfile'
import { detectAudioCapabilities, type AudioCapabilities } from './audio/AudioCapabilities'
import { AudioLevelMonitor } from './audio/AudioLevelMonitor'
import { MediaRecorderEngine } from './audio/MediaRecorderEngine'
import type { RecorderChunk, RecorderState } from './audio/RecorderEngine'
import { PageLifecycleManager, type LifecycleEvent } from './lifecycle/PageLifecycleManager'
import { WakeLockManager, type WakeLockState } from './lifecycle/WakeLockManager'
import { openDatabase } from './storage/db'
import { ChunkStore } from './storage/ChunkStore'
import { LifecycleStore } from './storage/LifecycleStore'
import { MarkerStore } from './storage/MarkerStore'
import { createSegmentPlayback, type SegmentPlayback } from './storage/SegmentRecovery'
import { SegmentStore } from './storage/SegmentStore'
import { SessionStore } from './storage/SessionStore'
import { sha256Blob } from './storage/sha256'
import type { ChunkRecord, LifecycleEventRecord, MarkerRecord, MarkerType, SegmentRecord, SessionRecord } from './storage/types'
import { uploadQueue, type UploadQueueSnapshot } from './upload/UploadQueue'

interface DebugSegment { segment: SegmentRecord; chunks: ChunkRecord[] }
interface DebugSession { session: SessionRecord; segments: DebugSegment[] }

const COMPARISON_ORDER: AudioProfileId[] = ['browser-default', 'speech', 'raw-ish']
const CHUNK_TIMESLICE_MS = 30_000

const capabilities = ref<AudioCapabilities>(detectAudioCapabilities())
const selectedProfile = ref<AudioProfileId>('raw-ish')
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
const currentChunkIndex = ref(-1)
const savedBytes = ref(0)
const lastSavedChunk = ref<ChunkRecord | null>(null)
const recoverySession = ref<SessionRecord | null>(null)
const debugSessions = ref<DebugSession[]>([])
const restoredUrls = ref<Record<string, string>>({})
const restoredMeta = ref<Record<string, SegmentPlayback>>({})
const restoringSegmentId = ref<string | null>(null)
const comparisonMode = ref(false)
const comparisonIndex = ref(0)
const lifecycleEvents = ref<LifecycleEventRecord[]>([])
const pageVisibility = ref<DocumentVisibilityState>(document.visibilityState)
const uploadSnapshot = ref<UploadQueueSnapshot>({ serverOnline: null, isUploading: false, total: 0, uploaded: 0, pending: 0, failed: 0, lastError: '' })
const sessionUpload = ref({ total: 0, uploaded: 0, pending: 0, failed: 0 })
const lastMarkerMessage = ref('')

const engine = new MediaRecorderEngine()
const levelMonitor = new AudioLevelMonitor((nextLevel) => { level.value = nextLevel })
const wakeLockManager = new WakeLockManager((state, message) => {
  wakeLockState.value = state
  wakeLockMessage.value = message ?? ''
})
const pageLifecycleManager = new PageLifecycleManager((event) => { pageVisibility.value = event.visibilityState; void persistLifecycleEvent(event) })
const unsubscribeUploadQueue = uploadQueue.subscribe((snapshot) => { uploadSnapshot.value = snapshot; void refreshSessionUpload() })
let durationTimer: number | null = null
let activeMonotonicStartedAt = 0
let activeElapsedBaseMs = 0
let activeSegmentBaseMs = 0

const selectedProfileDetails = computed(() => AUDIO_PROFILES.find((profile) => profile.id === selectedProfile.value) ?? AUDIO_PROFILES[0])
const isRecording = computed(() => recorderState.value === 'recording')
const comparisonComplete = computed(() => comparisonMode.value && comparisonIndex.value >= COMPARISON_ORDER.length)
const canStart = computed(() => storageReady.value && !isBusy.value && !isRecording.value && !comparisonComplete.value)
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

async function refreshSessionUpload(sessionId = currentSession.value?.id): Promise<void> {
  if (!sessionId) {
    sessionUpload.value = { total: 0, uploaded: 0, pending: 0, failed: 0 }
    return
  }
  try {
    sessionUpload.value = await uploadQueue.snapshotForSession(sessionId)
  } catch {
    sessionUpload.value = { total: 0, uploaded: 0, pending: 0, failed: 0 }
  }
}

function resetRestoredAudio(event: Event): void {
  const audio = event.currentTarget as HTMLAudioElement
  audio.pause()
  try { audio.currentTime = 0 } catch { /* The media timeline may not be ready yet. */ }
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
  currentChunkIndex.value = persistedRecord.index
  savedBytes.value += persistedRecord.size
  elapsedMs.value = Math.max(elapsedMs.value, persistedRecord.elapsedMs)
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
  lastMarkerMessage.value = `${type} 已本地保存 · ${formatDuration(marker.elapsedMs)}`
  uploadQueue.kick()
}

async function createSession(): Promise<SessionRecord> {
  const now = Date.now()
  const session: SessionRecord = { id: createId('session'), title: `LiveNote ${new Date(now).toLocaleString()}`, startedAt: now, endedAt: null, status: 'RECORDING', durationMs: 0, createdAt: now, updatedAt: now }
  await SessionStore.put(session)
  currentSession.value = session
  return session
}

async function createSegment(session: SessionRecord): Promise<SegmentRecord> {
  const segment: SegmentRecord = { id: createId('segment'), sessionId: session.id, index: await SegmentStore.nextIndex(session.id), startedAt: Date.now(), endedAt: null, mimeType: '', mediaSettings: {}, status: 'RECORDING', durationMs: 0 }
  await SegmentStore.put(segment)
  currentSegment.value = segment
  return segment
}

async function hydrateSessionStats(sessionId: string): Promise<void> {
  const chunks = await ChunkStore.listBySessionId(sessionId)
  savedChunkCount.value = chunks.length
  currentChunkIndex.value = chunks.length ? chunks[chunks.length - 1].index : -1
  savedBytes.value = chunks.reduce((total, chunk) => total + chunk.size, 0)
  lastSavedChunk.value = chunks[chunks.length - 1] ?? null
}

function startDurationTimer(baseMs: number): void {
  stopDurationTimer()
  activeElapsedBaseMs = baseMs
  activeMonotonicStartedAt = performance.now()
  elapsedMs.value = baseMs
  durationTimer = window.setInterval(() => { elapsedMs.value = activeElapsedBaseMs + Math.max(0, performance.now() - activeMonotonicStartedAt) }, 200)
}

function stopDurationTimer(): void {
  if (durationTimer !== null) { window.clearInterval(durationTimer); durationTimer = null }
}

async function startRecording(): Promise<void> {
  if (!canStart.value) return
  errorMessage.value = ''
  storageError.value = ''
  isBusy.value = true
  let session: SessionRecord | null = currentSession.value
  let segment: SegmentRecord | null = null
  try {
    if (!session || (!comparisonMode.value && session.status === 'COMPLETED')) session = await createSession()
    else await hydrateSessionStats(session.id)
    segment = await createSegment(session)
    const sessionElapsedBaseMs = session.durationMs
    activeSegmentBaseMs = sessionElapsedBaseMs
    const mediaSession = await engine.start(selectedProfile.value, { timesliceMs: CHUNK_TIMESLICE_MS, sessionElapsedBaseMs, onChunk: (chunk) => persistChunk(chunk, session as SessionRecord, segment as SegmentRecord) })
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
    await refreshSessionUpload(session.id)
    uploadQueue.kick()
  } catch (error) {
    recorderState.value = 'error'
    pageLifecycleManager.setRecording(false)
    engine.dispose()
    if (segment) {
      const interruptedSegment = { ...segment, status: 'INTERRUPTED' as const, endedAt: Date.now() }
      await SegmentStore.put(interruptedSegment).catch(() => undefined)
      currentSegment.value = interruptedSegment
    }
    if (session) {
      const interruptedSession = { ...session, status: 'INTERRUPTED' as const, updatedAt: Date.now() }
      await SessionStore.put(interruptedSession).catch(() => undefined)
      currentSession.value = interruptedSession
    }
    errorMessage.value = error instanceof Error ? error.message : '无法开始录音。'
  } finally { isBusy.value = false }
}

async function stopRecording(): Promise<void> {
  if (!isRecording.value || isBusy.value) return
  isBusy.value = true
  recorderState.value = 'stopping'
  stopDurationTimer()
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
      void uploadQueue.completeSegment(session.id, segment.id)
      if (!comparisonMode.value || comparisonIndex.value >= COMPARISON_ORDER.length - 1) void uploadQueue.completeSession(session.id)
      void refreshSessionUpload(session.id)
    }
    await refreshDebugData()
  } catch (error) {
    recorderState.value = 'error'
    errorMessage.value = error instanceof Error ? error.message : '无法停止录音。'
  } finally { pageLifecycleManager.setRecording(false); levelMonitor.stop(); await wakeLockManager.release(); engine.dispose(); isBusy.value = false }
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

async function loadStorage(): Promise<void> {
  if (!capabilities.value.indexedDB) { storageError.value = '当前浏览器没有 IndexedDB，无法进入 M3 可靠录音模式。'; return }
  try {
    await openDatabase()
    storageReady.value = true
    const openSessions = await SessionStore.listOpen()
    recoverySession.value = openSessions[0] ?? null
    await refreshSessionUpload(currentSession.value?.id)
    await refreshDebugData()
  } catch (error) { storageError.value = error instanceof Error ? error.message : '无法打开 IndexedDB。' }
}

async function continueRecovery(): Promise<void> {
  if (!recoverySession.value) return
  const recoverySegments = await SegmentStore.listBySessionId(recoverySession.value.id)
  const interruptedAt = Date.now()
  await Promise.all(recoverySegments.filter((segment) => segment.status === 'RECORDING').map((segment) => (
    SegmentStore.put({ ...segment, status: 'INTERRUPTED', endedAt: interruptedAt })
  )))
  const session = { ...recoverySession.value, status: 'RECORDING' as const, endedAt: null, updatedAt: Date.now() }
  await SessionStore.put(session)
  currentSession.value = session
  recoverySession.value = null
  comparisonMode.value = false
  resetCurrentView()
  await startRecording()
}

async function finishRecovery(): Promise<void> {
  const session = recoverySession.value
  if (!session) return
  const segments = await SegmentStore.listBySessionId(session.id)
  const endedAt = Date.now()
  await Promise.all(segments.filter((segment) => segment.status === 'RECORDING').map((segment) => SegmentStore.put({ ...segment, status: 'INTERRUPTED', endedAt })))
  await SessionStore.put({ ...session, status: 'COMPLETED' as const, endedAt, updatedAt: endedAt })
  recoverySession.value = null
  await refreshDebugData()
}

async function refreshDebugData(): Promise<void> {
  if (!storageReady.value) return
  const sessions = await SessionStore.list()
  const nested: DebugSession[] = []
  for (const session of sessions) {
    const segments = await SegmentStore.listBySessionId(session.id)
    nested.push({ session, segments: await Promise.all(segments.map(async (segment) => ({ segment, chunks: await ChunkStore.listBySegmentId(segment.id) }))) })
  }
  debugSessions.value = nested
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

function refreshCapabilities(): void { capabilities.value = detectAudioCapabilities(); void loadStorage() }

onMounted(() => { wakeLockManager.start(); pageLifecycleManager.start(); uploadQueue.start(); void loadStorage() })
onBeforeUnmount(() => { stopDurationTimer(); pageLifecycleManager.stop(); levelMonitor.stop(); wakeLockManager.stop(); uploadQueue.stop(); unsubscribeUploadQueue(); engine.dispose(); Object.values(restoredUrls.value).forEach((url) => URL.revokeObjectURL(url)) })
</script>

<template>
  <main class="app-shell">
    <header class="topbar"><h1>LiveNote</h1><span class="stage-tag">本地录音</span></header>

    <details class="section-block capability-section">
      <summary class="section-heading capability-summary"><div><p class="eyebrow">01 / CHECK</p><h3>浏览器能力</h3></div><span class="collapse-label">{{ capabilities.indexedDB ? '本地存储可用' : '点击检查' }}</span></summary>
      <div class="capability-body"><div class="capability-list"><div v-for="([name, supported]) in capabilityRows" :key="name" class="capability-row"><span>{{ name }}</span><span class="status" :class="supported ? 'is-ok' : 'is-no'"><i></i>{{ supported ? '可用' : '不可用' }}</span></div></div><button class="text-button capability-refresh" type="button" @click="refreshCapabilities">重新检测 ↗</button><p v-if="storageError" class="error-message">{{ storageError }}</p></div>
    </details>

    <section v-if="recoverySession" class="recovery-banner" aria-labelledby="recovery-title"><div><p class="eyebrow accent">RECOVERY NEEDED</p><h3 id="recovery-title">发现未正常结束的 Session</h3><p>{{ recoverySession.title }} · 已记录 {{ formatDuration(recoverySession.durationMs) }}</p></div><div class="recovery-actions"><button class="primary-button compact-button" type="button" @click="continueRecovery">继续这场直播</button><button class="stop-button compact-button" type="button" @click="finishRecovery">结束并保存</button></div></section>

    <details class="section-block diagnostics-section" :open="comparisonMode">
      <summary class="section-heading diagnostics-summary"><div><p class="eyebrow">02 / DIAGNOSTICS</p><h3>录音诊断</h3></div><span class="collapse-label">{{ comparisonMode ? comparisonStepLabel : '三组音质对比' }}</span></summary>
      <div v-if="!comparisonMode" class="compare-launch diagnostics-content"><div><strong>三组对比测试</strong><p>按 Default → Speech → Raw-ish 录制同一段声音。</p></div><button class="secondary-button" type="button" :disabled="!canStart" @click="beginComparison">开始对比 →</button></div>
      <div v-else class="comparison-guide diagnostics-content"><div class="comparison-guide-top"><div><p class="eyebrow accent">PROFILE COMPARISON</p><strong>{{ comparisonStepLabel }} · {{ selectedProfileDetails.name }}</strong></div><button class="text-button" type="button" :disabled="isRecording || isBusy" @click="endComparison">结束对比</button></div><div class="comparison-steps"><span v-for="(profileId, index) in COMPARISON_ORDER" :key="profileId" class="comparison-step" :class="{ current: index === comparisonIndex, done: index < comparisonIndex }">{{ index + 1 }}. {{ AUDIO_PROFILES.find((profile) => profile.id === profileId)?.shortName }}</span></div><p class="comparison-instruction">手机 A 播放同一段内容，播放时开始录音，结束后停止。</p></div>
    </details>

    <section class="section-block recorder-section" aria-labelledby="recorder-title"><div class="section-heading"><div><p class="eyebrow">03 / RECORDING</p><h3 id="recorder-title">连续录音</h3></div><span class="state-label" :class="`state-${recorderState}`">{{ recorderState }}</span></div>
      <div v-if="!comparisonMode" class="profile-selector" role="radiogroup" aria-label="Audio Profile"><button v-for="profile in AUDIO_PROFILES" :key="profile.id" class="profile-option" :class="{ selected: selectedProfile === profile.id }" type="button" :aria-checked="selectedProfile === profile.id" role="radio" :disabled="isRecording || isBusy" @click="selectedProfile = profile.id"><span class="profile-radio"></span><span><strong>{{ profile.name }}</strong><small>{{ profile.description }}</small></span></button></div>
      <div class="record-control"><div class="record-readout"><span class="record-indicator" :class="{ active: isRecording }"></span><span class="duration">{{ durationLabel }}</span><span class="profile-caption">{{ selectedProfileDetails.shortName }}</span></div><div class="level-meter" aria-label="实时麦克风音量"><span v-for="index in 18" :key="index" class="meter-segment" :class="{ lit: level >= index / 18 }"></span></div><div class="control-actions"><button class="primary-button" type="button" :disabled="!canStart" @click="startRecording"><span class="button-icon">●</span>{{ comparisonMode ? `开始第 ${comparisonIndex + 1} 组` : '开始录音' }}</button><button class="stop-button" type="button" :disabled="!isRecording || isBusy" @click="stopRecording"><span class="button-icon">■</span>{{ comparisonMode && comparisonIndex < COMPARISON_ORDER.length - 1 ? '停止并下一组' : '停止并保存' }}</button></div></div>
      <p v-if="errorMessage" class="error-message" role="alert">{{ errorMessage }}</p>
      <div class="data-grid data-grid-4"><div class="data-field"><span class="field-label">Session ID</span><code>{{ sessionIdLabel }}</code></div><div class="data-field"><span class="field-label">Segment</span><strong>#{{ segmentIndexLabel }}</strong></div><div class="data-field"><span class="field-label">已保存 Chunk</span><strong>{{ savedChunkCount }} · #{{ currentChunkIndex }}</strong></div><div class="data-field"><span class="field-label">IndexedDB 字节</span><strong>{{ formatBytes(savedBytes) }}</strong></div></div><div class="data-grid"><div class="data-field"><span class="field-label">当前 Segment ID</span><code>{{ segmentIdLabel }}</code></div><div class="data-field"><span class="field-label">当前 MIME</span><code>{{ currentMimeType }}</code></div><div class="data-field"><span class="field-label">页面</span><strong>{{ pageVisibility === 'visible' ? '前台' : '后台' }}</strong></div><div class="data-field"><span class="field-label">屏幕常亮</span><strong :class="wakeLockState === 'ACTIVE' ? 'wake-on' : 'wake-off'">{{ wakeLockState === 'ACTIVE' ? '正常' : wakeLockState === 'UNSUPPORTED' ? '不支持' : wakeLockState === 'FAILED' ? '失败（录音继续）' : wakeLockState }}</strong><small v-if="wakeLockMessage" class="field-hint">{{ wakeLockMessage }}</small></div></div><div class="status-strip"><div><span>服务器</span><strong>{{ uploadSnapshot.serverOnline === true ? '在线' : uploadSnapshot.serverOnline === false ? '离线 / 请求失败' : '未检测' }}</strong></div><div><span>本场上传</span><strong>{{ sessionUpload.uploaded }} / {{ sessionUpload.total }}</strong></div><div><span>Pending</span><strong>{{ sessionUpload.pending }}</strong></div><div><span>Failed</span><strong>{{ sessionUpload.failed }}</strong></div></div><div class="local-save-note">{{ uploadSnapshot.serverOnline === true ? '服务器上传异步进行，本地仍保留 Chunk。' : '服务器未连接，录音仍在本地继续保存。' }}</div><small v-if="uploadSnapshot.lastError" class="upload-error">{{ uploadSnapshot.lastError }}</small><div class="marker-actions"><span class="field-label">快速标记</span><button type="button" :disabled="!isRecording" @click="addMarker('KEY_POINT')">重点</button><button type="button" :disabled="!isRecording" @click="addMarker('QUESTION')">疑问</button><button type="button" :disabled="!isRecording" @click="addMarker('IDEA')">灵感</button><button type="button" :disabled="!isRecording" @click="addMarker('TODO')">待办</button><small v-if="lastMarkerMessage">{{ lastMarkerMessage }}</small></div><div v-if="hasLifecycleRisk" class="lifecycle-warning"><strong>录音期间页面曾离开前台</strong><span>最后一次：{{ formatDate(lastHiddenEvent?.wallClockMs ?? null) }} · 风险区间 {{ formatDuration(lifecycleGapMs) }}</span><small>后台期间录音完整性不作保证；返回前台后已尝试重新申请屏幕常亮。</small></div>
    </section>

    <section class="section-block settings-section" aria-labelledby="settings-title"><div class="section-heading"><div><p class="eyebrow">04 / ACTUAL INPUT</p><h3 id="settings-title">实际 MediaTrackSettings</h3></div><span class="muted-label">当前 Segment</span></div><div v-if="settingsRows.length" class="settings-table"><div v-for="row in settingsRows" :key="row.key" class="settings-row"><span>{{ row.key }}</span><code>{{ row.value }}</code></div></div><p v-else class="empty-state">开始录音后，这里会显示实际输入设置。</p></section>

    <section class="section-block debug-section" aria-labelledby="debug-title"><div class="section-heading"><div><p class="eyebrow">05 / STORAGE DEBUG</p><h3 id="debug-title">Session → Segment → Chunk</h3></div><button class="text-button" type="button" @click="refreshDebugData">刷新存储视图 ↗</button></div><div v-if="debugSessions.length" class="debug-tree"><details v-for="item in debugSessions" :key="item.session.id" class="debug-session" open><summary><strong>{{ item.session.title }}</strong><span>{{ item.session.status }} · {{ formatDuration(item.session.durationMs) }}</span></summary><div v-for="segmentItem in item.segments" :key="segmentItem.segment.id" class="debug-segment"><div class="debug-line"><span>Segment #{{ segmentItem.segment.index }} · {{ segmentItem.segment.status }}</span><span>{{ segmentItem.chunks.length }} chunks · {{ segmentItem.segment.mimeType || '—' }}</span></div><div class="debug-line muted-line"><span>{{ segmentItem.segment.id }}</span><span>{{ formatDate(segmentItem.segment.startedAt) }}</span></div><div class="chunk-list"><div v-for="chunk in segmentItem.chunks" :key="chunk.id" class="chunk-row"><span>#{{ chunk.index }}</span><span>{{ formatBytes(chunk.size) }}</span><span>{{ formatDuration(chunk.elapsedMs) }}</span><span>{{ formatDate(chunk.createdAt) }}</span><span>{{ chunk.uploadStatus }}</span></div></div><button class="secondary-button restore-button" type="button" :disabled="restoringSegmentId === segmentItem.segment.id" @click="restoreSegmentForPlayback(segmentItem.segment.id)">{{ restoringSegmentId === segmentItem.segment.id ? '正在重组…' : '按序重组并验证' }}</button><div v-if="restoredUrls[segmentItem.segment.id]" class="restore-result"><audio :key="restoredUrls[segmentItem.segment.id]" :src="restoredUrls[segmentItem.segment.id]" controls preload="auto" @loadedmetadata="resetRestoredAudio"></audio><span v-if="restoredMeta[segmentItem.segment.id]">{{ restoredMeta[segmentItem.segment.id].chunkCount }} chunks · {{ formatBytes(restoredMeta[segmentItem.segment.id].totalBytes) }} · {{ restoredMeta[segmentItem.segment.id].hasGaps ? '发现 index 间隙' : 'index 连续' }} · 播放方式：{{ restoredMeta[segmentItem.segment.id].playbackMode === 'media-source' ? 'MediaSource 顺序追加' : 'Blob 兼容回退' }}</span><span v-else>正在建立播放器…</span></div></div></details></div><p v-else class="empty-state">IndexedDB 中还没有 Session。</p></section>

    <div class="footer-actions"><button class="text-button" type="button" :disabled="isRecording" @click="resetTest">清空当前页面状态</button></div><footer class="footer-note"><span>LiveNote / M4 + M5</span><span>本地优先 · 异步上传 · 无 ASR</span></footer>
  </main>
</template>

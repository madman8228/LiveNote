import type { ChunkRecord, MarkerRecord, SegmentRecord, SessionRecord } from '../storage/types'
import type { PlaybackDiagnosticsSink } from '../diagnostics/PlaybackDiagnostics'

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) || '/api/v1'
const API_KEY = (import.meta.env.VITE_API_KEY as string | undefined) || ''
const API_TIMEOUT_MS = Math.max(10_000, Number(import.meta.env.VITE_API_TIMEOUT_MS ?? 30_000) || 30_000)

export class ApiRequestError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiRequestError'
  }
}

export interface UploadStateResponse {
  sessionId: string
  segments: Array<{
    segmentId: string
    chunks: Array<{ index: number; size: number; sha256: string }>
  }>
}

export interface ServerCapabilities {
  processingMode?: 'local' | 'storage' | string
  storageOnly?: boolean
  ffmpeg: boolean
  ffprobe: boolean
  manualProcessing?: boolean
  localTranscription?: boolean
  liveIncrementalProcessing?: boolean
}

export interface HealthResponse {
  ok: boolean
  service: string
  storageSchema?: number
  capabilities?: ServerCapabilities
}

export const REQUIRED_SERVER_STORAGE_SCHEMA = 9

export function isCompatibleServerHealth(health: HealthResponse): boolean {
  return (health.storageSchema ?? 0) >= REQUIRED_SERVER_STORAGE_SCHEMA && Boolean(health.capabilities)
}

export interface LiveProcessingStatus {
  runId: string
  status: string
  completedWindows: number
  totalWindows: number
  processedDurationMs: number
  totalDurationMs: number
  uploadedChunks: number
  lastError: string
  errorCode: string
  canRetry: boolean
  updatedAt: number
  lastContiguousChunk: number
  lastWindowDurationMs: number
  lastAsrDurationMs: number
  peakStagingBytes: number
}

export interface TranscriptResponse {
  ok?: boolean
  sessionId?: string
  model: string
  device: string
  language: string
  audioQuality?: { meanVolumeDb: number | null; maxVolumeDb: number | null }
  chunked?: boolean
  chunkDurationSeconds?: number
  chunkOverlapSeconds?: number
  text: string
  segments: Array<{ index: number; startMs: number; endMs: number; text: string }>
}

export interface AdminTranscriptResponse {
  taskId: string
  sessionId: string
  status: ProcessingTaskStatus
  run: { id: string; generation: number; source_hash: string; model: string; language: string; status: string; completed_at: number | null }
  transcript: TranscriptResponse
}

export interface ReportMarker {
  id?: string
  elapsedMs: number
  wallClockMs?: number
  note?: string
  createdAt?: number
}

export interface ContentSummary {
  title: string
  overview: string
  keyPoints: string[]
  knowledgeStructure: Array<{ title: string; points: string[] }>
  questions: Array<{ question: string; answer: string; startMs: number | null }>
  actionItems: string[]
  entities: string[]
  confidenceNotes: string[]
}

export interface ReportResponse {
  ok?: boolean
  sessionId?: string
  version: number
  processingMode: string
  summaryStatus: string
  session?: { id?: string; title?: string; durationMs?: number }
  summaryError?: string | null
  summary?: ContentSummary | null
  localDraft?: { overviewPreview: string; keyPoints: ReportMarker[]; questions: ReportMarker[]; ideas: ReportMarker[]; todos: ReportMarker[]; source?: string; knowledgeStructure?: Array<{ title: string; points: string[] }> }
  analysisProvider?: string
  transcript: TranscriptResponse
  counts: Record<string, number>
  timeline?: TranscriptResponse['segments']
  markers?: Record<string, ReportMarker[]>
}

export type ProcessingTaskStatus = 'READY' | 'CLAIMED' | 'LOCAL_READY' | 'TRANSCRIBING' | 'TRANSCRIBED' | 'SUMMARIZING' | 'PROCESSING' | 'REVIEW' | 'READY_TO_UPLOAD' | 'COMPLETED' | 'FAILED' | string

export interface ProcessingProgress {
  completedParts: number
  totalParts: number
  percent: number
}

export interface ProcessingTask {
  id: string
  sessionId: string
  title: string | null
  ownerId?: string | null
  ownerName?: string | null
  durationMs: number | null
  sessionStatus: string | null
  sourceId?: string
  sourceLabel?: string
  status: ProcessingTaskStatus
  attempts: number
  claimedBy: string | null
  claimedAt: number | null
  errorMessage: string
  resultVersion: number
  createdAt: number
  updatedAt: number
  requestedWorkerId?: string | null
  leaseExpiresAt?: number | null
  downloadedAt?: number | null
  errorStage?: string
  adminNote?: string
  progress?: ProcessingProgress | null
}

export interface ServerResultResponse {
  sessionId: string
  taskId: string
  status: ProcessingTaskStatus
  version: number
  updatedAt: number
  result: Record<string, unknown> | null
  revisionId?: string | null
}

export interface AdminUser {
  id: string
  display_name?: string
  displayName?: string
  status: string
  session_count?: number
  device_count?: number
  created_at?: number
  updated_at?: number
}

export interface AdminResultRevision {
  id: string
  taskId: string
  sessionId: string
  version: number
  createdAt: number
  result: Record<string, unknown>
}

export interface DiagnosticUploadResponse {
  ok: boolean
  diagnosticId: string
  files: string[]
}

async function fetchWithTimeout(url: string, init: RequestInit = {}): Promise<Response> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), API_TIMEOUT_MS)
  const callerSignal = init.signal
  const abortFromCaller = () => controller.abort()
  if (callerSignal) {
    if (callerSignal.aborted) controller.abort()
    else callerSignal.addEventListener('abort', abortFromCaller, { once: true })
  }
  try {
    return await fetch(url, { ...init, signal: controller.signal })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError' && !callerSignal?.aborted) {
      throw new Error(`服务器请求超时（${Math.round(API_TIMEOUT_MS / 1000)} 秒）；录音仍会继续保存在本机。`)
    }
    throw error
  } finally {
    window.clearTimeout(timer)
    callerSignal?.removeEventListener('abort', abortFromCaller)
  }
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const adminSession = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem('livenote-admin-session') || '' : ''
  const adminToken = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem('livenote-admin-token') || '' : ''
  const deviceToken = typeof localStorage !== 'undefined' ? localStorage.getItem('livenote-device-token') || '' : ''
  const identityHeaders: Record<string, string> = path.startsWith('/admin') && adminSession
    ? { 'X-Admin-Session': adminSession }
    : path.startsWith('/admin') && adminToken
      ? { 'X-Admin-Token': adminToken }
    : deviceToken
      ? { Authorization: `Bearer ${deviceToken}` }
      : {}
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    ...init,
    headers: { Accept: 'application/json', ...(API_KEY ? { 'X-API-Key': API_KEY } : {}), ...identityHeaders, ...(init.headers ?? {}) },
  })
  if (!response.ok) {
    if ([401, 503].includes(response.status) && path.startsWith('/admin')) {
      ApiClient.clearAdminAuth()
    }
    const detail = await response.text().catch(() => '')
    throw new ApiRequestError(response.status, `服务器请求失败 (${response.status})${detail ? `：${detail}` : ''}`)
  }
  return response.json() as Promise<T>
}

async function requestAdminBlob(path: string, init: RequestInit = {}): Promise<Blob> {
  const adminSession = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem('livenote-admin-session') || '' : ''
  const adminToken = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem('livenote-admin-token') || '' : ''
  const identityHeaders: Record<string, string> = adminSession
    ? { 'X-Admin-Session': adminSession }
    : adminToken
      ? { 'X-Admin-Token': adminToken }
      : {}
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    ...init,
    headers: { Accept: 'audio/webm', ...(API_KEY ? { 'X-API-Key': API_KEY } : {}), ...identityHeaders, ...(init.headers ?? {}) },
  })
  if (!response.ok) {
    if ([401, 503].includes(response.status)) {
      ApiClient.clearAdminAuth()
    }
    const detail = await response.text().catch(() => '')
    throw new ApiRequestError(response.status, `服务器请求失败 (${response.status})${detail ? `：${detail}` : ''}`)
  }
  return response.blob()
}

async function requestWorkerJson<T>(path: string, workerToken: string, init: RequestInit = {}): Promise<T> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(API_KEY ? { 'X-API-Key': API_KEY } : {}),
      'X-Worker-Token': workerToken,
      ...(init.headers ?? {}),
    },
  })
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new ApiRequestError(response.status, `服务器请求失败 (${response.status})${detail ? `：${detail}` : ''}`)
  }
  return response.json() as Promise<T>
}

async function requestWorkerBlob(path: string, workerToken: string, init: RequestInit = {}): Promise<Blob> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'audio/webm',
      ...(API_KEY ? { 'X-API-Key': API_KEY } : {}),
      'X-Worker-Token': workerToken,
      ...(init.headers ?? {}),
    },
  })
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new ApiRequestError(response.status, `服务器请求失败 (${response.status})${detail ? `：${detail}` : ''}`)
  }
  return response.blob()
}

export const ApiClient = {
  health() {
    return requestJson<HealthResponse>('/health')
  },
  adminAuthStatus() {
    return requestJson<{ configured: boolean; setupAvailable: boolean }>('/auth/admin/status')
  },
  adminSetup(username: string, password: string) {
    return requestJson<{ sessionToken: string; expiresInSeconds: number }>('/auth/admin/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username.trim(), password }),
    })
  },
  adminLogin(username: string, password: string) {
    return requestJson<{ sessionToken: string; expiresInSeconds: number }>('/auth/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: username.trim(), password }),
    })
  },
  setAdminSession(token: string) {
    if (typeof sessionStorage !== 'undefined') sessionStorage.setItem('livenote-admin-session', token.trim())
  },
  clearAdminSession() {
    if (typeof sessionStorage !== 'undefined') sessionStorage.removeItem('livenote-admin-session')
  },
  clearAdminAuth() {
    if (typeof sessionStorage !== 'undefined') {
      sessionStorage.removeItem('livenote-admin-session')
      sessionStorage.removeItem('livenote-admin-token')
    }
  },
  setAdminToken(token: string) {
    if (typeof sessionStorage !== 'undefined') sessionStorage.setItem('livenote-admin-token', token.trim())
  },
  setDeviceToken(token: string) {
    if (typeof localStorage !== 'undefined') localStorage.setItem('livenote-device-token', token.trim())
  },
  clearDeviceToken() {
    if (typeof localStorage !== 'undefined') localStorage.removeItem('livenote-device-token')
  },
  setWorkerToken(token: string) {
    if (typeof sessionStorage !== 'undefined') sessionStorage.setItem('livenote-worker-token', token.trim())
  },
  pairDevice(code: string, label: string) {
    return requestJson<{ deviceId: string; userId: string; token: string }>('/auth/pair', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code, label }) })
  },
  getCurrentDevice() {
    return requestJson<{ device: { id: string; userId: string }; user: { id: string; displayName: string; status: string } }>('/auth/me')
  },
  checkHealth() {
    return requestJson<HealthResponse>('/health')
  },

  createSession(session: SessionRecord, signal?: AbortSignal) {
    const payload = { ...session, startedAt: Math.round(session.startedAt), endedAt: session.endedAt === null ? null : Math.round(session.endedAt), durationMs: Math.round(session.durationMs), createdAt: Math.round(session.createdAt), updatedAt: Math.round(session.updatedAt) }
    return requestJson<{ id: string }>(`/sessions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), signal })
  },
  createSegment(segment: SegmentRecord, signal?: AbortSignal) {
    const payload = { ...segment, index: Math.round(segment.index), startedAt: Math.round(segment.startedAt), startElapsedMs: Math.round(segment.startElapsedMs ?? 0), endedAt: segment.endedAt === null ? null : Math.round(segment.endedAt), durationMs: Math.round(segment.durationMs) }
    return requestJson<{ id: string }>(`/sessions/${encodeURIComponent(segment.sessionId)}/segments`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), signal })
  },
  uploadChunk(chunk: ChunkRecord, signal?: AbortSignal) {
    return requestJson<{ ok: boolean; already_exists: boolean; verified: boolean }>(
      `/sessions/${encodeURIComponent(chunk.sessionId)}/segments/${encodeURIComponent(chunk.segmentId)}/chunks/${chunk.index}`,
      {
        method: 'PUT',
        headers: {
          'Content-Type': chunk.mimeType || 'application/octet-stream',
          'X-Chunk-SHA256': chunk.sha256,
          'X-Chunk-Size': String(chunk.size),
          'X-Chunk-Elapsed-Ms': String(Math.round(chunk.elapsedMs)),
        },
        body: chunk.blob,
        signal,
      },
    )
  },
  completeSegment(sessionId: string, segmentId: string, expectedChunkCount?: number, signal?: AbortSignal) {
    return requestJson<{ ok: boolean }>(`/sessions/${encodeURIComponent(sessionId)}/segments/${encodeURIComponent(segmentId)}/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expectedChunkCount }),
      signal,
    })
  },
  completeSession(sessionId: string, expectedChunkCount?: number, signal?: AbortSignal) {
    return requestJson<{ ok: boolean }>(`/sessions/${encodeURIComponent(sessionId)}/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expectedChunkCount }),
      signal,
    })
  },
  uploadMarker(marker: MarkerRecord, signal?: AbortSignal) {
    const payload = { ...marker, elapsedMs: Math.round(marker.elapsedMs), wallClockMs: Math.round(marker.wallClockMs), createdAt: Math.round(marker.createdAt) }
    return requestJson<{ ok: boolean; already_exists: boolean }>(`/sessions/${encodeURIComponent(marker.sessionId)}/markers`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), signal })
  },
  getUploadState(sessionId: string) {
    return requestJson<UploadStateResponse>(`/sessions/${encodeURIComponent(sessionId)}/upload-state`)
  },
  listSessions(query = '', offset = 0, limit = 100) {
    const params = new URLSearchParams({ query, offset: String(Math.max(0, offset)), limit: String(Math.min(100, Math.max(1, limit))) })
    return requestJson<{ items: Array<{ id: string; title: string; startedAt: number; endedAt: number | null; status: string; durationMs: number; createdAt: number; updatedAt: number; segmentCount: number; chunkCount: number; taskStatus: string | null; resultVersion: number; hasPublishedResult: boolean; liveProcessing: LiveProcessingStatus | null }>; total: number; offset: number; limit: number }>(`/sessions?${params.toString()}`)
  },
  getLiveProcessing(sessionId: string) {
    return requestJson<{ sessionId: string; liveProcessing: LiveProcessingStatus | null }>(`/sessions/${encodeURIComponent(sessionId)}/live-processing`)
  },
  getLiveTranscript(sessionId: string) {
    return requestJson<{ sessionId: string; status: LiveProcessingStatus | null; transcript: TranscriptResponse }>(`/sessions/${encodeURIComponent(sessionId)}/live-transcript`)
  },
  retryLiveProcessing(sessionId: string) {
    return requestJson<{ sessionId: string; liveProcessing: LiveProcessingStatus | null }>(`/sessions/${encodeURIComponent(sessionId)}/live-processing/retry`, { method: 'POST' })
  },
  deleteSession(sessionId: string) {
    return requestJson<{ ok: boolean; sessionId: string; segments: number; chunks: number; jobs: number }>(`/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
  },
  listTasks(status = 'ALL', limit = 100) {
    return requestJson<{ tasks: ProcessingTask[] }>(`/tasks?status=${encodeURIComponent(status)}&limit=${Math.min(200, Math.max(1, limit))}`)
  },
  claimTask(taskId: string, workerId: string) {
    return requestJson<{ task: ProcessingTask }>(`/tasks/${encodeURIComponent(taskId)}/claim`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workerId }),
    })
  },
  updateTaskStatus(taskId: string, status: ProcessingTaskStatus, workerId?: string, errorMessage = '') {
    return requestJson<{ task: ProcessingTask }>(`/tasks/${encodeURIComponent(taskId)}/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, workerId, errorMessage }),
    })
  },
  workerListReady(workerToken: string) {
    return requestWorkerJson<{ tasks: ProcessingTask[] }>('/tasks?status=READY&limit=20', workerToken)
  },
  workerClaimTask(taskId: string, workerId: string, workerToken: string) {
    return requestWorkerJson<{ task: ProcessingTask; leaseToken: string }>(`/tasks/${encodeURIComponent(taskId)}/claim`, workerToken, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ workerId }),
    })
  },
  workerDownloadTaskAudio(taskId: string, workerId: string, leaseToken: string, workerToken: string) {
    return requestWorkerBlob(`/tasks/${encodeURIComponent(taskId)}/audio`, workerToken, {
      headers: { 'X-Worker-Id': workerId, 'X-Task-Lease': leaseToken },
    })
  },
  workerUpdateTaskStatus(taskId: string, status: ProcessingTaskStatus, workerId: string, leaseToken: string, workerToken: string, errorMessage = '') {
    return requestWorkerJson<{ task: ProcessingTask }>(`/tasks/${encodeURIComponent(taskId)}/status`, workerToken, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Worker-Id': workerId, 'X-Task-Lease': leaseToken },
      body: JSON.stringify({ status, workerId, leaseToken, errorMessage }),
    })
  },
  adminListUsers(query = '', offset = 0, limit = 50) {
    return requestJson<{ items: AdminUser[]; total: number; offset: number; limit: number }>(`/admin/users?query=${encodeURIComponent(query)}&offset=${offset}&limit=${limit}`)
  },
  adminCreateUser(displayName: string) {
    return requestJson<{ id: string; displayName: string; status: string }>('/admin/users', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ displayName }) })
  },
  adminCreatePairingCode(userId: string) {
    return requestJson<{ code: string; expiresAt: number }>(`/admin/users/${encodeURIComponent(userId)}/pairing-codes`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) })
  },
  adminListTasks(status = 'ALL', offset = 0, limit = 50) {
    return requestJson<{ items: ProcessingTask[]; total: number; offset: number; limit: number }>(`/admin/tasks?status=${encodeURIComponent(status)}&offset=${offset}&limit=${limit}`)
  },
  adminUpdateTaskNote(taskId: string, note: string) {
    return requestJson<{ ok: boolean; taskId: string; adminNote: string }>(`/admin/tasks/${encodeURIComponent(taskId)}/note`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note }) })
  },
  adminListSessions(query = '', status = '', offset = 0, limit = 50, ownerId = '', startedFrom = '', startedTo = '') {
    const params = new URLSearchParams({ ownerId, query, status, offset: String(offset), limit: String(limit) })
    if (startedFrom) params.set('startedFrom', startedFrom)
    if (startedTo) params.set('startedTo', startedTo)
    return requestJson<{ items: Array<Record<string, unknown>>; total: number; offset: number; limit: number }>(`/admin/sessions?${params.toString()}`)
  },
  adminAssignSessionsOwner(ownerId: string, onlyUnassigned = true) {
    return requestJson<{ ok: boolean; ownerId: string; updatedCount: number }>('/admin/sessions/assign-owner', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ownerId, onlyUnassigned }) })
  },
  adminGetSession(sessionId: string) {
    return requestJson<{ session: Record<string, unknown>; segments: Array<Record<string, unknown>>; task: ProcessingTask | null; revisions: Array<Record<string, unknown>> }>(`/admin/sessions/${encodeURIComponent(sessionId)}`)
  },
  adminUpdateSession(sessionId: string, patch: { title?: string; ownerId?: string }) {
    return requestJson<{ ok: boolean; sessionId: string }>(`/admin/sessions/${encodeURIComponent(sessionId)}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) })
  },
  adminInterruptSession(sessionId: string) {
    return requestJson<{ ok: boolean; sessionId: string; status: string; durationMs: number }>(`/admin/sessions/${encodeURIComponent(sessionId)}/interrupt`, { method: 'POST' })
  },
  adminDeleteSession(sessionId: string) {
    return requestJson<{ ok: boolean; sessionId: string; segments: number; chunks: number; jobs: number }>(`/admin/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
  },
  adminRequestPull(taskId: string, workerId: string) {
    return requestJson<{ task: ProcessingTask }>(`/admin/tasks/${encodeURIComponent(taskId)}/request-pull`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workerId }) })
  },
  adminLocalWorker() {
    return requestJson<{ enabled: boolean; workerId: string; inbox: string }>('/admin/local-worker')
  },
  adminPullTaskLocal(taskId: string, workerId: string) {
    return requestJson<{ task: ProcessingTask; mode: 'local'; localPath: string; size?: number; reused?: boolean }>(`/admin/tasks/${encodeURIComponent(taskId)}/pull-local`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workerId }) })
  },
  adminStartProcessing(taskId: string, workerId: string) {
    return requestJson<{ task: ProcessingTask }>(`/admin/tasks/${encodeURIComponent(taskId)}/start-processing`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workerId }) })
  },
  adminAutoProcessTask(taskId: string, workerId: string) {
    return requestJson<{ task: ProcessingTask; job?: { id: string; status: string; stage: string; message: string } | null; deferred?: boolean }>(`/admin/tasks/${encodeURIComponent(taskId)}/auto-process`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workerId }) })
  },
  adminUploadTaskResult(taskId: string, payload: { version?: number; result: Record<string, unknown> }) {
    return requestJson<{ ok: boolean; taskId: string; sessionId: string; revisionId: string; version: number; status: string }>(`/admin/tasks/${encodeURIComponent(taskId)}/result`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  },
  adminUploadLocalTaskResult(taskId: string) {
    return requestJson<{ ok: boolean; taskId: string; sessionId: string; revisionId: string; version: number; status: string }>(`/admin/tasks/${encodeURIComponent(taskId)}/result-local`, { method: 'POST' })
  },
  adminReleaseTask(taskId: string) {
    return requestJson<{ ok: boolean; taskId: string; status: string }>(`/admin/tasks/${encodeURIComponent(taskId)}/release`, { method: 'POST' })
  },
  adminRetryTask(taskId: string) {
    return requestJson<{ ok: boolean; taskId: string; status: string }>(`/admin/tasks/${encodeURIComponent(taskId)}/retry`, { method: 'POST' })
  },
  adminTaskResults(taskId: string) {
    return requestJson<{ taskId: string; sessionId: string; status: ProcessingTaskStatus; items: AdminResultRevision[] }>(`/admin/tasks/${encodeURIComponent(taskId)}/results`)
  },
  adminGetTaskTranscript(taskId: string) {
    return requestJson<AdminTranscriptResponse>(`/admin/tasks/${encodeURIComponent(taskId)}/transcript`)
  },
  adminDownloadTaskAudio(taskId: string) {
    return requestAdminBlob(`/admin/tasks/${encodeURIComponent(taskId)}/audio`)
  },
  adminPublish(taskId: string, revisionId: string) {
    return requestJson<{ ok: boolean; status: string }>(`/admin/tasks/${encodeURIComponent(taskId)}/publish`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ revisionId }) })
  },
  getSessionResult(sessionId: string) {
    return requestJson<ServerResultResponse>(`/sessions/${encodeURIComponent(sessionId)}/result`)
  },
  async downloadSessionAudio(sessionId: string, diagnostics?: PlaybackDiagnosticsSink): Promise<Blob> {
    const deviceToken = typeof localStorage !== 'undefined' ? localStorage.getItem('livenote-device-token') || '' : ''
    const startedAt = typeof performance !== 'undefined' ? performance.now() : Date.now()
    diagnostics?.preparation('server-audio-request-start', { sessionId })
    try {
      const response = await fetchWithTimeout(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/audio`, {
        headers: {
          Accept: 'audio/webm',
          ...(API_KEY ? { 'X-API-Key': API_KEY } : {}),
          ...(deviceToken ? { Authorization: `Bearer ${deviceToken}` } : {}),
        },
      })
      diagnostics?.preparation('server-audio-response', { status: response.status, ok: response.ok })
      if (!response.ok) {
        const detail = await response.text().catch(() => '')
        diagnostics?.preparation('server-audio-failure', { category: 'http', status: response.status })
        throw new Error(`服务器整场音频重建失败 (${response.status})${detail ? `：${detail}` : ''}`)
      }
      const blob = await response.blob()
      diagnostics?.preparation('server-audio-download-complete', { bytes: blob.size, durationMs: (typeof performance !== 'undefined' ? performance.now() : Date.now()) - startedAt })
      return blob
    } catch (error) {
      if (error instanceof Error && !error.message.includes('服务器整场音频重建失败')) diagnostics?.preparation('server-audio-failure', { category: error.name === 'AbortError' ? 'timeout' : 'network' })
      throw error
    }
  },
  async downloadSegmentAudio(sessionId: string, segmentId: string): Promise<Blob> {
    const deviceToken = typeof localStorage !== 'undefined' ? localStorage.getItem('livenote-device-token') || '' : ''
    const response = await fetchWithTimeout(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/segments/${encodeURIComponent(segmentId)}/audio`, {
      headers: {
        Accept: 'audio/webm',
        ...(API_KEY ? { 'X-API-Key': API_KEY } : {}),
        ...(deviceToken ? { Authorization: `Bearer ${deviceToken}` } : {}),
      },
    })
    if (!response.ok) {
      const detail = await response.text().catch(() => '')
      throw new Error(`服务器 Segment 音频重建失败 (${response.status})${detail ? `：${detail}` : ''}`)
    }
    return response.blob()
  },
  uploadDiagnostic(formData: FormData) {
    return requestJson<DiagnosticUploadResponse>('/diagnostics', {
      method: 'POST',
      body: formData,
    })
  },
  uploadDiagnosticSnapshot(snapshot: Record<string, unknown>, description: string, attachment?: File | null) {
    const formData = new FormData()
    formData.append('description', description)
    formData.append('snapshot', new Blob([JSON.stringify(snapshot)], { type: 'application/json' }), 'snapshot.json')
    if (attachment) formData.append('attachment', attachment, attachment.name)
    return this.uploadDiagnostic(formData)
  },
}

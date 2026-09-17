import type { ChunkRecord, MarkerRecord, SegmentRecord, SessionRecord } from '../storage/types'

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
  ffmpeg: boolean
  ffprobe: boolean
  asrModel: string
  asrModelCached: boolean
  llmConfigured: boolean
}

export interface HealthResponse {
  ok: boolean
  service: string
  storageSchema?: number
  capabilities?: ServerCapabilities
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

export interface ProcessingJob {
  id: string
  sessionId: string
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | string
  stage: 'QUEUED' | 'RECONSTRUCTING' | 'ASR' | 'REPORT' | 'DONE' | 'FAILED' | string
  message: string
  model: string
  language: string
  transcriptSegments?: number
  markerCount?: number
  reportStatus?: string
  error?: string
  createdAt?: number
  updatedAt?: number
}

export interface DiagnosticUploadResponse {
  ok: boolean
  diagnosticId: string
  files: string[]
}

async function fetchWithTimeout(url: string, init: RequestInit = {}): Promise<Response> {
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), API_TIMEOUT_MS)
  try {
    return await fetch(url, { ...init, signal: controller.signal })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error(`服务器请求超时（${Math.round(API_TIMEOUT_MS / 1000)} 秒）；录音仍会继续保存在本机。`)
    }
    throw error
  } finally {
    window.clearTimeout(timer)
  }
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    ...init,
    headers: { Accept: 'application/json', ...(API_KEY ? { 'X-API-Key': API_KEY } : {}), ...(init.headers ?? {}) },
  })
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new ApiRequestError(response.status, `服务器请求失败 (${response.status})${detail ? `：${detail}` : ''}`)
  }
  return response.json() as Promise<T>
}

export const ApiClient = {
  checkHealth() {
    return requestJson<HealthResponse>('/health')
  },

  createSession(session: SessionRecord) {
    const payload = { ...session, startedAt: Math.round(session.startedAt), endedAt: session.endedAt === null ? null : Math.round(session.endedAt), durationMs: Math.round(session.durationMs), createdAt: Math.round(session.createdAt), updatedAt: Math.round(session.updatedAt) }
    return requestJson<{ id: string }>(`/sessions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  },
  createSegment(segment: SegmentRecord) {
    const payload = { ...segment, index: Math.round(segment.index), startedAt: Math.round(segment.startedAt), startElapsedMs: Math.round(segment.startElapsedMs ?? 0), endedAt: segment.endedAt === null ? null : Math.round(segment.endedAt), durationMs: Math.round(segment.durationMs) }
    return requestJson<{ id: string }>(`/sessions/${encodeURIComponent(segment.sessionId)}/segments`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  },
  uploadChunk(chunk: ChunkRecord) {
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
      },
    )
  },
  completeSegment(sessionId: string, segmentId: string, expectedChunkCount?: number) {
    return requestJson<{ ok: boolean }>(`/sessions/${encodeURIComponent(sessionId)}/segments/${encodeURIComponent(segmentId)}/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expectedChunkCount }),
    })
  },
  completeSession(sessionId: string, expectedChunkCount?: number) {
    return requestJson<{ ok: boolean }>(`/sessions/${encodeURIComponent(sessionId)}/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expectedChunkCount }),
    })
  },
  uploadMarker(marker: MarkerRecord) {
    const payload = { ...marker, elapsedMs: Math.round(marker.elapsedMs), wallClockMs: Math.round(marker.wallClockMs), createdAt: Math.round(marker.createdAt) }
    return requestJson<{ ok: boolean; already_exists: boolean }>(`/sessions/${encodeURIComponent(marker.sessionId)}/markers`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  },
  getUploadState(sessionId: string) {
    return requestJson<UploadStateResponse>(`/sessions/${encodeURIComponent(sessionId)}/upload-state`)
  },
  deleteSession(sessionId: string) {
    return requestJson<{ ok: boolean; sessionId: string; segments: number; chunks: number; jobs: number }>(`/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
  },
  transcribeSession(sessionId: string, model = 'medium', language = 'zh') {
    return requestJson<TranscriptResponse>(`/sessions/${encodeURIComponent(sessionId)}/transcribe`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, language }),
    })
  },
  createReport(sessionId: string) {
    return requestJson<ReportResponse>(`/sessions/${encodeURIComponent(sessionId)}/report`, { method: 'POST' })
  },
  startProcessing(sessionId: string, model = 'medium', language = 'zh') {
    return requestJson<ProcessingJob>(`/sessions/${encodeURIComponent(sessionId)}/process`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, language }),
    })
  },
  getProcessingJob(jobId: string) {
    return requestJson<ProcessingJob>(`/jobs/${encodeURIComponent(jobId)}`)
  },
  getLatestProcessingJob(sessionId: string) {
    return requestJson<ProcessingJob>(`/sessions/${encodeURIComponent(sessionId)}/processing`)
  },
  getReport(sessionId: string) {
    return requestJson<ReportResponse>(`/sessions/${encodeURIComponent(sessionId)}/report`)
  },
  async downloadSummaryImage(sessionId: string): Promise<Blob> {
    const response = await fetchWithTimeout(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/summary.svg`, {
      headers: { Accept: 'image/svg+xml', ...(API_KEY ? { 'X-API-Key': API_KEY } : {}) },
    })
    if (!response.ok) {
      const detail = await response.text().catch(() => '')
      throw new Error(`总结图片生成失败 (${response.status})${detail ? `：${detail}` : ''}`)
    }
    return response.blob()
  },
  async downloadSessionAudio(sessionId: string): Promise<Blob> {
    const response = await fetchWithTimeout(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/audio`, {
      headers: { Accept: 'audio/webm', ...(API_KEY ? { 'X-API-Key': API_KEY } : {}) },
    })
    if (!response.ok) {
      const detail = await response.text().catch(() => '')
      throw new Error(`服务器整场音频重建失败 (${response.status})${detail ? `：${detail}` : ''}`)
    }
    return response.blob()
  },
  async downloadSegmentAudio(sessionId: string, segmentId: string): Promise<Blob> {
    const response = await fetchWithTimeout(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/segments/${encodeURIComponent(segmentId)}/audio`, {
      headers: { Accept: 'audio/webm', ...(API_KEY ? { 'X-API-Key': API_KEY } : {}) },
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
}

import type { ChunkRecord, MarkerRecord, SegmentRecord, SessionRecord } from '../storage/types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined) || '/api/v1'

export interface UploadStateResponse {
  sessionId: string
  segments: Array<{
    segmentId: string
    chunks: Array<{ index: number; size: number; sha256: string }>
  }>
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { Accept: 'application/json', ...(init.headers ?? {}) },
  })
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new Error(`服务器请求失败 (${response.status})${detail ? `：${detail}` : ''}`)
  }
  return response.json() as Promise<T>
}

export const ApiClient = {
  createSession(session: SessionRecord) {
    const payload = { ...session, startedAt: Math.round(session.startedAt), endedAt: session.endedAt === null ? null : Math.round(session.endedAt), durationMs: Math.round(session.durationMs), createdAt: Math.round(session.createdAt), updatedAt: Math.round(session.updatedAt) }
    return requestJson<{ id: string }>(`/sessions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  },
  createSegment(segment: SegmentRecord) {
    const payload = { ...segment, index: Math.round(segment.index), startedAt: Math.round(segment.startedAt), endedAt: segment.endedAt === null ? null : Math.round(segment.endedAt), durationMs: Math.round(segment.durationMs) }
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
  completeSegment(sessionId: string, segmentId: string) {
    return requestJson<{ ok: boolean }>(`/sessions/${encodeURIComponent(sessionId)}/segments/${encodeURIComponent(segmentId)}/complete`, { method: 'POST' })
  },
  completeSession(sessionId: string) {
    return requestJson<{ ok: boolean }>(`/sessions/${encodeURIComponent(sessionId)}/complete`, { method: 'POST' })
  },
  uploadMarker(marker: MarkerRecord) {
    const payload = { ...marker, elapsedMs: Math.round(marker.elapsedMs), wallClockMs: Math.round(marker.wallClockMs), createdAt: Math.round(marker.createdAt) }
    return requestJson<{ ok: boolean; already_exists: boolean }>(`/sessions/${encodeURIComponent(marker.sessionId)}/markers`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
  },
  getUploadState(sessionId: string) {
    return requestJson<UploadStateResponse>(`/sessions/${encodeURIComponent(sessionId)}/upload-state`)
  },
}

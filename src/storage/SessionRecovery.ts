import { ChunkStore } from './ChunkStore'
import { SegmentStore } from './SegmentStore'
import { SessionStore } from './SessionStore'

export interface RestoredSession {
  blob: Blob
  segmentCount: number
  chunkCount: number
  totalBytes: number
  durationMs: number
  mimeType: string
  hasGaps: boolean
}

export interface SessionPlayback extends RestoredSession {
  url: string
  playbackMode: 'media-source' | 'blob-fallback'
}

interface SessionData {
  chunks: Awaited<ReturnType<typeof ChunkStore.listBySegmentId>>
  segmentCount: number
  mimeType: string
  hasGaps: boolean
  totalBytes: number
  durationMs: number
}

async function loadSessionData(sessionId: string): Promise<SessionData> {
  const [session, segments] = await Promise.all([
    SessionStore.get(sessionId),
    SegmentStore.listBySessionId(sessionId),
  ])

  if (!session) throw new Error(`Session 不存在：${sessionId}`)

  const segmentChunks = await Promise.all(segments.map(async (segment) => ({
    segment,
    chunks: await ChunkStore.listBySegmentId(segment.id),
  })))
  const orderedChunks = segmentChunks.flatMap(({ chunks }) => chunks)
  const mimeType = segments.find((segment) => segment.mimeType)?.mimeType
    || orderedChunks[0]?.mimeType
    || 'audio/webm'
  const hasGaps = segmentChunks.some(({ chunks }) => chunks.some((chunk, index) => chunk.index !== index))
  const totalBytes = orderedChunks.reduce((total, chunk) => total + chunk.size, 0)
  const chunkDurationMs = orderedChunks.reduce((max, chunk) => Math.max(max, chunk.elapsedMs), 0)
  const durationMs = Math.max(session.durationMs, chunkDurationMs)

  return { chunks: orderedChunks, segmentCount: segments.length, mimeType, hasGaps, totalBytes, durationMs }
}

function appendBuffer(sourceBuffer: SourceBuffer, buffer: ArrayBuffer): Promise<void> {
  return new Promise((resolve, reject) => {
    const onUpdateEnd = () => { cleanup(); resolve() }
    const onError = () => { cleanup(); reject(new Error('MediaSource 无法解析某个 Session 音频分片')) }
    const cleanup = () => {
      sourceBuffer.removeEventListener('updateend', onUpdateEnd)
      sourceBuffer.removeEventListener('error', onError)
      sourceBuffer.removeEventListener('abort', onError)
    }

    sourceBuffer.addEventListener('updateend', onUpdateEnd, { once: true })
    sourceBuffer.addEventListener('error', onError, { once: true })
    sourceBuffer.addEventListener('abort', onError, { once: true })

    try { sourceBuffer.appendBuffer(buffer) } catch (error) { cleanup(); reject(error) }
  })
}

export async function restoreSession(sessionId: string): Promise<RestoredSession> {
  const data = await loadSessionData(sessionId)
  const blob = new Blob(data.chunks.map((chunk) => chunk.blob), { type: data.mimeType })
  return {
    blob,
    segmentCount: data.segmentCount,
    chunkCount: data.chunks.length,
    totalBytes: data.totalBytes,
    durationMs: data.durationMs,
    mimeType: data.mimeType,
    hasGaps: data.hasGaps,
  }
}

export async function createSessionPlayback(sessionId: string, onUrlReady?: (url: string) => void): Promise<SessionPlayback> {
  const data = await loadSessionData(sessionId)
  const base = {
    segmentCount: data.segmentCount,
    chunkCount: data.chunks.length,
    totalBytes: data.totalBytes,
    durationMs: data.durationMs,
    mimeType: data.mimeType,
    hasGaps: data.hasGaps,
  }

  if (data.chunks.length && typeof MediaSource !== 'undefined' && MediaSource.isTypeSupported(data.mimeType)) {
    const mediaSource = new MediaSource()
    const url = URL.createObjectURL(mediaSource)
    onUrlReady?.(url)
    let mediaDurationMs = data.durationMs

    try {
      await new Promise<void>((resolve, reject) => {
        let timedOut = false
        const timeoutId = window.setTimeout(() => {
          timedOut = true
          reject(new Error('整场播放器初始化超时，请重新点击验证。'))
        }, 15_000)
        const open = async () => {
          if (timedOut) return
          try {
            const sourceBuffer = mediaSource.addSourceBuffer(data.mimeType)
            try { sourceBuffer.mode = 'sequence' } catch { /* Browser default is acceptable. */ }
            for (const chunk of data.chunks) await appendBuffer(sourceBuffer, await chunk.blob.arrayBuffer())

            if (mediaSource.readyState === 'open') {
              const bufferedEnd = sourceBuffer.buffered.length > 0
                ? sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1)
                : 0
              const durationSeconds = Math.max(data.durationMs / 1000, bufferedEnd)
              if (durationSeconds > 0) {
                mediaSource.duration = durationSeconds
                mediaDurationMs = durationSeconds * 1000
              }
            }
            if (mediaSource.readyState === 'open') mediaSource.endOfStream()
            resolve()
          } catch (error) { reject(error) }
          finally { window.clearTimeout(timeoutId) }
        }

        if (mediaSource.readyState === 'open') void open()
        else mediaSource.addEventListener('sourceopen', () => void open(), { once: true })
      })

      return { ...base, durationMs: mediaDurationMs, blob: new Blob(), url, playbackMode: 'media-source' }
    } catch { URL.revokeObjectURL(url) }
  }

  const fallback = await restoreSession(sessionId)
  const fallbackUrl = URL.createObjectURL(fallback.blob)
  onUrlReady?.(fallbackUrl)
  return { ...fallback, url: fallbackUrl, playbackMode: 'blob-fallback' }
}

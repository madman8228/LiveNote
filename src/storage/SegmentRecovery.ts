import { ChunkStore } from './ChunkStore'
import { SegmentStore } from './SegmentStore'

export interface RestoredSegment {
  blob: Blob
  chunkCount: number
  totalBytes: number
  mimeType: string
  indexes: number[]
  hasGaps: boolean
}

export interface SegmentPlayback extends RestoredSegment {
  url: string
  playbackMode: 'media-source' | 'blob-fallback'
}

interface SegmentData {
  chunks: Awaited<ReturnType<typeof ChunkStore.listBySegmentId>>
  mimeType: string
  indexes: number[]
  hasGaps: boolean
  totalBytes: number
}

async function loadSegmentData(segmentId: string): Promise<SegmentData> {
  const [segment, chunks] = await Promise.all([
    SegmentStore.get(segmentId),
    ChunkStore.listBySegmentId(segmentId),
  ])

  if (!segment) throw new Error(`Segment 不存在：${segmentId}`)

  const orderedChunks = [...chunks].sort((a, b) => a.index - b.index)
  const indexes = orderedChunks.map((chunk) => chunk.index)
  const hasGaps = indexes.some((index, position) => index !== position)
  const totalBytes = orderedChunks.reduce((total, chunk) => total + chunk.size, 0)
  const mimeType = segment.mimeType || orderedChunks[0]?.mimeType || 'audio/webm'

  return { chunks: orderedChunks, mimeType, indexes, hasGaps, totalBytes }
}

export async function restoreSegment(segmentId: string): Promise<RestoredSegment> {
  const data = await loadSegmentData(segmentId)
  const blob = new Blob(data.chunks.map((chunk) => chunk.blob), { type: data.mimeType })

  return {
    blob,
    chunkCount: data.chunks.length,
    totalBytes: data.totalBytes,
    mimeType: data.mimeType,
    indexes: data.indexes,
    hasGaps: data.hasGaps,
  }
}

function appendBuffer(sourceBuffer: SourceBuffer, buffer: ArrayBuffer): Promise<void> {
  return new Promise((resolve, reject) => {
    const onUpdateEnd = () => {
      cleanup()
      resolve()
    }
    const onError = () => {
      cleanup()
      reject(new Error('MediaSource 无法解析某个音频分片'))
    }
    const cleanup = () => {
      sourceBuffer.removeEventListener('updateend', onUpdateEnd)
      sourceBuffer.removeEventListener('error', onError)
      sourceBuffer.removeEventListener('abort', onError)
    }

    sourceBuffer.addEventListener('updateend', onUpdateEnd, { once: true })
    sourceBuffer.addEventListener('error', onError, { once: true })
    sourceBuffer.addEventListener('abort', onError, { once: true })

    try {
      sourceBuffer.appendBuffer(buffer)
    } catch (error) {
      cleanup()
      reject(error)
    }
  })
}

/**
 * Create a playback URL by appending the stored MediaRecorder fragments in
 * order. Directly concatenating WebM fragments can produce a broken timeline
 * in Chrome, so MediaSource is preferred for validation playback.
 */
export async function createSegmentPlayback(segmentId: string, onUrlReady?: (url: string) => void): Promise<SegmentPlayback> {
  const data = await loadSegmentData(segmentId)
  const base = {
    chunkCount: data.chunks.length,
    totalBytes: data.totalBytes,
    mimeType: data.mimeType,
    indexes: data.indexes,
    hasGaps: data.hasGaps,
  }

  if (typeof MediaSource !== 'undefined' && MediaSource.isTypeSupported(data.mimeType)) {
    const mediaSource = new MediaSource()
    const url = URL.createObjectURL(mediaSource)
    onUrlReady?.(url)

    try {
      await new Promise<void>((resolve, reject) => {
        let timedOut = false
        const timeoutId = window.setTimeout(() => {
          timedOut = true
          reject(new Error('播放器初始化超时，请重新点击验证。'))
        }, 10_000)
        const open = async () => {
          if (timedOut) return
          try {
            const sourceBuffer = mediaSource.addSourceBuffer(data.mimeType)
            try {
              sourceBuffer.mode = 'sequence'
            } catch {
              // Some browsers expose only timestamp-based WebM buffering.
            }

            for (const chunk of data.chunks) {
              await appendBuffer(sourceBuffer, await chunk.blob.arrayBuffer())
            }

            if (mediaSource.readyState === 'open') mediaSource.endOfStream()
            resolve()
          } catch (error) {
            reject(error)
          } finally {
            window.clearTimeout(timeoutId)
          }
        }

        if (mediaSource.readyState === 'open') void open()
        else mediaSource.addEventListener('sourceopen', () => void open(), { once: true })
      })

      return { ...base, blob: new Blob(), url, playbackMode: 'media-source' }
    } catch {
      URL.revokeObjectURL(url)
    }
  }

  const fallback = await restoreSegment(segmentId)
  const fallbackUrl = URL.createObjectURL(fallback.blob)
  onUrlReady?.(fallbackUrl)

  return {
    ...fallback,
    url: fallbackUrl,
    playbackMode: 'blob-fallback',
  }
}

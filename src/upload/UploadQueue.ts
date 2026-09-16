import { ChunkStore } from '../storage/ChunkStore'
import { MarkerStore } from '../storage/MarkerStore'
import { SegmentStore } from '../storage/SegmentStore'
import { SessionStore } from '../storage/SessionStore'
import { sha256Blob } from '../storage/sha256'
import type { ChunkRecord, MarkerRecord } from '../storage/types'
import { ApiClient } from './ApiClient'
import { canRetryNow } from './RetryPolicy'

export interface UploadQueueSnapshot {
  serverOnline: boolean | null
  isUploading: boolean
  total: number
  uploaded: number
  pending: number
  failed: number
  lastError: string
}

export type UploadQueueListener = (snapshot: UploadQueueSnapshot) => void

const INITIAL_SNAPSHOT: UploadQueueSnapshot = {
  serverOnline: null,
  isUploading: false,
  total: 0,
  uploaded: 0,
  pending: 0,
  failed: 0,
  lastError: '',
}

export class UploadQueue {
  private running = false
  private started = false
  private retryTimer: number | null = null
  private snapshot: UploadQueueSnapshot = { ...INITIAL_SNAPSHOT }
  private readonly listeners = new Set<UploadQueueListener>()

  subscribe(listener: UploadQueueListener): () => void {
    this.listeners.add(listener)
    listener(this.snapshot)
    return () => this.listeners.delete(listener)
  }

  start(): void {
    if (this.started) return
    this.started = true
    window.addEventListener('online', this.handleOnline)
    this.retryTimer = window.setInterval(() => { void this.flush() }, 5_000)
    void this.reconcileAll()
    void this.flush()
  }

  stop(): void {
    this.started = false
    window.removeEventListener('online', this.handleOnline)
    if (this.retryTimer !== null) window.clearInterval(this.retryTimer)
    this.retryTimer = null
  }

  kick(): void {
    if (this.started) void this.flush()
  }

  async flush(): Promise<void> {
    if (this.running) return
    this.running = true
    this.updateSnapshot({ isUploading: true })
    try {
      const chunks = await ChunkStore.listPendingOrFailed()
      for (const chunk of chunks) {
        if (chunk.uploadStatus === 'FAILED' && !canRetryNow(chunk.retryCount, chunk.lastUploadAttemptAt)) continue
        try {
          await this.uploadChunk(chunk)
        } catch {
          break
        }
      }
      const markers = await MarkerStore.listPendingOrFailed()
      for (const marker of markers) {
        if (marker.uploadStatus === 'FAILED' && !canRetryNow(marker.retryCount, marker.lastUploadAttemptAt)) continue
        try {
          await this.uploadMarker(marker)
        } catch {
          break
        }
      }
    } finally {
      this.running = false
      await this.refreshSnapshot()
    }
  }

  async reconcileAll(): Promise<void> {
    const sessions = await SessionStore.list()
    for (const session of sessions) {
      try {
        await this.reconcileSession(session.id)
      } catch {
        // The queue will retry when the server is reachable.
      }
    }
    await this.refreshSnapshot()
  }

  async reconcileSession(sessionId: string): Promise<void> {
    const state = await ApiClient.getUploadState(sessionId)
    const serverChunks = new Map<string, { size: number; sha256: string }>()
    for (const segment of state.segments) {
      for (const chunk of segment.chunks) serverChunks.set(`${segment.segmentId}:${chunk.index}`, { size: chunk.size, sha256: chunk.sha256 })
    }

    const localChunks = (await ChunkStore.listBySessionId(sessionId))
    for (const local of localChunks) {
      const server = serverChunks.get(`${local.segmentId}:${local.index}`)
      if (!server) {
        if (local.uploadStatus === 'UPLOADED') await ChunkStore.put({ ...local, uploadStatus: 'PENDING' })
      } else if (server.sha256 === local.sha256 && server.size === local.size) {
        if (local.uploadStatus !== 'UPLOADED') await ChunkStore.put({ ...local, uploadStatus: 'UPLOADED', uploadedAt: local.uploadedAt ?? Date.now() })
      } else {
        await ChunkStore.put({ ...local, uploadStatus: 'FAILED', lastUploadAttemptAt: Date.now() })
        this.updateSnapshot({ lastError: `服务器 Chunk 校验不一致：${local.segmentId} #${local.index}` })
      }
    }
    this.updateSnapshot({ serverOnline: true })
  }

  async completeSegment(sessionId: string, segmentId: string): Promise<void> {
    try {
      await ApiClient.completeSegment(sessionId, segmentId)
      this.updateSnapshot({ serverOnline: true, lastError: '' })
    } catch (error) {
      this.setError(error)
    }
  }

  async completeSession(sessionId: string): Promise<void> {
    try {
      await ApiClient.completeSession(sessionId)
      this.updateSnapshot({ serverOnline: true, lastError: '' })
    } catch (error) {
      this.setError(error)
    }
  }

  async refreshSnapshot(): Promise<void> {
    const chunks = await ChunkStore.listAll()
    this.updateSnapshot({
      total: chunks.length,
      uploaded: chunks.filter((chunk) => chunk.uploadStatus === 'UPLOADED').length,
      pending: chunks.filter((chunk) => chunk.uploadStatus === 'PENDING' || chunk.uploadStatus === 'UPLOADING').length,
      failed: chunks.filter((chunk) => chunk.uploadStatus === 'FAILED').length,
      isUploading: this.running,
    })
  }

  async snapshotForSession(sessionId: string): Promise<Pick<UploadQueueSnapshot, 'total' | 'uploaded' | 'pending' | 'failed'>> {
    const chunks = await ChunkStore.listBySessionId(sessionId)
    return {
      total: chunks.length,
      uploaded: chunks.filter((chunk) => chunk.uploadStatus === 'UPLOADED').length,
      pending: chunks.filter((chunk) => chunk.uploadStatus === 'PENDING' || chunk.uploadStatus === 'UPLOADING').length,
      failed: chunks.filter((chunk) => chunk.uploadStatus === 'FAILED').length,
    }
  }

  private async uploadChunk(input: ChunkRecord): Promise<void> {
    const session = await SessionStore.get(input.sessionId)
    const segment = await SegmentStore.get(input.segmentId)
    if (!session || !segment) throw new Error('本地 Session 或 Segment 不存在。')

    let chunk = input
    if (!chunk.sha256) {
      chunk = { ...chunk, sha256: await sha256Blob(chunk.blob) }
      await ChunkStore.put(chunk)
    }
    const attemptAt = Date.now()
    chunk = { ...chunk, uploadStatus: 'UPLOADING', lastUploadAttemptAt: attemptAt }
    await ChunkStore.put(chunk)
    try {
      await ApiClient.createSession(session)
      await ApiClient.createSegment(segment)
      await ApiClient.uploadChunk(chunk)
      await ChunkStore.put({ ...chunk, uploadStatus: 'UPLOADED', uploadedAt: Date.now() })
      this.updateSnapshot({ serverOnline: true, lastError: '' })
    } catch (error) {
      await ChunkStore.put({ ...chunk, uploadStatus: 'FAILED', retryCount: chunk.retryCount + 1, lastUploadAttemptAt: attemptAt })
      this.setError(error)
      throw error
    }
  }

  private async uploadMarker(input: MarkerRecord): Promise<void> {
    const attemptAt = Date.now()
    const marker = { ...input, uploadStatus: 'UPLOADING' as const, lastUploadAttemptAt: attemptAt }
    await MarkerStore.put(marker)
    try {
      const session = await SessionStore.get(marker.sessionId)
      if (!session) throw new Error('Marker 所属 Session 不存在。')
      await ApiClient.createSession(session)
      await ApiClient.uploadMarker(marker)
      await MarkerStore.put({ ...marker, uploadStatus: 'UPLOADED', uploadedAt: Date.now() })
      this.updateSnapshot({ serverOnline: true, lastError: '' })
    } catch (error) {
      await MarkerStore.put({ ...marker, uploadStatus: 'FAILED', retryCount: marker.retryCount + 1, lastUploadAttemptAt: attemptAt })
      this.setError(error)
      throw error
    }
  }

  private readonly handleOnline = (): void => { void this.reconcileAll(); void this.flush() }

  private setError(error: unknown): void {
    this.updateSnapshot({ serverOnline: false, lastError: error instanceof Error ? error.message : '上传失败。' })
  }

  private updateSnapshot(patch: Partial<UploadQueueSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch }
    for (const listener of this.listeners) listener(this.snapshot)
  }
}

export const uploadQueue = new UploadQueue()

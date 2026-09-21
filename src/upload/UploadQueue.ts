import { ChunkStore } from '../storage/ChunkStore'
import { MarkerStore } from '../storage/MarkerStore'
import { SegmentStore } from '../storage/SegmentStore'
import { SessionStore } from '../storage/SessionStore'
import { sha256Blob } from '../storage/sha256'
import type { ChunkRecord, MarkerRecord } from '../storage/types'
import { ApiClient, ApiRequestError, isCompatibleServerHealth, type ServerCapabilities } from './ApiClient'
import { canRetryNow } from './RetryPolicy'

export interface UploadQueueSnapshot {
  serverOnline: boolean | null
  serverCompatible: boolean | null
  serverCapabilities: ServerCapabilities | null
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
  serverCompatible: null,
  serverCapabilities: null,
  isUploading: false,
  total: 0,
  uploaded: 0,
  pending: 0,
  failed: 0,
  lastError: '',
}

function isTransientNetworkError(error: unknown): boolean {
  if (error instanceof TypeError) return true
  if (!(error instanceof Error)) return false
  return /failed to fetch|networkerror|load failed|fetch failed|请求超时|服务器请求失败 \((408|429|5\d\d)\)/i.test(error.message)
}

export class UploadQueue {
  private running = false
  private flushPromise: Promise<void> | null = null
  private started = false
  private readonly pausedSessionIds = new Set<string>()
  private readonly sessionAbortControllers = new Map<string, AbortController>()
  private readonly activeSessionOperations = new Map<string, Promise<void>>()
  private retryTimer: number | null = null
  private healthTimer: number | null = null
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
    this.updateSnapshot({
      serverOnline: navigator.onLine ? this.snapshot.serverOnline : false,
      serverCompatible: navigator.onLine ? this.snapshot.serverCompatible : null,
      serverCapabilities: navigator.onLine ? this.snapshot.serverCapabilities : null,
      lastError: navigator.onLine ? this.snapshot.lastError : '网络已断开，上传暂停；录音继续本地保存。',
    })
    window.addEventListener('online', this.handleOnline)
    window.addEventListener('offline', this.handleOffline)
    this.retryTimer = window.setInterval(() => {
      if (!navigator.onLine) return
      if (this.snapshot.serverOnline !== true) void this.reconcileAll().then(() => this.flush())
      else void this.flush()
    }, 5_000)
    this.healthTimer = window.setInterval(() => {
      if (navigator.onLine) void this.refreshServerHealth()
    }, 30_000)
    if (navigator.onLine) {
      void this.reconcileAll().then(() => this.flush())
    }
  }

  stop(): void {
    this.started = false
    window.removeEventListener('online', this.handleOnline)
    window.removeEventListener('offline', this.handleOffline)
    if (this.retryTimer !== null) window.clearInterval(this.retryTimer)
    this.retryTimer = null
    if (this.healthTimer !== null) window.clearInterval(this.healthTimer)
    this.healthTimer = null
  }

  async pauseSessionAndWait(sessionId: string): Promise<void> {
    this.pausedSessionIds.add(sessionId)
    this.sessionAbortControllers.get(sessionId)?.abort()
    const activeOperation = this.activeSessionOperations.get(sessionId)
    if (activeOperation) {
      try {
        await activeOperation
      } catch {
        // The delete flow owns cleanup; a failed queue attempt must not block it.
      }
    }
  }

  resumeSession(sessionId: string): void {
    this.pausedSessionIds.delete(sessionId)
    this.sessionAbortControllers.delete(sessionId)
  }

  private sessionSignal(sessionId: string): AbortSignal {
    let controller = this.sessionAbortControllers.get(sessionId)
    if (!controller || controller.signal.aborted) {
      controller = new AbortController()
      this.sessionAbortControllers.set(sessionId, controller)
    }
    return controller.signal
  }

  private async trackSessionOperation(sessionId: string, operation: () => Promise<void>): Promise<void> {
    const previous = this.activeSessionOperations.get(sessionId) ?? Promise.resolve()
    const current = previous.then(operation)
    this.activeSessionOperations.set(sessionId, current)
    try {
      await current
    } finally {
      if (this.activeSessionOperations.get(sessionId) === current) this.activeSessionOperations.delete(sessionId)
    }
  }

  kick(): void {
    if (this.started) void this.flush()
  }

  async flush(): Promise<void> {
    if (this.flushPromise) return this.flushPromise
    if (!navigator.onLine) {
      this.updateSnapshot({ serverOnline: false, serverCompatible: null, serverCapabilities: null, lastError: '网络已断开，上传暂停；录音继续本地保存。' })
      await this.refreshSnapshot()
      return
    }
    // A reachable but incompatible API must be treated as a hard pause. The
    // old API can answer health checks successfully while rejecting the
    // current session/segment contract; retrying every local Chunk against it
    // only creates FAILED noise and can obscure the real restart requirement.
    if (this.snapshot.serverCompatible === null) await this.refreshServerHealth()
    if (this.snapshot.serverOnline === false || this.snapshot.serverCompatible === false) {
      await this.refreshSnapshot()
      return
    }
    this.running = true
    const promise = (async () => {
        this.updateSnapshot({ isUploading: true })
        try {
          const chunkMetadata = await ChunkStore.listPendingOrFailedMetadata()
          for (const metadata of chunkMetadata) {
            if (!this.started) break
            if (this.pausedSessionIds.has(metadata.sessionId)) continue
          if (metadata.uploadStatus === 'FAILED' && !canRetryNow(metadata.retryCount, metadata.lastUploadAttemptAt)) continue
          // Keep only one audio Blob in memory while recovering a large
          // offline queue. The metadata query above is intentionally Blob-free.
          const chunk = await ChunkStore.get(metadata.id)
          if (!chunk || !['PENDING', 'FAILED'].includes(chunk.uploadStatus)) continue
          if (chunk.uploadStatus === 'FAILED' && !canRetryNow(chunk.retryCount, chunk.lastUploadAttemptAt)) continue
          try {
            await this.trackSessionOperation(metadata.sessionId, () => this.uploadChunk(chunk))
          } catch {
            break
          }
          }
          const markers = await MarkerStore.listPendingOrFailed()
          for (const marker of markers) {
            if (!this.started) break
            if (this.pausedSessionIds.has(marker.sessionId)) continue
          if (marker.uploadStatus === 'FAILED' && !canRetryNow(marker.retryCount, marker.lastUploadAttemptAt)) continue
          try {
            await this.trackSessionOperation(marker.sessionId, () => this.uploadMarker(marker))
          } catch {
            break
          }
        }
        await this.finalizeCompletedSessions()
      } finally {
        this.running = false
        await this.refreshSnapshot()
      }
    })()
    this.flushPromise = promise
    try {
      await promise
    } finally {
      if (this.flushPromise === promise) this.flushPromise = null
    }
  }

  async reconcileAll(): Promise<void> {
    if (!navigator.onLine) {
      this.updateSnapshot({ serverOnline: false, serverCompatible: null, serverCapabilities: null, lastError: '网络已断开，上传暂停；录音继续本地保存。' })
      return
    }
    try {
      const health = await ApiClient.checkHealth()
      const compatible = isCompatibleServerHealth(health)
      this.updateSnapshot({ serverOnline: true, serverCompatible: compatible, serverCapabilities: health.capabilities ?? null, lastError: compatible ? '' : '服务器 API 版本过旧，请重启 8000 服务后再进行处理。' })
      if (!compatible) {
        await this.refreshSnapshot()
        return
      }
      await this.recoverInFlightUploads()
      const sessions = await SessionStore.list()
      for (const session of sessions) {
        if (this.pausedSessionIds.has(session.id)) continue
        try {
          await this.reconcileSession(session.id)
        } catch {
          // The queue will retry when the server is reachable.
        }
      }
    } catch (error) {
      this.setError(error)
    }
    await this.refreshSnapshot()
  }

  private async refreshServerHealth(): Promise<void> {
    try {
      const health = await ApiClient.checkHealth()
      const compatible = isCompatibleServerHealth(health)
      this.updateSnapshot({ serverOnline: true, serverCompatible: compatible, serverCapabilities: health.capabilities ?? null, lastError: compatible ? '' : '服务器 API 版本过旧，请重启 8000 服务后再进行处理。' })
    } catch (error) {
      this.setError(error)
    }
  }

  async reconcileSession(sessionId: string): Promise<void> {
    if (this.pausedSessionIds.has(sessionId)) return
    const state = await ApiClient.getUploadState(sessionId)
    const serverChunks = new Map<string, { size: number; sha256: string }>()
    for (const segment of state.segments) {
      for (const chunk of segment.chunks) serverChunks.set(`${segment.segmentId}:${chunk.index}`, { size: chunk.size, sha256: chunk.sha256 })
    }

    const localChunks = (await ChunkStore.listMetadataBySessionId(sessionId))
    for (const local of localChunks) {
      const server = serverChunks.get(`${local.segmentId}:${local.index}`)
      if (!server) {
        if (local.uploadStatus === 'UPLOADED' || local.uploadStatus === 'UPLOADING') await ChunkStore.updateUploadState(local.id, { uploadStatus: 'PENDING' })
      } else if (server.sha256 === local.sha256 && server.size === local.size) {
        if (local.uploadStatus !== 'UPLOADED') await ChunkStore.updateUploadState(local.id, { uploadStatus: 'UPLOADED', uploadedAt: local.uploadedAt ?? Date.now() })
      } else {
        await ChunkStore.updateUploadState(local.id, { uploadStatus: 'FAILED', lastUploadAttemptAt: Date.now() })
        this.updateSnapshot({ lastError: `服务器 Chunk 校验不一致：${local.segmentId} #${local.index}` })
      }
    }
    this.updateSnapshot({ serverOnline: true })
  }

  async completeSegment(sessionId: string, segmentId: string): Promise<void> {
    try {
      if (this.pausedSessionIds.has(sessionId)) return
      await this.flush()
      const chunks = await ChunkStore.listMetadataBySegmentId(segmentId)
      if (chunks.some((chunk) => chunk.uploadStatus !== 'UPLOADED')) throw new Error(`Segment 仍有未上传 Chunk：${segmentId}`)
      await ApiClient.completeSegment(sessionId, segmentId, chunks.length, this.sessionSignal(sessionId))
      this.updateSnapshot({ serverOnline: true, lastError: '' })
    } catch (error) {
      this.setError(error)
    }
  }

  async completeSession(sessionId: string): Promise<void> {
    try {
      if (this.pausedSessionIds.has(sessionId)) return
      await this.flush()
      const chunks = await ChunkStore.listMetadataBySessionId(sessionId)
      if (chunks.some((chunk) => chunk.uploadStatus !== 'UPLOADED')) throw new Error(`Session 仍有未上传 Chunk：${sessionId}`)
      await ApiClient.completeSession(sessionId, chunks.length, this.sessionSignal(sessionId))
      this.updateSnapshot({ serverOnline: true, lastError: '' })
    } catch (error) {
      this.setError(error)
    }
  }

  async refreshSnapshot(): Promise<void> {
    const [total, uploaded, pending, uploading, failed] = await Promise.all([
      ChunkStore.countAll(),
      ChunkStore.countByUploadStatus('UPLOADED'),
      ChunkStore.countByUploadStatus('PENDING'),
      ChunkStore.countByUploadStatus('UPLOADING'),
      ChunkStore.countByUploadStatus('FAILED'),
    ])
    this.updateSnapshot({
      total,
      uploaded,
      pending: pending + uploading,
      failed,
      isUploading: this.running,
    })
  }

  async snapshotForSession(sessionId: string): Promise<Pick<UploadQueueSnapshot, 'total' | 'uploaded' | 'pending' | 'failed'>> {
    const [total, uploaded, pending, uploading, failed] = await Promise.all([
      ChunkStore.countBySessionId(sessionId),
      ChunkStore.countBySessionAndUploadStatus(sessionId, 'UPLOADED'),
      ChunkStore.countBySessionAndUploadStatus(sessionId, 'PENDING'),
      ChunkStore.countBySessionAndUploadStatus(sessionId, 'UPLOADING'),
      ChunkStore.countBySessionAndUploadStatus(sessionId, 'FAILED'),
    ])
    return {
      total,
      uploaded,
      pending: pending + uploading,
      failed,
    }
  }

  private async uploadChunk(input: ChunkRecord): Promise<void> {
    if (this.pausedSessionIds.has(input.sessionId)) return
    const session = await SessionStore.get(input.sessionId)
    const segment = await SegmentStore.get(input.segmentId)
    if (!session || !segment) throw new Error('本地 Session 或 Segment 不存在。')
    const signal = this.sessionSignal(input.sessionId)

    let chunk = input
    if (!chunk.sha256) {
      chunk = { ...chunk, sha256: await sha256Blob(chunk.blob) }
      await ChunkStore.put(chunk)
    }
    const attemptAt = Date.now()
    chunk = { ...chunk, uploadStatus: 'UPLOADING', lastUploadAttemptAt: attemptAt }
    await ChunkStore.put(chunk)
    try {
      await ApiClient.createSession(session, signal)
      await ApiClient.createSegment(segment, signal)
      await ApiClient.uploadChunk(chunk, signal)
      await ChunkStore.put({ ...chunk, uploadStatus: 'UPLOADED', uploadedAt: Date.now() })
      this.updateSnapshot({ serverOnline: true, lastError: '' })
    } catch (error) {
      const transient = isTransientNetworkError(error)
      await ChunkStore.put({
        ...chunk,
        uploadStatus: transient ? 'PENDING' : 'FAILED',
        retryCount: transient ? chunk.retryCount : chunk.retryCount + 1,
        lastUploadAttemptAt: transient ? null : attemptAt,
      })
      this.setError(error)
      throw error
    }
  }

  private async uploadMarker(input: MarkerRecord): Promise<void> {
    if (this.pausedSessionIds.has(input.sessionId)) return
    const attemptAt = Date.now()
    const marker = { ...input, uploadStatus: 'UPLOADING' as const, lastUploadAttemptAt: attemptAt }
    const signal = this.sessionSignal(marker.sessionId)
    await MarkerStore.put(marker)
    try {
      const session = await SessionStore.get(marker.sessionId)
      if (!session) throw new Error('Marker 所属 Session 不存在。')
      await ApiClient.createSession(session, signal)
      await ApiClient.uploadMarker(marker, signal)
      await MarkerStore.put({ ...marker, uploadStatus: 'UPLOADED', uploadedAt: Date.now() })
      this.updateSnapshot({ serverOnline: true, lastError: '' })
    } catch (error) {
      const transient = isTransientNetworkError(error)
      await MarkerStore.put({
        ...marker,
        uploadStatus: transient ? 'PENDING' : 'FAILED',
        retryCount: transient ? marker.retryCount : marker.retryCount + 1,
        lastUploadAttemptAt: transient ? null : attemptAt,
      })
      this.setError(error)
      throw error
    }
  }

  private readonly handleOnline = (): void => {
    void this.reconcileAll().then(() => this.flush())
  }

  private readonly handleOffline = (): void => {
    this.updateSnapshot({ serverOnline: false, serverCompatible: null, serverCapabilities: null, lastError: '网络已断开，上传暂停；录音继续本地保存。' })
  }

  private async finalizeCompletedSessions(): Promise<void> {
    const sessions = (await SessionStore.list()).filter((session) => session.status === 'COMPLETED')
    for (const session of sessions) {
      if (this.pausedSessionIds.has(session.id)) continue
      try {
        const signal = this.sessionSignal(session.id)
        const [segments, chunks] = await Promise.all([
          SegmentStore.listBySessionId(session.id),
          ChunkStore.listMetadataBySessionId(session.id),
        ])
        if (chunks.some((chunk) => chunk.uploadStatus !== 'UPLOADED')) continue

        await ApiClient.createSession(session, signal)
        for (const segment of segments) {
          const segmentChunks = chunks.filter((chunk) => chunk.segmentId === segment.id)
          await ApiClient.createSegment(segment, signal)
          if (segment.status === 'COMPLETED') await ApiClient.completeSegment(session.id, segment.id, segmentChunks.length, signal)
        }
        await ApiClient.completeSession(session.id, chunks.length, signal)
        this.updateSnapshot({ serverOnline: true, lastError: '' })
      } catch (error) {
        this.setError(error)
      }
    }
  }

  private async recoverInFlightUploads(): Promise<void> {
    const [chunks, markers] = await Promise.all([ChunkStore.listUploading(), MarkerStore.listUploading()])
    await Promise.all([
      ...chunks.map((chunk) => ChunkStore.put({ ...chunk, uploadStatus: 'PENDING' })),
      ...markers.map((marker) => MarkerStore.put({ ...marker, uploadStatus: 'PENDING' })),
    ])
  }

  private setError(error: unknown): void {
    // Receiving an HTTP response proves that the API is reachable. Only a
    // network/timeout error should flip the server to offline; 4xx responses
    // are application errors (for example an incomplete upload) and should
    // not block the next queue retry or mislead the user about connectivity.
    const isReachableApiError = error instanceof ApiRequestError
    const isNetworkError = !isReachableApiError && (!navigator.onLine || isTransientNetworkError(error))
    this.updateSnapshot({
      serverOnline: isNetworkError ? false : true,
      ...(isNetworkError ? { serverCompatible: null, serverCapabilities: null } : {}),
      lastError: !navigator.onLine
        ? '网络已断开，上传暂停；录音继续本地保存。'
        : error instanceof Error ? error.message : '上传失败。',
    })
  }

  private updateSnapshot(patch: Partial<UploadQueueSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch }
    for (const listener of this.listeners) listener(this.snapshot)
  }
}

export const uploadQueue = new UploadQueue()

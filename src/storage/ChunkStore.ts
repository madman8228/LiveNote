import { countByIndex, countRecords, deleteRecord, getAllByIndex, getAllByIndexCursor, getAllRecords, getRecord, putRecord, STORE_NAMES } from './db'
import type { ChunkMetadata, ChunkRecord, UploadStatus } from './types'

export const ChunkStore = {
  listBySessionId(sessionId: string) {
    return getAllByIndex<ChunkRecord>(STORE_NAMES.chunks, 'sessionId', sessionId).then((chunks) => (
      chunks.map(normalizeChunk).sort((a, b) => a.createdAt - b.createdAt || a.index - b.index)
    ))
  },
  listBySegmentId(segmentId: string) {
    return getAllByIndex<ChunkRecord>(STORE_NAMES.chunks, 'segmentId', segmentId).then((chunks) => (
      chunks.map(normalizeChunk).sort((a, b) => a.index - b.index)
    ))
  },
  listMetadataBySessionId(sessionId: string): Promise<ChunkMetadata[]> {
    return getAllByIndex<ChunkRecord>(STORE_NAMES.chunks, 'sessionId', sessionId).then((chunks) => (
      chunks.map(toMetadata).sort((a, b) => a.createdAt - b.createdAt || a.index - b.index)
    ))
  },
  listMetadataBySegmentId(segmentId: string): Promise<ChunkMetadata[]> {
    return getAllByIndex<ChunkRecord>(STORE_NAMES.chunks, 'segmentId', segmentId).then((chunks) => (
      chunks.map(toMetadata).sort((a, b) => a.index - b.index)
    ))
  },
  countAll() {
    return countRecords(STORE_NAMES.chunks)
  },
  countBySessionId(sessionId: string) {
    return countByIndex(STORE_NAMES.chunks, 'sessionId', sessionId)
  },
  countByUploadStatus(status: UploadStatus) {
    return countByIndex(STORE_NAMES.chunks, 'uploadStatus', status)
  },
  countBySessionAndUploadStatus(sessionId: string, status: UploadStatus) {
    return countByIndex(STORE_NAMES.chunks, 'sessionUploadStatus', [sessionId, status])
  },
  listAll() {
    return getAllRecords<ChunkRecord>(STORE_NAMES.chunks).then((chunks) => chunks.map(normalizeChunk))
  },
  get(id: string) {
    return getRecord<ChunkRecord>(STORE_NAMES.chunks, id).then((chunk) => chunk ? normalizeChunk(chunk) : undefined)
  },
  listPendingOrFailed() {
    return Promise.all([
      getAllByIndex<ChunkRecord>(STORE_NAMES.chunks, 'uploadStatus', 'PENDING'),
      getAllByIndex<ChunkRecord>(STORE_NAMES.chunks, 'uploadStatus', 'FAILED'),
    ]).then(([pending, failed]) => [...pending, ...failed]
      .map(normalizeChunk)
      .sort((a, b) => a.sessionId.localeCompare(b.sessionId) || a.segmentId.localeCompare(b.segmentId) || a.index - b.index))
  },
  listPendingOrFailedMetadata(): Promise<ChunkMetadata[]> {
    return Promise.all([
      getAllByIndexCursor<ChunkRecord, ChunkMetadata>(STORE_NAMES.chunks, 'uploadStatus', 'PENDING', toMetadata),
      getAllByIndexCursor<ChunkRecord, ChunkMetadata>(STORE_NAMES.chunks, 'uploadStatus', 'FAILED', toMetadata),
    ]).then(([pending, failed]) => [...pending, ...failed]
      .sort((a, b) => a.sessionId.localeCompare(b.sessionId) || a.segmentId.localeCompare(b.segmentId) || a.index - b.index))
  },
  listUploading() {
    return getAllByIndex<ChunkRecord>(STORE_NAMES.chunks, 'uploadStatus', 'UPLOADING').then((chunks) => chunks.map(normalizeChunk))
  },
  put(chunk: ChunkRecord) {
    return putRecord(STORE_NAMES.chunks, chunk)
  },
  delete(id: string) {
    return deleteRecord(STORE_NAMES.chunks, id)
  },
  async updateUploadState(id: string, patch: Partial<Pick<ChunkRecord, 'uploadStatus' | 'lastUploadAttemptAt' | 'uploadedAt'>>) {
    const chunk = await getRecord<ChunkRecord>(STORE_NAMES.chunks, id)
    if (chunk) await putRecord(STORE_NAMES.chunks, { ...normalizeChunk(chunk), ...patch })
  },
}

function normalizeChunk(chunk: ChunkRecord): ChunkRecord {
  return {
    ...chunk,
    sha256: chunk.sha256 ?? '',
    retryCount: chunk.retryCount ?? 0,
    lastUploadAttemptAt: chunk.lastUploadAttemptAt ?? null,
    uploadedAt: chunk.uploadedAt ?? null,
  }
}

function toMetadata(chunk: ChunkRecord): ChunkMetadata {
  const normalized = normalizeChunk(chunk)
  const { blob: _blob, ...metadata } = normalized
  return metadata
}

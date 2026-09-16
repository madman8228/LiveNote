import { getAllByIndex, getAllRecords, getRecord, putRecord, STORE_NAMES } from './db'
import type { ChunkRecord } from './types'

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
  listAll() {
    return getAllRecords<ChunkRecord>(STORE_NAMES.chunks).then((chunks) => chunks.map(normalizeChunk))
  },
  get(id: string) {
    return getRecord<ChunkRecord>(STORE_NAMES.chunks, id).then((chunk) => chunk ? normalizeChunk(chunk) : undefined)
  },
  listPendingOrFailed() {
    return getAllRecords<ChunkRecord>(STORE_NAMES.chunks).then((chunks) => chunks
      .map(normalizeChunk)
      .filter((chunk) => chunk.uploadStatus === 'PENDING' || chunk.uploadStatus === 'FAILED')
      .sort((a, b) => a.sessionId.localeCompare(b.sessionId) || a.segmentId.localeCompare(b.segmentId) || a.index - b.index))
  },
  put(chunk: ChunkRecord) {
    return putRecord(STORE_NAMES.chunks, chunk)
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

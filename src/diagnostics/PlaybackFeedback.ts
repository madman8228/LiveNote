import { ApiClient, type DiagnosticUploadResponse } from '../upload/ApiClient'
import type { PlaybackDiagnosticReport } from './PlaybackDiagnostics'

const DB_NAME = 'livenote-playback-diagnostics'
const DB_VERSION = 1
const STORE_NAME = 'reports'
const MAX_PENDING = 3
const TTL_MS = 24 * 60 * 60 * 1000

export interface PendingPlaybackFeedback {
  clientReportId: string
  ownerId: string | null
  report: PlaybackDiagnosticReport
  description: string
  createdAt: number
  updatedAt: number
  attempts: number
  nextAttemptAt: number
  status: 'pending' | 'error'
  lastError: string
}

export interface PlaybackFeedbackResult {
  status: 'sent' | 'pending' | 'error'
  diagnosticId?: string
  error?: string
}

export interface PlaybackFeedbackUploader {
  upload(snapshot: Record<string, unknown>, description: string): Promise<DiagnosticUploadResponse>
}

const defaultUploader: PlaybackFeedbackUploader = {
  upload: (snapshot, description) => ApiClient.uploadDiagnosticSnapshot(snapshot, description),
}

function request<T>(operation: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    operation.onsuccess = () => resolve(operation.result)
    operation.onerror = () => reject(operation.error ?? new Error('播放诊断本地保存失败。'))
  })
}

function openFeedbackDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const opening = indexedDB.open(DB_NAME, DB_VERSION)
    opening.onupgradeneeded = () => {
      const database = opening.result
      const store = database.objectStoreNames.contains(STORE_NAME)
        ? opening.transaction?.objectStore(STORE_NAME)
        : database.createObjectStore(STORE_NAME, { keyPath: 'clientReportId' })
      if (store && !store.indexNames.contains('ownerId')) store.createIndex('ownerId', 'ownerId', { unique: false })
      if (store && !store.indexNames.contains('updatedAt')) store.createIndex('updatedAt', 'updatedAt', { unique: false })
    }
    opening.onsuccess = () => resolve(opening.result)
    opening.onerror = () => reject(opening.error ?? new Error('无法打开播放诊断本地存储。'))
    opening.onblocked = () => reject(new Error('播放诊断本地存储正在被其他页面占用。'))
  })
}

async function putPending(record: PendingPlaybackFeedback): Promise<void> {
  const database = await openFeedbackDatabase()
  const transaction = database.transaction(STORE_NAME, 'readwrite')
  transaction.objectStore(STORE_NAME).put(record)
  await new Promise<void>((resolve, reject) => {
    transaction.oncomplete = () => resolve()
    transaction.onerror = () => reject(transaction.error ?? new Error('播放诊断本地保存失败。'))
    transaction.onabort = () => reject(transaction.error ?? new Error('播放诊断本地保存被中止。'))
  })
  database.close()
}

async function deletePending(clientReportId: string): Promise<void> {
  const database = await openFeedbackDatabase()
  const transaction = database.transaction(STORE_NAME, 'readwrite')
  transaction.objectStore(STORE_NAME).delete(clientReportId)
  await new Promise<void>((resolve, reject) => {
    transaction.oncomplete = () => resolve()
    transaction.onerror = () => reject(transaction.error ?? new Error('播放诊断清理失败。'))
    transaction.onabort = () => reject(transaction.error ?? new Error('播放诊断清理被中止。'))
  })
  database.close()
}

async function listPending(ownerId: string | null): Promise<PendingPlaybackFeedback[]> {
  const database = await openFeedbackDatabase()
  const transaction = database.transaction(STORE_NAME, 'readonly')
  const records = await request(transaction.objectStore(STORE_NAME).getAll())
  database.close()
  const now = Date.now()
  return records
    .filter((record) => record.ownerId === ownerId && now - record.updatedAt <= TTL_MS)
    .sort((a, b) => a.updatedAt - b.updatedAt)
}

async function prune(ownerId: string | null): Promise<void> {
  const records = await listPending(ownerId)
  for (const record of records.slice(0, Math.max(0, records.length - MAX_PENDING))) await deletePending(record.clientReportId)
}

function descriptionFor(report: PlaybackDiagnosticReport): string {
  return `playback clientReportId=${report.clientReportId} sessionId=${report.sessionId}`
}

async function tryUpload(record: PendingPlaybackFeedback, uploader: PlaybackFeedbackUploader): Promise<PlaybackFeedbackResult> {
  try {
    const response = await uploader.upload(record.report as unknown as Record<string, unknown>, record.description)
    await deletePending(record.clientReportId)
    return { status: 'sent', diagnosticId: response.diagnosticId }
  } catch (error) {
    const message = error instanceof Error ? error.message : '播放诊断上传失败。'
    const now = Date.now()
    const attempts = record.attempts + 1
    await putPending({ ...record, status: 'error', attempts, lastError: message, updatedAt: now, nextAttemptAt: now + Math.min(60 * 60 * 1000, 2 ** Math.min(attempts, 8) * 1000) })
    return { status: 'pending', error: message }
  }
}

export async function saveAndUploadPlaybackFeedback(
  report: PlaybackDiagnosticReport,
  ownerId: string | null,
  uploader: PlaybackFeedbackUploader = defaultUploader,
): Promise<PlaybackFeedbackResult> {
  const now = Date.now()
  const record: PendingPlaybackFeedback = {
    clientReportId: report.clientReportId,
    ownerId,
    report,
    description: descriptionFor(report),
    createdAt: now,
    updatedAt: now,
    attempts: 0,
    nextAttemptAt: now,
    status: 'pending',
    lastError: '',
  }
  try {
    await putPending(record)
    await prune(ownerId)
  } catch (error) {
    const result = await tryUpload(record, uploader)
    if (result.status === 'sent') return result
    return { status: 'error', error: error instanceof Error ? error.message : '播放诊断本地保存失败。' }
  }
  return tryUpload(record, uploader)
}

export async function flushPendingPlaybackFeedback(ownerId: string | null, uploader: PlaybackFeedbackUploader = defaultUploader): Promise<number> {
  if (!ownerId) return 0
  let sent = 0
  try {
    for (const record of await listPending(ownerId)) {
      if (record.nextAttemptAt > Date.now()) continue
      const result = await tryUpload(record, uploader)
      if (result.status === 'sent') sent += 1
    }
  } catch {
    return sent
  }
  return sent
}

export const PLAYBACK_FEEDBACK_LIMITS = { maxPending: MAX_PENDING, ttlMs: TTL_MS }

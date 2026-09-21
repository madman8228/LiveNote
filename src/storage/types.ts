export type SessionStatus = 'RECORDING' | 'PAUSED' | 'FINALIZING' | 'COMPLETED' | 'INTERRUPTED'
export type SegmentStatus = 'RECORDING' | 'COMPLETED' | 'INTERRUPTED'
export type UploadStatus = 'PENDING' | 'UPLOADING' | 'UPLOADED' | 'FAILED'

export interface SessionRecord {
  id: string
  title: string
  startedAt: number
  endedAt: number | null
  status: SessionStatus
  durationMs: number
  createdAt: number
  updatedAt: number
}

export interface SegmentRecord {
  id: string
  sessionId: string
  index: number
  startedAt: number
  startElapsedMs: number
  endedAt: number | null
  mimeType: string
  mediaSettings: Record<string, unknown>
  status: SegmentStatus
  durationMs: number
}

export interface ChunkRecord {
  id: string
  sessionId: string
  segmentId: string
  index: number
  blob: Blob
  size: number
  mimeType: string
  createdAt: number
  wallClockMs: number
  elapsedMs: number
  uploadStatus: UploadStatus
  sha256: string
  retryCount: number
  lastUploadAttemptAt: number | null
  uploadedAt: number | null
}

export type ChunkMetadata = Omit<ChunkRecord, 'blob'>

export type MarkerType = 'KEY_POINT' | 'QUESTION' | 'IDEA' | 'TODO'

export interface MarkerRecord {
  id: string
  sessionId: string
  type: MarkerType
  elapsedMs: number
  wallClockMs: number
  note: string
  createdAt: number
  uploadStatus: UploadStatus
  retryCount: number
  lastUploadAttemptAt: number | null
  uploadedAt: number | null
}

export type LifecycleEventType = 'visibilitychange' | 'pagehide' | 'pageshow' | 'freeze' | 'resume'

export interface LifecycleEventRecord {
  id: string
  sessionId: string
  eventType: LifecycleEventType
  wallClockMs: number
  elapsedMs: number
  visibilityState: DocumentVisibilityState
}

export interface PublishedResultRecord {
  sessionId: string
  ownerId: string | null
  revisionId: string | null
  version: number
  status: string
  updatedAt: number
  result: Record<string, unknown>
}

export interface ResultDraftRecord {
  sessionId: string
  ownerId: string | null
  baseRevisionId: string | null
  updatedAt: number
  summary: {
    title: string
    overview: string
    keyPoints: string[]
    knowledgeStructure: Array<{ title: string; points: string[] }>
    questions: Array<{ question: string; answer: string; startMs: number | null }>
    actionItems: string[]
    entities: string[]
    confidenceNotes: string[]
  }
}

import type { AudioProfileId } from './AudioProfile'

export type RecorderState = 'idle' | 'recording' | 'stopping' | 'ready' | 'error'

export interface RecordingSession {
  stream: MediaStream
  mimeType: string
  startedAt: number
  segmentStartedAt: number
}

export interface RecorderChunk {
  index: number
  blob: Blob
  mimeType: string
  wallClockMs: number
  elapsedMs: number
}

export interface RecordingResult {
  durationMs: number
  chunkCount: number
  totalBytes: number
}

export interface RecorderStartOptions {
  timesliceMs?: number
  sessionElapsedBaseMs?: number
  onChunk?: (chunk: RecorderChunk) => Promise<void> | void
  onChunkError?: (error: Error) => void
  onRecorderError?: (error: Error) => void
}

export interface RecorderEngine {
  readonly state: RecorderState
  start(profileId: AudioProfileId, options?: RecorderStartOptions): Promise<RecordingSession>
  stop(): Promise<RecordingResult>
  dispose(): void
}

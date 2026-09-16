import { getAudioProfile } from './AudioProfile'
import { preferredMimeType, type MediaRecorderConstructorLike } from './AudioCapabilities'
import type {
  RecorderEngine,
  RecorderState,
  RecorderChunk,
  RecorderStartOptions,
  RecordingResult,
  RecordingSession,
} from './RecorderEngine'
import type { AudioProfileId } from './AudioProfile'

export interface MediaRecorderEngineDependencies {
  getUserMedia: (constraints: MediaStreamConstraints) => Promise<MediaStream>
  MediaRecorder: MediaRecorderConstructorLike
}

function browserDependencies(): MediaRecorderEngineDependencies {
  return {
    getUserMedia: (constraints) => navigator.mediaDevices.getUserMedia(constraints),
    MediaRecorder: window.MediaRecorder,
  }
}

export class MediaRecorderEngine implements RecorderEngine {
  private _state: RecorderState = 'idle'
  private recorder: MediaRecorder | null = null
  private stream: MediaStream | null = null
  private startedAt = 0
  private sessionElapsedBaseMs = 0
  private selectedMimeType = ''
  private chunkIndex = 0
  private chunkCount = 0
  private totalBytes = 0
  private pendingChunkWrites: Promise<void> = Promise.resolve()
  private chunkError: Error | null = null
  private stopPromise: Promise<RecordingResult> | null = null
  private chunkHandler: ((chunk: RecorderChunk) => Promise<void> | void) | undefined
  private stopRequestedAt = 0

  constructor(private readonly dependencies: MediaRecorderEngineDependencies = browserDependencies()) {}

  get state(): RecorderState {
    return this._state
  }

  async start(profileId: AudioProfileId, options: RecorderStartOptions = {}): Promise<RecordingSession> {
    if (this._state === 'recording' || this._state === 'stopping') {
      throw new Error('已有录音正在进行。')
    }

    if (!this.dependencies.getUserMedia || !this.dependencies.MediaRecorder) {
      throw new Error('当前浏览器缺少录音所需能力。')
    }

    this.dispose()
    const profile = getAudioProfile(profileId)
    const stream = await this.dependencies.getUserMedia(profile.constraints)
    const requestedMimeType = preferredMimeType(this.dependencies.MediaRecorder)
    const recorderOptions: MediaRecorderOptions = {
      audioBitsPerSecond: 64_000,
    }

    if (requestedMimeType) {
      recorderOptions.mimeType = requestedMimeType
    }

    let recorder: MediaRecorder
    try {
      recorder = new this.dependencies.MediaRecorder(stream, recorderOptions)
    } catch (error) {
      if (!requestedMimeType) {
        stream.getTracks().forEach((track) => track.stop())
        throw error
      }

      // Some browsers report support but reject the constructor option at runtime.
      // Retry once without mimeType so the browser can choose its default format.
      try {
        recorder = new this.dependencies.MediaRecorder(stream, { audioBitsPerSecond: 64_000 })
      } catch (fallbackError) {
        stream.getTracks().forEach((track) => track.stop())
        throw fallbackError
      }
    }

    this.recorder = recorder
    this.stream = stream
    this.startedAt = performance.now()
    this.sessionElapsedBaseMs = options.sessionElapsedBaseMs ?? 0
    this.chunkIndex = 0
    this.chunkCount = 0
    this.totalBytes = 0
    this.pendingChunkWrites = Promise.resolve()
    this.chunkError = null
    this.stopRequestedAt = 0
    this.chunkHandler = options.onChunk
    this.selectedMimeType = recorder.mimeType || requestedMimeType || 'browser-default'
    this._state = 'recording'

    recorder.addEventListener('dataavailable', (event) => {
      if (event.data.size > 0) {
        const chunk: RecorderChunk = {
          index: this.chunkIndex++,
          blob: event.data,
          mimeType: event.data.type || this.selectedMimeType,
          wallClockMs: Date.now(),
          elapsedMs: this.sessionElapsedBaseMs + Math.max(0, performance.now() - this.startedAt),
        }
        this.chunkCount += 1
        this.totalBytes += event.data.size
        const task = async () => {
          try {
            await this.chunkHandler?.(chunk)
          } catch (error: unknown) {
            // Keep the queue alive so a transient failure for one chunk does
            // not silently discard every later chunk. Stop() reports the
            // first error after all already-emitted chunks finish processing.
            if (!this.chunkError) this.chunkError = error instanceof Error ? error : new Error('Chunk 保存失败。')
          }
        }
        this.pendingChunkWrites = this.pendingChunkWrites.then(task, task)
      }
    })

    recorder.addEventListener('error', () => {
      this._state = 'error'
    })

    recorder.start(options.timesliceMs ?? 30_000)

    return {
      stream,
      mimeType: this.selectedMimeType,
      startedAt: this.startedAt,
      segmentStartedAt: Date.now(),
    }
  }

  stop(): Promise<RecordingResult> {
    if (!this.recorder || this._state !== 'recording') {
      return Promise.reject(new Error('当前没有正在进行的录音。'))
    }

    const recorder = this.recorder

    if (this.stopPromise) {
      return this.stopPromise
    }

    this.stopRequestedAt = performance.now()
    this._state = 'stopping'
    this.stopPromise = new Promise<RecordingResult>((resolve, reject) => {
      recorder.addEventListener('stop', () => {
        void this.pendingChunkWrites.then(() => {
          if (this.chunkError) throw this.chunkError
          const durationMs = Math.max(0, this.stopRequestedAt - this.startedAt) + this.sessionElapsedBaseMs
          this._state = 'ready'
          resolve({ durationMs, chunkCount: this.chunkCount, totalBytes: this.totalBytes })
        }).catch((error: unknown) => {
          this._state = 'error'
          reject(error)
        })
      }, { once: true })
      recorder.addEventListener('error', () => {
        this._state = 'error'
        reject(new Error('录音停止时发生错误。'))
      }, { once: true })

      try {
        recorder.stop()
      } catch (error) {
        this._state = 'error'
        reject(error)
      }
    })

    return this.stopPromise
  }

  dispose(): void {
    if (this.recorder && this._state === 'recording') {
      try {
        this.recorder.stop()
      } catch {
        // The stream cleanup below is still safe if stop() is not accepted.
      }
    }

    this.stream?.getTracks().forEach((track) => track.stop())
    this.recorder = null
    this.stream = null
    this.stopPromise = null
    if (this._state !== 'error') {
      this._state = 'idle'
    }
  }
}

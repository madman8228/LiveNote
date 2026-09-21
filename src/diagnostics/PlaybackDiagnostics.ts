export interface PlaybackDiagnosticsClock {
  now(): number
  wallClock(): number
}

export interface PlaybackDiagnosticsSink {
  record(type: string, data?: Record<string, unknown>): void
  preparation(type: string, data?: Record<string, unknown>): void
  recordMediaSource(type: string, data?: Record<string, unknown>): void
}

export interface PlaybackDiagnosticEvent {
  seq: number
  atMs: number
  type: string
  attemptId: string
  elementId: string | null
  sourceId: number
  data: Record<string, unknown>
}

export interface PlaybackDiagnosticReport {
  schemaVersion: 1
  kind: 'playback-diagnostic'
  clientReportId: string
  createdAt: string
  buildId: string
  sessionId: string
  attemptId: string
  captureStart: number
  captureEnd: number
  truncatedReason: string | null
  userDescription: string
  platform: { userAgent: string; language: string; online: boolean; visibility: DocumentVisibilityState }
  session: { segmentCount: number; chunkCount: number; durationMs: number; mimeType: string; localUploadComplete: boolean }
  preparation: PlaybackDiagnosticEvent[]
  events: PlaybackDiagnosticEvent[]
  droppedEvents: number
}

export interface PlaybackDiagnosticsContext {
  sessionId: string
  segmentCount: number
  chunkCount: number
  durationMs: number
  mimeType: string
  localUploadComplete: boolean
}

const MAX_EVENTS = 600
const MAX_PREPARATION_EVENTS = 80
const MAX_BUFFER_MS = 60_000
const MAX_REPORT_BYTES = 256 * 1024

const defaultClock: PlaybackDiagnosticsClock = {
  now: () => (typeof performance !== 'undefined' ? performance.now() : Date.now()),
  wallClock: () => Date.now(),
}

function id(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return `playback-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function finite(value: number): number | null {
  return Number.isFinite(value) ? value : null
}

function rangeList(ranges: TimeRanges | undefined): { start: number | null; end: number | null }[] {
  if (!ranges) return []
  const result: { start: number | null; end: number | null }[] = []
  const limit = Math.min(8, ranges.length)
  for (let index = 0; index < limit; index += 1) {
    try { result.push({ start: finite(ranges.start(index)), end: finite(ranges.end(index)) }) } catch { break }
  }
  return result
}

function mediaState(audio: HTMLMediaElement): Record<string, unknown> {
  return {
    currentTime: finite(audio.currentTime),
    duration: finite(audio.duration),
    paused: audio.paused,
    seeking: audio.seeking,
    ended: audio.ended,
    playbackRate: finite(audio.playbackRate),
    readyState: audio.readyState,
    networkState: audio.networkState,
    errorCode: audio.error?.code ?? null,
    buffered: rangeList(audio.buffered),
    seekable: rangeList(audio.seekable),
  }
}

function safeData(data: Record<string, unknown> | undefined): Record<string, unknown> {
  if (!data) return {}
  const result: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(data)) {
    if (typeof value === 'string' || typeof value === 'boolean' || value === null) result[key] = value
    else if (typeof value === 'number') result[key] = finite(value)
    else if (Array.isArray(value)) result[key] = value.slice(0, 8)
    else if (typeof value === 'object') result[key] = value
  }
  return result
}

export class PlaybackDiagnostics implements PlaybackDiagnosticsSink {
  readonly clientReportId = id()
  readonly attemptId = id()
  private readonly clock: PlaybackDiagnosticsClock
  private readonly events: PlaybackDiagnosticEvent[] = []
  private readonly preparationEvents: PlaybackDiagnosticEvent[] = []
  private readonly startedAt: number
  private readonly captureStart: number
  private sequence = 0
  private sourceId = 0
  private elementId: string | null = null
  private lastSampleAt = -Infinity
  private lastMseProgressAt = -Infinity
  private droppedEvents = 0
  private attachedAudio: HTMLMediaElement | null = null
  private cleanupMedia: (() => void) | null = null
  private readonly context: PlaybackDiagnosticsContext
  private readonly buildId: string

  constructor(context: PlaybackDiagnosticsContext, buildId: string, clock: PlaybackDiagnosticsClock = defaultClock) {
    this.context = context
    this.buildId = buildId
    this.clock = clock
    this.startedAt = clock.now()
    this.captureStart = clock.wallClock()
    this.record('attempt-start', { sessionId: context.sessionId })
  }

  record(type: string, data?: Record<string, unknown>): void {
    const now = this.clock.now()
    if (type === 'timeupdate' || type === 'media-sample') {
      if (now - this.lastSampleAt < 500) return
      this.lastSampleAt = now
    }
    const event: PlaybackDiagnosticEvent = {
      seq: this.sequence++,
      atMs: Math.max(0, now - this.startedAt),
      type,
      attemptId: this.attemptId,
      elementId: this.elementId,
      sourceId: this.sourceId,
      data: safeData(data),
    }
    if (this.events.length >= MAX_EVENTS) {
      this.events.shift()
      this.droppedEvents += 1
    }
    this.events.push(event)
    while (this.events.length > 1 && event.atMs - this.events[0].atMs > MAX_BUFFER_MS) {
      this.events.shift()
      this.droppedEvents += 1
    }
  }

  preparation(type: string, data?: Record<string, unknown>): void {
    const event: PlaybackDiagnosticEvent = {
      seq: this.sequence++,
      atMs: Math.max(0, this.clock.now() - this.startedAt),
      type,
      attemptId: this.attemptId,
      elementId: this.elementId,
      sourceId: this.sourceId,
      data: safeData(data),
    }
    if (this.preparationEvents.length >= MAX_PREPARATION_EVENTS) this.preparationEvents.shift()
    this.preparationEvents.push(event)
  }

  nextSourceId(): number {
    this.sourceId += 1
    return this.sourceId
  }

  attachMediaElement(audio: HTMLMediaElement | null): void {
    if (this.attachedAudio === audio) return
    this.cleanupMedia?.()
    this.cleanupMedia = null
    if (this.attachedAudio) this.record('audio-unmount')
    this.attachedAudio = audio
    this.elementId = audio ? id() : null
    if (!audio) return
    this.record('audio-mount')
    const eventNames = ['loadstart', 'loadedmetadata', 'durationchange', 'loadeddata', 'canplay', 'play', 'playing', 'pause', 'waiting', 'stalled', 'seeking', 'seeked', 'ratechange', 'ended', 'emptied', 'abort', 'error']
    const handlers = new Map<string, EventListener>()
    for (const name of eventNames) {
      const handler: EventListener = () => {
        const data = mediaState(audio)
        if (name === 'seeking' || name === 'seeked') data.seekReason = 'unknown'
        this.record(name, data)
      }
      handlers.set(name, handler)
      audio.addEventListener(name, handler)
    }
    const sample = () => this.record('media-sample', mediaState(audio))
    audio.addEventListener('timeupdate', sample)
    const pointer = (event: Event) => this.record(`pointer-${event.type}`, { trusted: event.isTrusted })
    const key = (event: KeyboardEvent) => this.record('keydown', { keyCategory: event.key === ' ' || event.key === 'Enter' ? 'playback-control' : 'other' })
    audio.addEventListener('pointerdown', pointer)
    audio.addEventListener('pointerup', pointer)
    audio.addEventListener('pointercancel', pointer)
    audio.addEventListener('keydown', key)
    this.cleanupMedia = () => {
      for (const [name, handler] of handlers) audio.removeEventListener(name, handler)
      audio.removeEventListener('timeupdate', sample)
      audio.removeEventListener('pointerdown', pointer)
      audio.removeEventListener('pointerup', pointer)
      audio.removeEventListener('pointercancel', pointer)
      audio.removeEventListener('keydown', key)
    }
  }

  recordMediaSource(type: string, data?: Record<string, unknown>): void {
    if (type === 'append-end') {
      const now = this.clock.now()
      if (now - this.lastMseProgressAt < 1_000) return
      this.lastMseProgressAt = now
    }
    this.record(`mse-${type}`, data)
  }

  freeze(userDescription: string, truncatedReason: string | null = null): PlaybackDiagnosticReport {
    this.cleanupMedia?.()
    this.cleanupMedia = null
    this.record('feedback-freeze', { userDescription: userDescription.slice(0, 500), truncatedReason })
    const report: PlaybackDiagnosticReport = {
      schemaVersion: 1,
      kind: 'playback-diagnostic',
      clientReportId: this.clientReportId,
      createdAt: new Date(this.captureStart).toISOString(),
      buildId: this.buildId,
      sessionId: this.context.sessionId,
      attemptId: this.attemptId,
      captureStart: this.captureStart,
      captureEnd: this.clock.wallClock(),
      truncatedReason,
      userDescription: userDescription.slice(0, 500),
      platform: {
        userAgent: typeof navigator === 'undefined' ? 'unknown' : navigator.userAgent,
        language: typeof navigator === 'undefined' ? 'unknown' : navigator.language,
        online: typeof navigator === 'undefined' ? true : navigator.onLine,
        visibility: typeof document === 'undefined' ? 'visible' : document.visibilityState,
      },
      session: this.context,
      preparation: [...this.preparationEvents],
      events: [...this.events],
      droppedEvents: this.droppedEvents,
    }
    return trimReport(report)
  }

  dispose(): void {
    this.cleanupMedia?.()
    this.cleanupMedia = null
    this.attachedAudio = null
  }
}

export function trimReport(report: PlaybackDiagnosticReport): PlaybackDiagnosticReport {
  let candidate = report
  while (JSON.stringify(candidate).length > MAX_REPORT_BYTES && candidate.events.length > 20) {
    candidate = { ...candidate, events: candidate.events.slice(Math.ceil(candidate.events.length / 4)) }
  }
  return candidate
}

export function diagnosticReportBytes(report: PlaybackDiagnosticReport): number {
  return JSON.stringify(report).length
}

export const PLAYBACK_DIAGNOSTICS_LIMITS = { maxEvents: MAX_EVENTS, maxPreparationEvents: MAX_PREPARATION_EVENTS, maxBufferMs: MAX_BUFFER_MS, maxReportBytes: MAX_REPORT_BYTES }

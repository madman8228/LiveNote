import type { LifecycleEventType } from '../storage/types'

export interface LifecycleEvent {
  eventType: LifecycleEventType
  wallClockMs: number
  elapsedMs: number
  visibilityState: DocumentVisibilityState
}

export class PageLifecycleManager {
  private recording = false
  private elapsedBaseMs = 0
  private monotonicStartedAt = 0
  private started = false
  private readonly onEvent: (event: LifecycleEvent) => void

  constructor(onEvent: (event: LifecycleEvent) => void) {
    this.onEvent = onEvent
  }

  start(): void {
    if (this.started) return
    this.started = true
    document.addEventListener('visibilitychange', this.handleVisibilityChange)
    window.addEventListener('pagehide', this.handlePageHide)
    window.addEventListener('pageshow', this.handlePageShow)
    document.addEventListener('freeze', this.handleFreeze as EventListener)
    document.addEventListener('resume', this.handleResume as EventListener)
  }

  setRecording(recording: boolean, elapsedBaseMs = 0): void {
    this.recording = recording
    if (recording) {
      this.elapsedBaseMs = elapsedBaseMs
      this.monotonicStartedAt = performance.now()
    }
  }

  stop(): void {
    this.recording = false
    this.started = false
    document.removeEventListener('visibilitychange', this.handleVisibilityChange)
    window.removeEventListener('pagehide', this.handlePageHide)
    window.removeEventListener('pageshow', this.handlePageShow)
    document.removeEventListener('freeze', this.handleFreeze as EventListener)
    document.removeEventListener('resume', this.handleResume as EventListener)
  }

  private capture(eventType: LifecycleEventType): void {
    this.onEvent({
      eventType,
      wallClockMs: Date.now(),
      elapsedMs: this.recording ? this.elapsedBaseMs + Math.max(0, performance.now() - this.monotonicStartedAt) : 0,
      visibilityState: document.visibilityState,
    })
  }

  private readonly handleVisibilityChange = (): void => { this.capture('visibilitychange') }
  private readonly handlePageHide = (): void => { this.capture('pagehide') }
  private readonly handlePageShow = (): void => { this.capture('pageshow') }
  private readonly handleFreeze = (): void => { this.capture('freeze') }
  private readonly handleResume = (): void => { this.capture('resume') }
}

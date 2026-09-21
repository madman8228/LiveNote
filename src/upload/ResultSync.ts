export interface ResultSyncOptions {
  intervalMs?: number
  canRun?: () => boolean
  poll: () => Promise<void>
  onError?: (error: unknown) => void
}

/** Keeps result polling independent from recording and upload lifecycles. */
export class ResultSyncManager {
  private readonly intervalMs: number
  private readonly canRun: () => boolean
  private readonly poll: () => Promise<void>
  private readonly onError: (error: unknown) => void
  private timer: number | null = null
  private inFlight: Promise<void> | null = null

  constructor(options: ResultSyncOptions) {
    this.intervalMs = Math.max(5_000, options.intervalMs ?? 30_000)
    this.canRun = options.canRun ?? (() => true)
    this.poll = options.poll
    this.onError = options.onError ?? (() => undefined)
  }

  start(): void {
    if (this.timer !== null) return
    this.timer = window.setInterval(() => { void this.request() }, this.intervalMs)
  }

  stop(): void {
    if (this.timer !== null) window.clearInterval(this.timer)
    this.timer = null
  }

  request(): Promise<void> {
    if (!this.canRun()) return Promise.resolve()
    if (this.inFlight) return this.inFlight
    this.inFlight = this.poll()
      .catch((error) => { this.onError(error) })
      .finally(() => { this.inFlight = null })
    return this.inFlight
  }
}

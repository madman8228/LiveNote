export type WakeLockState = 'UNSUPPORTED' | 'REQUESTING' | 'ACTIVE' | 'RELEASED' | 'FAILED'

export class WakeLockManager {
  private sentinel: WakeLockSentinel | null = null
  private requested = false
  private started = false
  private readonly onStateChange: (state: WakeLockState, message?: string) => void

  constructor(onStateChange: (state: WakeLockState, message?: string) => void) {
    this.onStateChange = onStateChange
  }

  start(): void {
    if (this.started) return
    this.started = true
    document.addEventListener('visibilitychange', this.handleVisibilityChange)
    if (!this.isSupported()) this.onStateChange('UNSUPPORTED', '当前浏览器不支持屏幕常亮。')
    else this.onStateChange('RELEASED')
  }

  async request(): Promise<void> {
    if (!this.isSupported()) {
      this.onStateChange('UNSUPPORTED', '当前浏览器不支持屏幕常亮，录音仍会继续。')
      return
    }
    if (document.visibilityState !== 'visible') {
      this.requested = true
      this.onStateChange('RELEASED', '页面不在前台，返回后会重新申请屏幕常亮。')
      return
    }

    this.requested = true
    this.onStateChange('REQUESTING')
    try {
      await this.sentinel?.release().catch(() => undefined)
      this.sentinel = await navigator.wakeLock.request('screen')
      this.sentinel.addEventListener('release', this.handleRelease)
      this.onStateChange('ACTIVE')
    } catch (error) {
      this.sentinel = null
      this.onStateChange('FAILED', error instanceof Error ? error.message : '无法保持屏幕常亮，录音仍会继续。')
    }
  }

  async release(): Promise<void> {
    this.requested = false
    this.sentinel?.removeEventListener('release', this.handleRelease)
    await this.sentinel?.release().catch(() => undefined)
    this.sentinel = null
    this.onStateChange('RELEASED')
  }

  stop(): void {
    this.requested = false
    this.started = false
    document.removeEventListener('visibilitychange', this.handleVisibilityChange)
    this.sentinel?.removeEventListener('release', this.handleRelease)
    void this.sentinel?.release().catch(() => undefined)
    this.sentinel = null
  }

  private isSupported(): boolean {
    return 'wakeLock' in navigator && typeof navigator.wakeLock?.request === 'function'
  }

  private readonly handleVisibilityChange = (): void => {
    if (!this.requested) return
    if (document.visibilityState === 'visible') void this.request()
    else this.onStateChange('RELEASED', '页面暂时离开前台，系统可能释放屏幕常亮。')
  }

  private readonly handleRelease = (): void => {
    this.sentinel = null
    if (this.requested && document.visibilityState === 'visible') void this.request()
    else if (this.requested) this.onStateChange('RELEASED', '屏幕常亮已被系统释放，返回前台后会重试。')
    else this.onStateChange('RELEASED')
  }
}

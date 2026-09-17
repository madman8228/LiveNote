export type PwaInstallStatus = 'installed' | 'prompt' | 'manual' | 'unavailable'

export interface PwaInstallSnapshot {
  status: PwaInstallStatus
  isStandalone: boolean
  isSecureContext: boolean
  isProductionBuild: boolean
}

interface BeforeInstallPromptEvent extends Event {
  readonly platforms: string[]
  readonly userChoice: Promise<{ outcome: 'accepted' | 'dismissed'; platform: string }>
  prompt(): Promise<void>
}

type SnapshotListener = (snapshot: PwaInstallSnapshot) => void

const INSTALLED_STORAGE_KEY = 'livenote.pwa-installed'

function detectStandalone(): boolean {
  const iosStandalone = Boolean((navigator as Navigator & { standalone?: boolean }).standalone)
  return window.matchMedia('(display-mode: standalone)').matches || iosStandalone
}

export class PwaInstallManager {
  private deferredPrompt: BeforeInstallPromptEvent | null = null
  private readonly listeners = new Set<SnapshotListener>()
  private snapshot: PwaInstallSnapshot = this.createSnapshot('manual')

  subscribe(listener: SnapshotListener): () => void {
    this.listeners.add(listener)
    listener(this.snapshot)
    return () => this.listeners.delete(listener)
  }

  start(): void {
    window.addEventListener('beforeinstallprompt', this.handleBeforeInstallPrompt as EventListener)
    window.addEventListener('appinstalled', this.handleAppInstalled)
    window.matchMedia('(display-mode: standalone)').addEventListener?.('change', this.handleDisplayModeChange)
    this.refresh()
  }

  stop(): void {
    window.removeEventListener('beforeinstallprompt', this.handleBeforeInstallPrompt as EventListener)
    window.removeEventListener('appinstalled', this.handleAppInstalled)
    window.matchMedia('(display-mode: standalone)').removeEventListener?.('change', this.handleDisplayModeChange)
  }

  getSnapshot(): PwaInstallSnapshot {
    return this.snapshot
  }

  async promptInstall(): Promise<'accepted' | 'dismissed' | 'unavailable'> {
    if (!this.deferredPrompt) return 'unavailable'
    const promptEvent = this.deferredPrompt
    this.deferredPrompt = null
    await promptEvent.prompt()
    const choice = await promptEvent.userChoice
    if (choice.outcome === 'accepted') localStorage.setItem(INSTALLED_STORAGE_KEY, '1')
    this.refresh()
    return choice.outcome
  }

  private readonly handleBeforeInstallPrompt = (event: Event): void => {
    event.preventDefault()
    this.deferredPrompt = event as BeforeInstallPromptEvent
    this.refresh()
  }

  private readonly handleAppInstalled = (): void => {
    localStorage.setItem(INSTALLED_STORAGE_KEY, '1')
    this.deferredPrompt = null
    this.refresh()
  }

  private readonly handleDisplayModeChange = (): void => this.refresh()

  private refresh(): void {
    const standalone = detectStandalone()
    const rememberedInstalled = localStorage.getItem(INSTALLED_STORAGE_KEY) === '1'
    const status: PwaInstallStatus = standalone || rememberedInstalled
      ? 'installed'
      : this.deferredPrompt
        ? 'prompt'
        : window.isSecureContext && import.meta.env.PROD
          ? 'manual'
          : 'unavailable'
    this.snapshot = this.createSnapshot(status, standalone)
    this.listeners.forEach((listener) => listener(this.snapshot))
  }

  private createSnapshot(status: PwaInstallStatus, isStandalone = false): PwaInstallSnapshot {
    return { status, isStandalone, isSecureContext: window.isSecureContext, isProductionBuild: import.meta.env.PROD }
  }
}

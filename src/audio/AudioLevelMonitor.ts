type AudioContextConstructor = new () => AudioContext

export class AudioLevelMonitor {
  private context: AudioContext | null = null
  private analyser: AnalyserNode | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private frameId: number | null = null
  private samples: Uint8Array<ArrayBuffer> | null = null
  private readonly onLevel: (level: number) => void

  constructor(onLevel: (level: number) => void) {
    this.onLevel = onLevel
  }

  async start(stream: MediaStream): Promise<void> {
    this.stop()
    const AudioContextClass = (window.AudioContext
      ?? (window as typeof window & { webkitAudioContext?: AudioContextConstructor }).webkitAudioContext) as AudioContextConstructor | undefined

    if (!AudioContextClass) {
      this.onLevel(0)
      return
    }

    this.context = new AudioContextClass()
    await this.context.resume()
    this.source = this.context.createMediaStreamSource(stream)
    this.analyser = this.context.createAnalyser()
    this.analyser.fftSize = 2048
    this.samples = new Uint8Array(this.analyser.fftSize)
    this.source.connect(this.analyser)

    const tick = () => {
      if (!this.analyser || !this.samples) return

      this.analyser.getByteTimeDomainData(this.samples)
      let sum = 0
      for (const sample of this.samples) {
        const normalized = (sample - 128) / 128
        sum += normalized * normalized
      }
      const rms = Math.sqrt(sum / this.samples.length)
      this.onLevel(Math.min(1, rms * 3.2))
      this.frameId = requestAnimationFrame(tick)
    }

    tick()
  }

  stop(): void {
    if (this.frameId !== null) {
      cancelAnimationFrame(this.frameId)
      this.frameId = null
    }
    this.source?.disconnect()
    this.analyser?.disconnect()
    void this.context?.close()
    this.source = null
    this.analyser = null
    this.context = null
    this.samples = null
    this.onLevel(0)
  }
}

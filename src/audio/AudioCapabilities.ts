export interface AudioCapabilities {
  getUserMedia: boolean
  mediaRecorder: boolean
  indexedDB: boolean
  wakeLock: boolean
  webmOpus: boolean
  webm: boolean
  mp4: boolean
}

function supportsMimeType(mimeType: string): boolean {
  return typeof window !== 'undefined'
    && typeof window.MediaRecorder !== 'undefined'
    && window.MediaRecorder.isTypeSupported(mimeType)
}

export function detectAudioCapabilities(): AudioCapabilities {
  const mediaDevicesAvailable = typeof navigator !== 'undefined' && 'mediaDevices' in navigator

  return {
    getUserMedia: mediaDevicesAvailable && typeof navigator.mediaDevices?.getUserMedia === 'function',
    mediaRecorder: typeof window !== 'undefined' && typeof window.MediaRecorder !== 'undefined',
    indexedDB: typeof window !== 'undefined' && 'indexedDB' in window,
    wakeLock: typeof navigator !== 'undefined' && 'wakeLock' in navigator,
    webmOpus: supportsMimeType('audio/webm;codecs=opus'),
    webm: supportsMimeType('audio/webm'),
    mp4: supportsMimeType('audio/mp4'),
  }
}

export function preferredMimeType(mediaRecorderConstructor: MediaRecorderConstructorLike): string | undefined {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
  ]

  return candidates.find((mimeType) => mediaRecorderConstructor.isTypeSupported(mimeType))
}

export interface MediaRecorderConstructorLike {
  new (stream: MediaStream, options?: MediaRecorderOptions): MediaRecorder
  isTypeSupported(mimeType: string): boolean
}

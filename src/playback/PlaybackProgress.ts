export function resolvePlaybackDurationMs(mediaDurationSeconds: number, fallbackDurationMs: number): number {
  if (Number.isFinite(mediaDurationSeconds) && mediaDurationSeconds > 0) return mediaDurationSeconds * 1000
  return Number.isFinite(fallbackDurationMs) && fallbackDurationMs > 0 ? fallbackDurationMs : 0
}

export function playbackProgressPercent(currentTimeSeconds: number, durationMs: number): number {
  if (!Number.isFinite(currentTimeSeconds) || !Number.isFinite(durationMs) || durationMs <= 0) return 0
  return Math.min(100, Math.max(0, (currentTimeSeconds * 1000 / durationMs) * 100))
}

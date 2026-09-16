const RETRY_DELAYS_MS = [2_000, 5_000, 15_000, 30_000, 60_000]

export function retryDelayMs(retryCount: number): number {
  return RETRY_DELAYS_MS[Math.min(Math.max(retryCount, 0), RETRY_DELAYS_MS.length - 1)]
}

export function canRetryNow(retryCount: number, lastAttemptAt: number | null, now = Date.now()): boolean {
  if (!lastAttemptAt) return true
  return now - lastAttemptAt >= retryDelayMs(retryCount)
}

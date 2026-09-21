export interface PlaybackAvailabilitySession {
  serverOnly?: boolean
  serverChunkCount?: number
  segments: Array<{ chunks: unknown[] }>
}

export function hasPlayableSession(session: PlaybackAvailabilitySession | null | undefined): boolean {
  if (!session) return false
  if (session.serverOnly) return (session.serverChunkCount ?? 0) > 0
  return session.segments.some((segment) => segment.chunks.length > 0)
}

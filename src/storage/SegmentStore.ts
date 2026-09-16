import { getAllByIndex, getRecord, putRecord, STORE_NAMES } from './db'
import type { SegmentRecord } from './types'

export const SegmentStore = {
  get(id: string) {
    return getRecord<SegmentRecord>(STORE_NAMES.segments, id)
  },
  listBySessionId(sessionId: string) {
    return getAllByIndex<SegmentRecord>(STORE_NAMES.segments, 'sessionId', sessionId).then((segments) => (
      segments.sort((a, b) => a.index - b.index)
    ))
  },
  put(segment: SegmentRecord) {
    return putRecord(STORE_NAMES.segments, segment)
  },
  async nextIndex(sessionId: string): Promise<number> {
    const segments = await this.listBySessionId(sessionId)
    return segments.length ? Math.max(...segments.map((segment) => segment.index)) + 1 : 1
  },
}
